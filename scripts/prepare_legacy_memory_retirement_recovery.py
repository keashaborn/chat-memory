#!/usr/bin/env python3
from __future__ import annotations

"""Create hash-bound recovery evidence before legacy memory schema retirement."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import asyncpg

SCHEMA_VERSION = "seebx-legacy-memory-recovery-receipt-v1"
ROW_MANIFEST_VERSION = "seebx-legacy-memory-row-manifest-v1"
TARGET_SCHEMAS = ("memory", "memory_ingest_private")
EXPECTED_TABLE_COUNTS = {"memory": 160, "memory_ingest_private": 7}
EXPECTED_DATABASE = "memory"
EXPECTED_ROLE = "sage"
EXPECTED_PORT = 5432
PG_DUMP = "/usr/bin/pg_dump"
PG_RESTORE = "/usr/bin/pg_restore"
CREATEDB = "/usr/bin/createdb"
DROPDB = "/usr/bin/dropdb"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,47}$")
SAFE_DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
LIBPQ_QUERY_ENV = {
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "sslcert": "PGSSLCERT",
    "sslcrl": "PGSSLCRL",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
}


class RecoveryContractError(RuntimeError):
    pass


class RecoveryExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    query_environment: dict[str, str]


def parse_postgres_dsn(dsn: str) -> ConnectionSettings:
    try:
        parsed = urlsplit(dsn)
        port = parsed.port or 5432
    except ValueError as error:
        raise RecoveryContractError("dsn_invalid") from error
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RecoveryContractError("dsn_scheme_invalid")
    host = parsed.hostname or ""
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.removeprefix("/"))
    if not host or not user or not password or not database:
        raise RecoveryContractError("dsn_fields_missing")
    if any(character in database for character in ("/", "\x00", "\n", "\r")):
        raise RecoveryContractError("dsn_database_invalid")

    query_environment: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in seen:
            raise RecoveryContractError("dsn_query_duplicate")
        seen.add(key)
        environment_name = LIBPQ_QUERY_ENV.get(key)
        if environment_name is None:
            raise RecoveryContractError("dsn_query_parameter_unsupported")
        if not value or any(character in value for character in ("\x00", "\n", "\r")):
            raise RecoveryContractError("dsn_query_value_invalid")
        query_environment[environment_name] = value
    return ConnectionSettings(host, port, user, password, database, query_environment)


def replace_dsn_database(dsn: str, database: str) -> str:
    if SAFE_DATABASE.fullmatch(database) is None:
        raise RecoveryContractError("restore_database_name_invalid")
    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + quote(database, safe=""), parsed.query, ""))


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def write_pgpass(settings: ConnectionSettings) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix="seebx-legacy-memory-", suffix=".pgpass")
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        line = ":".join(
            _pgpass_escape(value)
            for value in (
                settings.host,
                str(settings.port),
                "*",
                settings.user,
                settings.password,
            )
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def subprocess_environment(
    settings: ConnectionSettings,
    pgpass_path: Path,
    *,
    dsn_environment_name: str,
) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        dsn_environment_name,
        "POSTGRES_DSN",
        "PGPASSWORD",
        "PGSERVICE",
        "PGSERVICEFILE",
    ):
        environment.pop(name, None)
    environment.update(settings.query_environment)
    environment["PGPASSFILE"] = str(pgpass_path)
    return environment


def connection_arguments(settings: ConnectionSettings) -> list[str]:
    return [
        "--host", settings.host,
        "--port", str(settings.port),
        "--username", settings.user,
    ]


def build_dump_command(
    settings: ConnectionSettings,
    *,
    snapshot_id: str,
    output_path: Path,
    binary: str = "/usr/bin/pg_dump",
) -> list[str]:
    return [
        binary,
        *connection_arguments(settings),
        "--dbname", settings.database,
        "--format=custom",
        "--compress=6",
        "--snapshot", snapshot_id,
        "--schema", "memory",
        "--schema", "memory_ingest_private",
        "--file", str(output_path),
    ]


def build_createdb_command(
    settings: ConnectionSettings,
    database: str,
    binary: str = "/usr/bin/createdb",
) -> list[str]:
    return [
        binary,
        *connection_arguments(settings),
        "--maintenance-db", settings.database,
        "--template", "template0",
        "--encoding", "UTF8",
        database,
    ]


def build_restore_command(
    settings: ConnectionSettings,
    database: str,
    archive: Path,
    binary: str = "/usr/bin/pg_restore",
) -> list[str]:
    return [
        binary,
        *connection_arguments(settings),
        "--dbname", database,
        "--exit-on-error",
        "--single-transaction",
        str(archive),
    ]


def build_dropdb_command(
    settings: ConnectionSettings,
    database: str,
    binary: str = "/usr/bin/dropdb",
) -> list[str]:
    return [
        binary,
        *connection_arguments(settings),
        "--maintenance-db", settings.database,
        database,
    ]


def run_checked(
    command: list[str],
    environment: dict[str, str],
    *,
    label: str,
    timeout: int,
) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RecoveryExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise RecoveryExecutionError(f"{label}_failed")
    return completed.stdout.strip()


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, value: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def collect_row_manifest(connection: Any, database: str) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT n.nspname AS schema_name,
               c.relname AS table_name,
               format('%I.%I', n.nspname, c.relname) AS qualified_name
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY($1::text[])
          AND c.relkind = 'r'
        ORDER BY n.nspname, c.relname
        """,
        list(TARGET_SCHEMAS),
    )
    tables: list[dict[str, Any]] = []
    for row in rows:
        count = await connection.fetchval(
            f"SELECT count(*)::bigint FROM {row['qualified_name']}"
        )
        tables.append(
            {
                "schema": str(row["schema_name"]),
                "table": str(row["table_name"]),
                "rows": int(count),
            }
        )
    counts = {
        schema: sum(1 for table in tables if table["schema"] == schema)
        for schema in TARGET_SCHEMAS
    }
    return {
        "schema_version": ROW_MANIFEST_VERSION,
        "database": database,
        "schemas": list(TARGET_SCHEMAS),
        "ordinary_table_counts": counts,
        "tables": tables,
        "table_inventory_sha256": sha256_bytes(canonical_bytes(tables)),
    }


