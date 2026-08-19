#!/usr/bin/env python3
from __future__ import annotations

"""Verify the Zep chat-history cutover against a disposable restored database."""

import argparse
import asyncio
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import asyncpg

try:
    from prepare_legacy_memory_retirement_recovery import (
        CREATEDB,
        DROPDB,
        EXPECTED_DATABASE,
        EXPECTED_PORT,
        EXPECTED_ROLE,
        PG_RESTORE,
        atomic_write,
        build_createdb_command,
        build_dropdb_command,
        build_restore_command,
        canonical_bytes,
        parse_postgres_dsn,
        prepare_output_directory,
        replace_dsn_database,
        sha256_bytes,
        sha256_file,
        subprocess_environment,
        write_pgpass,
    )
except ModuleNotFoundError:
    from scripts.prepare_legacy_memory_retirement_recovery import (
        CREATEDB,
        DROPDB,
        EXPECTED_DATABASE,
        EXPECTED_PORT,
        EXPECTED_ROLE,
        PG_RESTORE,
        atomic_write,
        build_createdb_command,
        build_dropdb_command,
        build_restore_command,
        canonical_bytes,
        parse_postgres_dsn,
        prepare_output_directory,
        replace_dsn_database,
        sha256_bytes,
        sha256_file,
        subprocess_environment,
        write_pgpass,
    )

SCHEMA_VERSION = "seebx-zep-chat-history-migration-receipt-v1"
PACKAGE_SCHEMA_VERSION = "seebx-zep-chat-history-migration-package-v1"
RECOVERY_SCHEMA_VERSION = "seebx-legacy-memory-recovery-receipt-v1"
PSQL = "/usr/bin/psql"
PACKAGE_RELATIVE_PATH = Path(
    "ops/migrations/20260819_zep_chat_history_cutover_v1/package.json"
)
HISTORY_SIGNATURE = "chat_history_private.clear_history(uuid,text,uuid,integer)"
TAIL_SIGNATURE = "chat_history_private.clear_message_tail(uuid,uuid)"
OUTBOX = "conversation_sync_private.zep_turn_outbox"
LEGACY_TRIGGER_NAMES = (
    "chat_attachments_serialize_source_erasure",
    "chat_log_serialize_source_erasure",
    "threads_serialize_source_erasure",
    "response_transcript_serialize_source_erasure",
)


class MigrationContractError(RuntimeError):
    pass


class MigrationExecutionError(RuntimeError):
    pass


