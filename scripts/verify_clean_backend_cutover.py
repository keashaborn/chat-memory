#!/usr/bin/env python3
from __future__ import annotations

"""Read-only preflight for the clean SeeBx backend cutover."""

import argparse
import asyncio
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import asyncpg

try:
    from verify_runtime_dependencies import DependencyContractError, load_contract, verify_contract
except ModuleNotFoundError:  # Imported as scripts.verify_clean_backend_cutover in tests.
    from scripts.verify_runtime_dependencies import (
        DependencyContractError,
        load_contract,
        verify_contract,
    )

SCHEMA_VERSION = "seebx-clean-backend-cutover-preflight-v1"
LEGACY_CRON_COMMAND = "cd /opt/chat-memory && ./eval_all_users.sh"
LEGACY_TRIGGER_NAMES = (
    "chat_attachments_serialize_source_erasure",
    "chat_log_serialize_source_erasure",
    "threads_serialize_source_erasure",
    "response_transcript_serialize_source_erasure",
)
HISTORY_SIGNATURE = "chat_history_private.clear_history(uuid,text,uuid,integer)"
TAIL_SIGNATURE = "chat_history_private.clear_message_tail(uuid,uuid)"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SYSTEM_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class PreflightOperationalError(RuntimeError):
    pass


def _check(name: str, passed: bool, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "status": "pass" if passed else "fail", "actual": actual, "expected": expected}


def _valid_hash(value: str | None) -> bool:
    return bool(value and SHA256.fullmatch(value))