def validate_source_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("ordinary_table_counts") != EXPECTED_TABLE_COUNTS:
        raise RecoveryContractError("legacy_schema_table_shape_mismatch")
    tables = manifest.get("tables")
    if not isinstance(tables, list) or len(tables) != sum(EXPECTED_TABLE_COUNTS.values()):
        raise RecoveryContractError("legacy_schema_table_manifest_mismatch")
    identities = [(table.get("schema"), table.get("table")) for table in tables]
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise RecoveryContractError("legacy_schema_table_identity_invalid")
    for table in tables:
        if not isinstance(table.get("rows"), int) or table["rows"] < 0:
            raise RecoveryContractError("legacy_schema_row_count_invalid")


def compare_restored_manifest(source: dict[str, Any], restored: dict[str, Any]) -> None:
    validate_source_manifest(source)
    validate_source_manifest(restored)
    if source["schemas"] != restored["schemas"]:
        raise RecoveryExecutionError("restored_schema_list_mismatch")
    if source["tables"] != restored["tables"]:
        raise RecoveryExecutionError("restored_table_or_row_count_mismatch")
    if source["table_inventory_sha256"] != restored["table_inventory_sha256"]:
        raise RecoveryExecutionError("restored_inventory_hash_mismatch")


def prepare_output_directory(root: Path, run_id: str) -> Path:
    if RUN_ID.fullmatch(run_id) is None:
        raise RecoveryContractError("run_id_invalid")
    try:
        root_lstat = root.lstat()
        root_stat = root.stat()
    except OSError as error:
        raise RecoveryContractError("output_root_unavailable") from error
    if stat.S_ISLNK(root_lstat.st_mode):
        raise RecoveryContractError("output_root_symlink_rejected")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RecoveryContractError("output_root_not_directory")
    if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise RecoveryContractError("output_root_permissions_invalid")
    output = root / run_id
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def verify_binaries(paths: tuple[str, ...]) -> None:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise RecoveryContractError("postgres_client_binary_invalid")


async def verify_source_identity(connection: Any, expected_database: str, expected_user: str) -> None:
    row = await connection.fetchrow(
        "SELECT current_database() AS database, current_user AS role"
    )
    if row is None or row["database"] != expected_database or row["role"] != expected_user:
        raise RecoveryContractError("source_database_identity_mismatch")


async def database_exists(dsn: str, database: str) -> bool:
    connection = await asyncpg.connect(dsn, command_timeout=15)
    try:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
                database,
            )
        )
    finally:
        await connection.close()