def _secure_regular_file(path: Path) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise MigrationContractError("evidence_file_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise MigrationContractError("evidence_file_type_invalid")
    if item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise MigrationContractError("evidence_file_permissions_invalid")
    return path.resolve(strict=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MigrationContractError("json_evidence_invalid") from error
    if not isinstance(value, dict):
        raise MigrationContractError("json_evidence_not_object")
    return value


def verify_recovery_source(backup: Path, receipt_path: Path) -> dict[str, Any]:
    backup = _secure_regular_file(backup)
    receipt_path = _secure_regular_file(receipt_path)
    if backup.parent != receipt_path.parent:
        raise MigrationContractError("recovery_evidence_directory_mismatch")
    receipt = _read_json(receipt_path)
    if (
        receipt.get("schema_version") != RECOVERY_SCHEMA_VERSION
        or receipt.get("status") != "pass"
    ):
        raise MigrationContractError("recovery_receipt_status_invalid")
    backup_record = receipt.get("backup")
    restore_record = receipt.get("restore")
    row_record = receipt.get("row_manifest")
    if not isinstance(backup_record, dict) or not isinstance(restore_record, dict):
        raise MigrationContractError("recovery_receipt_shape_invalid")
    if not isinstance(row_record, dict):
        raise MigrationContractError("recovery_receipt_shape_invalid")
    if (
        backup_record.get("path") != backup.name
        or backup_record.get("scope") != "full_database"
        or backup_record.get("sha256") != sha256_file(backup)
        or restore_record.get("verified") is not True
        or restore_record.get("disposable_database_dropped") is not True
        or row_record.get("ordinary_table_counts")
        != {"memory": 160, "memory_ingest_private": 7}
    ):
        raise MigrationContractError("recovery_receipt_binding_invalid")
    return {
        "backup_sha256": backup_record["sha256"],
        "recovery_receipt_sha256": sha256_file(receipt_path),
        "legacy_table_inventory_sha256": row_record.get("table_inventory_sha256"),
    }


def _resolve_package_file(repository_root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path or raw_path.startswith("/"):
        raise MigrationContractError("migration_path_invalid")
    root = repository_root.resolve(strict=True)
    path = (root / raw_path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise MigrationContractError("migration_path_outside_repository")
    return path


def verify_package(repository_root: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    package_path = repository_root / PACKAGE_RELATIVE_PATH
    package = _read_json(package_path)
    if package.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise MigrationContractError("migration_package_schema_invalid")
    target = package.get("target")
    if target != {
        "server": "seebx",
        "database": EXPECTED_DATABASE,
        "execution_role": EXPECTED_ROLE,
    }:
        raise MigrationContractError("migration_package_target_invalid")
    inputs = package.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "telemetry_forward",
        "zep_outbox_forward",
        "chat_history_forward",
        "chat_history_rollback",
        "zep_outbox_rollback",
        "telemetry_rollback",
    }:
        raise MigrationContractError("migration_package_inputs_invalid")
    resolved: dict[str, Path] = {}
    for name, record in inputs.items():
        if not isinstance(record, dict):
            raise MigrationContractError("migration_package_input_invalid")
        path = _resolve_package_file(repository_root, record.get("path"))
        if record.get("sha256") != sha256_file(path):
            raise MigrationContractError("migration_package_hash_mismatch")
        resolved[name] = path
    tool_record = package.get("tool")
    if not isinstance(tool_record, dict):
        raise MigrationContractError("migration_package_tool_invalid")
    tool_path = _resolve_package_file(repository_root, tool_record.get("path"))
    if tool_record.get("sha256") != sha256_file(tool_path):
        raise MigrationContractError("migration_package_tool_hash_mismatch")
    return package, resolved


def build_psql_command(
    settings: Any,
    database: str,
    sql_path: Path,
    *,
    single_transaction: bool,
) -> list[str]:
    command = [
        PSQL,
        "--host", settings.host,
        "--port", str(settings.port),
        "--username", settings.user,
        "--dbname", database,
        "--no-psqlrc",
        "--set", "ON_ERROR_STOP=1",
        "--quiet",
    ]
    if single_transaction:
        command.append("--single-transaction")
    command.extend(("--file", str(sql_path)))
    return command


def run_checked(
    command: list[str],
    environment: Mapping[str, str],
    *,
    label: str,
    timeout: int,
) -> None:
    try:
        completed = subprocess.run(
            command,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MigrationExecutionError(label + "_execution_failed") from error
    if completed.returncode != 0:
        raise MigrationExecutionError(label + "_failed")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def database_exists(dsn: str, database: str) -> bool:
    connection = await asyncpg.connect(dsn, command_timeout=15)
    try:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=$1)",
                database,
            )
        )
    finally:
        await connection.close()


async def collect_table_manifest(connection: Any) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname AS schema_name, c.relname AS table_name
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        WHERE c.relkind='r'
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp_%'
        ORDER BY n.nspname,c.relname
        """
    )
    result = []
    for row in rows:
        schema = str(row["schema_name"])
        table = str(row["table_name"])
        count = await connection.fetchval(
            "SELECT count(*)::bigint FROM "
            + _quote_identifier(schema)
            + "."
            + _quote_identifier(table)
        )
        result.append({"schema": schema, "table": table, "rows": int(count)})
    return result


async def _function_record(connection: Any, signature: str) -> dict[str, Any] | None:
    oid = await connection.fetchval(
        "SELECT to_regprocedure($1::text)::oid", signature
    )
    if oid is None:
        return None
    row = await connection.fetchrow(
        """
        SELECT p.proowner::regrole::text AS owner,
               pg_get_functiondef(p.oid) AS definition,
               p.prosrc AS body,
               p.proconfig AS config,
               has_function_privilege('brains_app',p.oid,'EXECUTE') AS brains_execute,
               NOT EXISTS (
                 SELECT 1
                 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) AS acl
                 WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
               ) AS public_execute_revoked
        FROM pg_proc AS p
        WHERE p.oid=$1::oid
        """,
        oid,
    )
    if row is None:
        raise MigrationExecutionError("function_identity_disappeared")
    definition = str(row["definition"])
    return {
        "owner": str(row["owner"]),
        "definition_sha256": sha256_bytes(definition.encode()),
        "body_sha256": sha256_bytes(str(row["body"]).encode()),
        "config": list(row["config"] or []),
        "brains_execute": bool(row["brains_execute"]),
        "public_execute_revoked": bool(row["public_execute_revoked"]),
    }


async def collect_database_state(connection: Any) -> dict[str, Any]:
    history_oid = await connection.fetchval(
        "SELECT to_regprocedure($1::text)::oid", HISTORY_SIGNATURE
    )
    tail_oid = await connection.fetchval(
        "SELECT to_regprocedure($1::text)::oid", TAIL_SIGNATURE
    )
    if history_oid is None or tail_oid is None:
        raise MigrationExecutionError("chat_history_functions_missing")
    history_definition = str(
        await connection.fetchval("SELECT pg_get_functiondef($1::oid)", history_oid)
    )
    tail_definition = str(
        await connection.fetchval("SELECT pg_get_functiondef($1::oid)", tail_oid)
    )
    triggers = await connection.fetch(
        """
        SELECT t.tgname, pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger AS t
        WHERE NOT t.tgisinternal AND t.tgname=ANY($1::text[])
        ORDER BY t.tgname
        """,
        list(LEGACY_TRIGGER_NAMES),
    )
    table_manifest = await collect_table_manifest(connection)
    return {
        "outbox_present": bool(
            await connection.fetchval("SELECT to_regclass($1::text) IS NOT NULL", OUTBOX)
        ),
        "history_definition_sha256": sha256_bytes(history_definition.encode()),
        "tail_definition_sha256": sha256_bytes(tail_definition.encode()),
        "history_definition": history_definition.lower(),
        "tail_definition": tail_definition.lower(),
        "legacy_telemetry": await _function_record(
            connection,
            "memory.enforce_telemetry_retention_v1()",
        ),
        "canonical_telemetry": await _function_record(
            connection,
            "ai_operations.enforce_telemetry_retention_v1()",
        ),
        "legacy_triggers": [
            {"name": str(row["tgname"]), "definition": str(row["definition"])}
            for row in triggers
        ],
        "table_manifest": table_manifest,
        "table_manifest_sha256": sha256_bytes(canonical_bytes(table_manifest)),
    }


def validate_forward_state(
    baseline: Mapping[str, Any],
    forward: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        baseline.get("outbox_present") is not False
        or baseline.get("legacy_telemetry") is None
        or baseline.get("canonical_telemetry") is not None
    ):
        raise MigrationExecutionError("baseline_state_invalid")
    expected_tables = list(baseline["table_manifest"])
    expected_tables.append(
        {"schema": "conversation_sync_private", "table": "zep_turn_outbox", "rows": 0}
    )
    expected_tables.sort(key=lambda item: (item["schema"], item["table"]))
    history = str(forward.get("history_definition") or "")
    tail = str(forward.get("tail_definition") or "")
    if (
        forward.get("outbox_present") is not True
        or forward.get("legacy_triggers") != []
        or OUTBOX not in history
        or OUTBOX not in tail
        or "memory_ingest_private" in history
        or "memory_ingest_private" in tail
        or forward.get("legacy_telemetry") is not None
        or forward.get("canonical_telemetry") is None
        or forward["canonical_telemetry"].get("owner") != "sage"
        or forward["canonical_telemetry"].get("body_sha256")
        != baseline["legacy_telemetry"].get("body_sha256")
        or forward["canonical_telemetry"].get("config")
        != ["search_path=pg_catalog, public"]
        or forward["canonical_telemetry"].get("brains_execute") is not True
        or forward["canonical_telemetry"].get("public_execute_revoked") is not True
        or forward.get("table_manifest") != expected_tables
    ):
        raise MigrationExecutionError("forward_state_mismatch")
    return {
        "outbox_present": True,
        "outbox_rows": 0,
        "legacy_trigger_count": 0,
        "canonical_telemetry_present": True,
        "table_manifest_sha256": forward["table_manifest_sha256"],
    }


def validate_rollback_state(
    baseline: Mapping[str, Any],
    rollback: Mapping[str, Any],
) -> dict[str, Any]:
    compared_keys = (
        "outbox_present",
        "history_definition_sha256",
        "tail_definition_sha256",
        "legacy_telemetry",
        "canonical_telemetry",
        "legacy_triggers",
        "table_manifest",
        "table_manifest_sha256",
    )
    if any(rollback.get(key) != baseline.get(key) for key in compared_keys):
        raise MigrationExecutionError("rollback_state_mismatch")
    return {
        "exact_baseline_restored": True,
        "outbox_present": False,
        "legacy_trigger_count": len(rollback["legacy_triggers"]),
        "table_manifest_sha256": rollback["table_manifest_sha256"],
    }


async def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    dsn = (os.getenv(arguments.dsn_env) or "").strip()
    if not dsn:
        raise MigrationContractError("dsn_environment_missing")
    settings = parse_postgres_dsn(dsn)
    if (
        settings.database != EXPECTED_DATABASE
        or settings.user != EXPECTED_ROLE
        or settings.host not in {"127.0.0.1", "localhost", "::1"}
        or settings.port != EXPECTED_PORT
    ):
        raise MigrationContractError("dsn_target_identity_mismatch")
    if not 60 <= arguments.timeout <= 7200:
        raise MigrationContractError("timeout_out_of_range")
    for raw_path in (PSQL, PG_RESTORE, CREATEDB, DROPDB):
        if not Path(raw_path).is_file() or not os.access(raw_path, os.X_OK):
            raise MigrationContractError("postgres_client_binary_invalid")

    repository_root = Path(__file__).resolve().parents[1]
    package, migration_paths = verify_package(repository_root)
    recovery = verify_recovery_source(arguments.backup, arguments.recovery_receipt)
    output = prepare_output_directory(arguments.output_root, arguments.run_id)
    receipt_path = output / "migration-verification-receipt.json"
    temporary_database = "ls_zep_" + arguments.run_id.replace("-", "_")
    if len(temporary_database) > 63:
        raise MigrationContractError("temporary_database_name_invalid")

    pgpass_path = write_pgpass(settings)
    environment = subprocess_environment(
        settings,
        pgpass_path,
        dsn_environment_name=arguments.dsn_env,
    )
    created = False
    try:
        if await database_exists(dsn, temporary_database):
            raise MigrationContractError("temporary_database_already_exists")
        await asyncio.to_thread(
            run_checked,
            build_createdb_command(settings, temporary_database, CREATEDB),
            environment,
            label="createdb",
            timeout=60,
        )
        created = True
        await asyncio.to_thread(
            run_checked,
            build_restore_command(
                settings,
                temporary_database,
                arguments.backup,
                PG_RESTORE,
            ),
            environment,
            label="pg_restore",
            timeout=arguments.timeout,
        )

        temporary_dsn = replace_dsn_database(dsn, temporary_database)
        connection = await asyncpg.connect(temporary_dsn, command_timeout=60)
        try:
            baseline = await collect_database_state(connection)
        finally:
            await connection.close()

        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["telemetry_forward"],
                single_transaction=False,
            ),
            environment,
            label="telemetry_forward",
            timeout=arguments.timeout,
        )
        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["zep_outbox_forward"],
                single_transaction=False,
            ),
            environment,
            label="zep_outbox_forward",
            timeout=arguments.timeout,
        )
        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["chat_history_forward"],
                single_transaction=True,
            ),
            environment,
            label="chat_history_forward",
            timeout=arguments.timeout,
        )

        connection = await asyncpg.connect(temporary_dsn, command_timeout=60)
        try:
            forward_state = await collect_database_state(connection)
        finally:
            await connection.close()
        forward_summary = validate_forward_state(baseline, forward_state)

        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["chat_history_rollback"],
                single_transaction=True,
            ),
            environment,
            label="chat_history_rollback",
            timeout=arguments.timeout,
        )
        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["zep_outbox_rollback"],
                single_transaction=False,
            ),
            environment,
            label="zep_outbox_rollback",
            timeout=arguments.timeout,
        )

        await asyncio.to_thread(
            run_checked,
            build_psql_command(
                settings,
                temporary_database,
                migration_paths["telemetry_rollback"],
                single_transaction=False,
            ),
            environment,
            label="telemetry_rollback",
            timeout=arguments.timeout,
        )

        connection = await asyncpg.connect(temporary_dsn, command_timeout=60)
        try:
            rollback_state = await collect_database_state(connection)
        finally:
            await connection.close()
        rollback_summary = validate_rollback_state(baseline, rollback_state)

        await asyncio.to_thread(
            run_checked,
            build_dropdb_command(settings, temporary_database, DROPDB),
            environment,
            label="dropdb",
            timeout=60,
        )
        created = False
        if await database_exists(dsn, temporary_database):
            raise MigrationExecutionError("temporary_database_cleanup_unverified")

        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "run_id": arguments.run_id,
            "source": recovery,
            "baseline": {
                "table_count": len(baseline["table_manifest"]),
                "table_manifest_sha256": baseline["table_manifest_sha256"],
                "legacy_trigger_count": len(baseline["legacy_triggers"]),
            },
            "forward": forward_summary,
            "rollback": rollback_summary,
            "temporary_database_dropped": True,
            "bindings": {
                "package_sha256": sha256_file(repository_root / PACKAGE_RELATIVE_PATH),
                "tool_sha256": sha256_file(Path(__file__).resolve()),
                "migrations": {
                    name: sha256_file(path)
                    for name, path in sorted(migration_paths.items())
                },
            },
        }
        receipt_bytes = canonical_bytes(receipt)
        atomic_write(receipt_path, receipt_bytes)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "output_directory": str(output),
            "receipt_sha256": sha256_bytes(receipt_bytes),
            "baseline_table_manifest_sha256": baseline["table_manifest_sha256"],
            "forward_table_manifest_sha256": forward_state["table_manifest_sha256"],
        }
    except Exception:
        if created:
            try:
                await asyncio.to_thread(
                    run_checked,
                    build_dropdb_command(settings, temporary_database, DROPDB),
                    environment,
                    label="failure_cleanup_dropdb",
                    timeout=60,
                )
            except Exception as cleanup_error:
                raise MigrationExecutionError(
                    "failure_cleanup_dropdb_failed"
                ) from cleanup_error
        raise
    finally:
        pgpass_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="LEGACY_MEMORY_ADMIN_DSN")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--recovery-receipt", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    arguments = parser.parse_args(argv)
    exit_code = 0
    try:
        result = asyncio.run(execute(arguments))
    except MigrationContractError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "migration_contract_error",
            "reason": str(error),
        }
        exit_code = 2
    except MigrationExecutionError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "migration_execution_error",
            "reason": str(error),
        }
        exit_code = 3
    except Exception:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "migration_unexpected_error",
        }
        exit_code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