def evaluate_readiness(
    database: Mapping[str, Any],
    *,
    crontab_text: str,
    dependencies: Mapping[str, Any],
    backup_sha256: str | None = None,
    restore_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    history_definition = str(database.get("clear_history_definition") or "").lower()
    tail_definition = str(database.get("clear_message_tail_definition") or "").lower()
    legacy_cron_present = any(
        LEGACY_CRON_COMMAND in line
        for line in crontab_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    deployment_checks = [
        _check("runtime_dependencies", dependencies.get("status") == "pass", dependencies.get("status"), "pass"),
        _check("database_transaction_read_only", database.get("transaction_read_only") is True, database.get("transaction_read_only"), True),
        _check("legacy_memory_cron_absent", not legacy_cron_present, legacy_cron_present, False),
        _check("zep_turn_outbox_present", bool(database.get("zep_turn_outbox_present")), bool(database.get("zep_turn_outbox_present")), True),
        _check("clear_history_present", bool(database.get("clear_history_present")), bool(database.get("clear_history_present")), True),
        _check("clear_message_tail_present", bool(database.get("clear_message_tail_present")), bool(database.get("clear_message_tail_present")), True),
        _check("clear_history_uses_zep_outbox", "conversation_sync_private.zep_turn_outbox" in history_definition, "conversation_sync_private.zep_turn_outbox" in history_definition, True),
        _check("clear_message_tail_uses_zep_outbox", "conversation_sync_private.zep_turn_outbox" in tail_definition, "conversation_sync_private.zep_turn_outbox" in tail_definition, True),
        _check("clear_functions_do_not_use_legacy_memory", "memory_ingest_private" not in history_definition and "memory_ingest_private" not in tail_definition, "memory_ingest_private" in history_definition or "memory_ingest_private" in tail_definition, False),
        _check("legacy_source_erasure_triggers_absent", int(database.get("legacy_trigger_count", -1)) == 0, int(database.get("legacy_trigger_count", -1)), 0),
        _check("legacy_ingest_nonterminal_zero", int(database.get("nonterminal_ingest", -1)) == 0, int(database.get("nonterminal_ingest", -1)), 0),
        _check("legacy_erasure_nonterminal_zero", int(database.get("nonterminal_erasure", -1)) == 0, int(database.get("nonterminal_erasure", -1)), 0),
        _check("telemetry_retention_owner_present", bool(database.get("telemetry_retention_present")), bool(database.get("telemetry_retention_present")), True),
    ]
    candidate_deploy_ready = all(item["status"] == "pass" for item in deployment_checks)

    memory_schema_present = bool(database.get("memory_schema_present"))
    ingest_schema_present = bool(database.get("memory_ingest_schema_present"))
    schemas_already_retired = not memory_schema_present and not ingest_schema_present
    retirement_checks = [
        *deployment_checks,
        _check("legacy_memory_schema_present", memory_schema_present, memory_schema_present, True),
        _check("legacy_memory_ingest_schema_present", ingest_schema_present, ingest_schema_present, True),
        _check("legacy_memory_table_shape", int(database.get("memory_table_count", -1)) == 160, int(database.get("memory_table_count", -1)), 160),
        _check("legacy_memory_ingest_table_shape", int(database.get("memory_ingest_table_count", -1)) == 7, int(database.get("memory_ingest_table_count", -1)), 7),
        _check("external_legacy_function_references_absent", int(database.get("external_legacy_function_refs", -1)) == 0, int(database.get("external_legacy_function_refs", -1)), 0),
        _check("backup_sha256_bound", _valid_hash(backup_sha256), bool(_valid_hash(backup_sha256)), True),
        _check("restore_receipt_sha256_bound", _valid_hash(restore_receipt_sha256), bool(_valid_hash(restore_receipt_sha256)), True),
    ]
    retirement_ready = not schemas_already_retired and all(item["status"] == "pass" for item in retirement_checks)
    deployment_gaps = [item["name"] for item in deployment_checks if item["status"] != "pass"]
    retirement_gaps = [] if schemas_already_retired else [
        item["name"] for item in retirement_checks if item["status"] != "pass"
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if candidate_deploy_ready else "fail",
        "candidate_deploy_ready": candidate_deploy_ready,
        "legacy_schema_state": "retired" if schemas_already_retired else "present",
        "legacy_schema_retirement_ready": retirement_ready,
        "deployment_checks": deployment_checks,
        "retirement_checks": retirement_checks,
        "deployment_gaps": deployment_gaps,
        "retirement_gaps": retirement_gaps,
        "gaps": retirement_gaps if not schemas_already_retired else deployment_gaps,
        "dependencies": dependencies,
    }


async def collect_database_state(connection: Any) -> dict[str, Any]:
    async def scalar(sql: str, *args: Any) -> Any:
        return await connection.fetchval(sql, *args)

    outbox = await scalar("SELECT to_regclass('conversation_sync_private.zep_turn_outbox') IS NOT NULL")
    history_oid = await scalar(
        "SELECT to_regprocedure($1::text)::oid",
        HISTORY_SIGNATURE,
    )
    tail_oid = await scalar(
        "SELECT to_regprocedure($1::text)::oid",
        TAIL_SIGNATURE,
    )
    memory_schema = await scalar("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'memory')")
    ingest_schema = await scalar("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'memory_ingest_private')")
    ingest_outbox = await scalar("SELECT to_regclass('memory_ingest_private.memory_ingest_outbox') IS NOT NULL")
    erasure_table = await scalar("SELECT to_regclass('memory_ingest_private.source_erasure_operation') IS NOT NULL")

    state: dict[str, Any] = {
        "zep_turn_outbox_present": bool(outbox),
        "clear_history_present": history_oid is not None,
        "clear_message_tail_present": tail_oid is not None,
        "clear_history_definition": await scalar("SELECT pg_get_functiondef($1::oid)", history_oid) if history_oid else "",
        "clear_message_tail_definition": await scalar("SELECT pg_get_functiondef($1::oid)", tail_oid) if tail_oid else "",
        "legacy_trigger_count": await scalar("SELECT count(*)::integer FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY($1::text[])", list(LEGACY_TRIGGER_NAMES)),
        "telemetry_retention_present": bool(await scalar("SELECT to_regprocedure('ai_operations.enforce_telemetry_retention_v1()') IS NOT NULL")),
        "memory_schema_present": bool(memory_schema),
        "memory_ingest_schema_present": bool(ingest_schema),
        "memory_table_count": await scalar("SELECT count(*)::integer FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'memory' AND c.relkind = 'r'"),
        "memory_ingest_table_count": await scalar("SELECT count(*)::integer FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'memory_ingest_private' AND c.relkind = 'r'"),
        "external_legacy_function_refs": await scalar("SELECT count(*)::integer FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prokind IN ('f','p') AND n.nspname NOT IN ('memory','memory_ingest_private') AND pg_get_functiondef(p.oid) ILIKE '%memory_ingest_private%'"),
        "nonterminal_ingest": 0,
        "nonterminal_erasure": 0,
    }
    if ingest_outbox:
        state["nonterminal_ingest"] = await scalar("SELECT count(*)::integer FROM memory_ingest_private.memory_ingest_outbox WHERE state NOT IN ('completed','skipped')")
    if erasure_table:
        state["nonterminal_erasure"] = await scalar("SELECT count(*)::integer FROM memory_ingest_private.source_erasure_operation WHERE state <> 'completed'")
    return state


async def collect_database_state_read_only(connection: Any) -> dict[str, Any]:
    async with connection.transaction(isolation="serializable", readonly=True):
        transaction_read_only = await connection.fetchval("SHOW transaction_read_only")
        if transaction_read_only != "on":
            raise PreflightOperationalError("database_transaction_not_read_only")
        state = await collect_database_state(connection)
        state["transaction_read_only"] = True
        return state


def read_crontab(user: str | None = None) -> str:
    if user is not None and not SYSTEM_USER.fullmatch(user):
        raise PreflightOperationalError("crontab_user_invalid")
    command = ["crontab"]
    if user is not None:
        command.extend(["-u", user])
    command.append("-l")
    process = subprocess.run(command, text=True, capture_output=True, check=False, timeout=10)
    if process.returncode == 0:
        return process.stdout
    if process.returncode == 1 and "no crontab" in process.stderr.lower():
        return ""
    raise PreflightOperationalError("crontab_read_failed")


async def run(arguments: argparse.Namespace) -> dict[str, Any]:
    dsn = (os.getenv(arguments.dsn_env) or "").strip()
    if not dsn:
        raise PreflightOperationalError(f"{arguments.dsn_env}_missing")
    python_version, pins = load_contract(arguments.pyproject)
    dependencies = verify_contract(python_version, pins)
    connection = await asyncpg.connect(dsn, command_timeout=20)
    try:
        database = await collect_database_state_read_only(connection)
    finally:
        await connection.close()
    return evaluate_readiness(
        database,
        crontab_text=read_crontab(arguments.crontab_user),
        dependencies=dependencies,
        backup_sha256=arguments.backup_sha256,
        restore_receipt_sha256=arguments.restore_receipt_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="POSTGRES_DSN")
    parser.add_argument("--crontab-user")
    parser.add_argument("--pyproject", type=Path, default=Path(__file__).resolve().parents[1] / "pyproject.toml")
    parser.add_argument("--backup-sha256")
    parser.add_argument("--restore-receipt-sha256")
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run(arguments))
    except DependencyContractError as error:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "error": "dependency_contract_error", "reason": str(error)}
        exit_code = 3
    except PreflightOperationalError as error:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "error": "preflight_operational_error", "reason": str(error)}
        exit_code = 3
    except Exception:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "error": "preflight_unexpected_error"}
        exit_code = 3
    else:
        exit_code = 0 if result["candidate_deploy_ready"] else 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