async def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    dsn = (os.getenv(arguments.dsn_env) or "").strip()
    if not dsn:
        raise RecoveryContractError("dsn_environment_missing")
    settings = parse_postgres_dsn(dsn)
    if settings.database != EXPECTED_DATABASE or settings.user != EXPECTED_ROLE:
        raise RecoveryContractError("dsn_target_identity_mismatch")
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RecoveryContractError("dsn_host_must_be_local")
    if settings.port != EXPECTED_PORT:
        raise RecoveryContractError("dsn_port_must_be_5432")
    if not 60 <= arguments.timeout <= 7200:
        raise RecoveryContractError("timeout_out_of_range")
    verify_binaries((PG_DUMP, PG_RESTORE, CREATEDB, DROPDB))
    output = prepare_output_directory(arguments.output_root, arguments.run_id)
    restore_database = "ls_mem_restore_" + arguments.run_id.replace("-", "_")
    if SAFE_DATABASE.fullmatch(restore_database) is None:
        raise RecoveryContractError("restore_database_name_invalid")

    archive = output / "legacy-memory-schemas.dump"
    archive_partial = output / "legacy-memory-schemas.dump.partial"
    manifest_path = output / "source-row-manifest.json"
    receipt_path = output / "restore-receipt.json"
    pgpass_path = write_pgpass(settings)
    environment = subprocess_environment(
        settings,
        pgpass_path,
        dsn_environment_name=arguments.dsn_env,
    )
    restore_created = False
    try:
        if await database_exists(dsn, restore_database):
            raise RecoveryContractError("restore_database_already_exists")
        source = await asyncpg.connect(dsn, command_timeout=30)
        try:
            await verify_source_identity(
                source,
                EXPECTED_DATABASE,
                EXPECTED_ROLE,
            )
            transaction = source.transaction(isolation="repeatable_read", readonly=True)
            await transaction.start()
            try:
                snapshot_id = str(await source.fetchval("SELECT pg_export_snapshot()"))
                source_manifest = await collect_row_manifest(source, settings.database)
                validate_source_manifest(source_manifest)
                dump_command = build_dump_command(
                    settings,
                    snapshot_id=snapshot_id,
                    output_path=archive_partial,
                    binary=PG_DUMP,
                )
                await asyncio.to_thread(
                    run_checked,
                    dump_command,
                    environment,
                    label="pg_dump",
                    timeout=arguments.timeout,
                )
            finally:
                await transaction.rollback()
        finally:
            await source.close()
        if not archive_partial.is_file() or archive_partial.stat().st_size <= 0:
            raise RecoveryExecutionError("pg_dump_archive_missing")
        os.replace(archive_partial, archive)
        archive.chmod(0o600)
        source_manifest_bytes = canonical_bytes(source_manifest)
        atomic_write(manifest_path, source_manifest_bytes)

        await asyncio.to_thread(
            run_checked,
            build_createdb_command(settings, restore_database, CREATEDB),
            environment,
            label="createdb",
            timeout=60,
        )
        restore_created = True
        await asyncio.to_thread(
            run_checked,
            build_restore_command(
                settings,
                restore_database,
                archive,
                PG_RESTORE,
            ),
            environment,
            label="pg_restore",
            timeout=arguments.timeout,
        )
        restore_dsn = replace_dsn_database(dsn, restore_database)
        restored_connection = await asyncpg.connect(restore_dsn, command_timeout=30)
        try:
            restored_manifest = await collect_row_manifest(
                restored_connection,
                restore_database,
            )
        finally:
            await restored_connection.close()
        compare_restored_manifest(source_manifest, restored_manifest)

        await asyncio.to_thread(
            run_checked,
            build_dropdb_command(settings, restore_database, DROPDB),
            environment,
            label="dropdb",
            timeout=60,
        )
        restore_created = False
        if await database_exists(dsn, restore_database):
            raise RecoveryExecutionError("restore_database_cleanup_unverified")

        repository_root = Path(__file__).resolve().parents[1]
        tool_path = Path(__file__).resolve()
        forward_path = repository_root / "ops/retirements/20260819_legacy_memory_v1/forward.pgsql"
        package_path = repository_root / "ops/retirements/20260819_legacy_memory_v1/package.json"
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "run_id": arguments.run_id,
            "source": {
                "database": settings.database,
                "role": settings.user,
                "schemas": list(TARGET_SCHEMAS),
            },
            "backup": {
                "path": archive.name,
                "format": "pg_dump custom",
                "bytes": archive.stat().st_size,
                "sha256": sha256_file(archive),
            },
            "row_manifest": {
                "path": manifest_path.name,
                "sha256": sha256_bytes(source_manifest_bytes),
                "table_inventory_sha256": source_manifest["table_inventory_sha256"],
                "ordinary_table_counts": source_manifest["ordinary_table_counts"],
            },
            "restore": {
                "database": restore_database,
                "verified": True,
                "table_inventory_sha256": restored_manifest["table_inventory_sha256"],
                "disposable_database_dropped": True,
            },
            "bindings": {
                "tool_sha256": sha256_file(tool_path),
                "retirement_forward_sha256": sha256_file(forward_path),
                "retirement_package_sha256": sha256_file(package_path),
            },
        }
        receipt_bytes = canonical_bytes(receipt)
        atomic_write(receipt_path, receipt_bytes)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "output_directory": str(output),
            "backup_sha256": receipt["backup"]["sha256"],
            "restore_receipt_sha256": sha256_bytes(receipt_bytes),
            "table_inventory_sha256": source_manifest["table_inventory_sha256"],
        }
    except Exception:
        if restore_created:
            try:
                await asyncio.to_thread(
                    run_checked,
                    build_dropdb_command(settings, restore_database, DROPDB),
                    environment,
                    label="failure_cleanup_dropdb",
                    timeout=60,
                )
            except Exception as cleanup_error:
                raise RecoveryExecutionError("failure_cleanup_dropdb_failed") from cleanup_error
        raise
    finally:
        archive_partial.unlink(missing_ok=True)
        pgpass_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="LEGACY_MEMORY_ADMIN_DSN")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(execute(arguments))
    except RecoveryContractError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "recovery_contract_error",
            "reason": str(error),
        }
        exit_code = 2
    except RecoveryExecutionError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "recovery_execution_error",
            "reason": str(error),
        }
        exit_code = 3
    except Exception:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "recovery_unexpected_error",
        }
        exit_code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
