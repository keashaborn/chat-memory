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
except ModuleNotFoundError:
    from scripts.verify_runtime_dependencies import DependencyContractError, load_contract, verify_contract

SCHEMA_VERSION = "seebx-clean-backend-cutover-preflight-v2"
EXPECTED_DATABASE = "memory"
EXPECTED_APPLICATION_ROLE = "brains_app"
EXPECTED_INSPECTION_ROLE = "lifeswitch_retirement_auditor"
EXPECTED_BACKUP_SHA256 = "0adc9bcaba1baee76b815aeb3000ce3c0685367648e976c138354145668afa15"
EXPECTED_RESTORE_RECEIPT_SHA256 = "1f5a117b4dd83176e34141340c9fd9eede8623f1319d8cfd260fbf5f37cb9e24"
LEGACY_CRON_COMMAND = "cd /opt/chat-memory && ./eval_all_users.sh"
LEGACY_TRIGGER_NAMES = (
    "chat_attachments_serialize_source_erasure", "chat_log_serialize_source_erasure",
    "threads_serialize_source_erasure", "response_transcript_serialize_source_erasure",
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
    database: Mapping[str, Any], *, crontab_text: str, dependencies: Mapping[str, Any],
    application_boundary: Mapping[str, Any] | None = None,
    inspection_boundary: Mapping[str, Any] | None = None,
    backup_sha256: str | None = None, restore_receipt_sha256: str | None = None,
    attestation_receipt_sha256: str | None = None,
    quarantine_ciphertext_sha256: str | None = None,
    key_custody_receipt_sha256: str | None = None,
    dependency_catalog_sha256: str | None = None,
) -> dict[str, Any]:
    application = application_boundary or {}
    inspection = inspection_boundary or {}
    history_definition = str(database.get("clear_history_definition") or "").lower()
    tail_definition = str(database.get("clear_message_tail_definition") or "").lower()
    legacy_cron_present = any(LEGACY_CRON_COMMAND in line for line in crontab_text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    deployment_checks = [
        _check("runtime_dependencies", dependencies.get("status") == "pass", dependencies.get("status"), "pass"),
        _check("database_transaction_read_only", database.get("transaction_read_only") is True, database.get("transaction_read_only"), True),
        _check("application_database_identity", application.get("database") == EXPECTED_DATABASE, application.get("database"), EXPECTED_DATABASE),
        _check("application_role_identity", application.get("role") == EXPECTED_APPLICATION_ROLE, application.get("role"), EXPECTED_APPLICATION_ROLE),
        _check("application_legacy_private_select_denied", application.get("legacy_private_select") is False, application.get("legacy_private_select"), False),
        _check("application_legacy_private_write_denied", application.get("legacy_private_write") is False, application.get("legacy_private_write"), False),
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
        _check("inspection_database_identity", inspection.get("database") == EXPECTED_DATABASE, inspection.get("database"), EXPECTED_DATABASE),
        _check("inspection_role_identity", inspection.get("role") == EXPECTED_INSPECTION_ROLE, inspection.get("role"), EXPECTED_INSPECTION_ROLE),
        _check("inspection_role_separate", inspection.get("role") != application.get("role"), inspection.get("role") != application.get("role"), True),
        _check("inspection_legacy_private_select_denied", inspection.get("legacy_private_select") is False, inspection.get("legacy_private_select"), False),
        _check("inspection_legacy_private_write_denied", inspection.get("legacy_private_write") is False, inspection.get("legacy_private_write"), False),
        _check("inspection_retirement_evidence_execute_allowed", inspection.get("retirement_evidence_execute") is True, inspection.get("retirement_evidence_execute"), True),
        _check("inspection_direct_evidence_select_denied", inspection.get("direct_evidence_select") is False, inspection.get("direct_evidence_select"), False),
        _check("inspection_direct_evidence_write_denied", inspection.get("direct_evidence_write") is False, inspection.get("direct_evidence_write"), False),
        _check("legacy_memory_schema_present", memory_schema_present, memory_schema_present, True),
        _check("legacy_memory_ingest_schema_present", ingest_schema_present, ingest_schema_present, True),
        _check("legacy_memory_table_shape", int(database.get("memory_table_count", -1)) == 160, int(database.get("memory_table_count", -1)), 160),
        _check("legacy_memory_ingest_table_shape", int(database.get("memory_ingest_table_count", -1)) == 7, int(database.get("memory_ingest_table_count", -1)), 7),
        _check("legacy_attestation_source_shape", int(database.get("attestation_source_rows", -1)) == 190, int(database.get("attestation_source_rows", -1)), 190),
        _check("legacy_attestation_eligible_shape", int(database.get("attestation_eligible_rows", -1)) == 32, int(database.get("attestation_eligible_rows", -1)), 32),
        _check("legacy_attestation_quarantine_shape", int(database.get("attestation_quarantine_rows", -1)) == 158, int(database.get("attestation_quarantine_rows", -1)), 158),
        _check("legacy_attestation_reconciled", int(database.get("attestation_reconciled_rows", -1)) == 32, int(database.get("attestation_reconciled_rows", -1)), 32),
        _check("external_legacy_function_references_absent", int(database.get("external_legacy_function_refs", -1)) == 0, int(database.get("external_legacy_function_refs", -1)), 0),
        _check("external_legacy_view_references_absent", int(database.get("external_legacy_view_refs", -1)) == 0, int(database.get("external_legacy_view_refs", -1)), 0),
        _check("external_legacy_materialized_view_references_absent", int(database.get("external_legacy_materialized_view_refs", -1)) == 0, int(database.get("external_legacy_materialized_view_refs", -1)), 0),
        _check("external_legacy_trigger_references_absent", int(database.get("external_legacy_trigger_refs", -1)) == 0, int(database.get("external_legacy_trigger_refs", -1)), 0),
        _check("external_legacy_foreign_keys_absent", int(database.get("external_legacy_foreign_keys", -1)) == 0, int(database.get("external_legacy_foreign_keys", -1)), 0),
        _check("external_legacy_catalog_dependencies_absent", int(database.get("external_legacy_catalog_dependencies", -1)) == 0, int(database.get("external_legacy_catalog_dependencies", -1)), 0),
        _check("backup_sha256_exact", backup_sha256 == EXPECTED_BACKUP_SHA256, backup_sha256, EXPECTED_BACKUP_SHA256),
        _check("restore_receipt_sha256_exact", restore_receipt_sha256 == EXPECTED_RESTORE_RECEIPT_SHA256, restore_receipt_sha256, EXPECTED_RESTORE_RECEIPT_SHA256),
        _check("attestation_receipt_sha256_bound", _valid_hash(attestation_receipt_sha256), bool(_valid_hash(attestation_receipt_sha256)), True),
        _check("quarantine_ciphertext_sha256_bound", _valid_hash(quarantine_ciphertext_sha256), bool(_valid_hash(quarantine_ciphertext_sha256)), True),
        _check("key_custody_receipt_sha256_bound", _valid_hash(key_custody_receipt_sha256), bool(_valid_hash(key_custody_receipt_sha256)), True),
        _check("dependency_catalog_sha256_bound", _valid_hash(dependency_catalog_sha256), bool(_valid_hash(dependency_catalog_sha256)), True),
    ]
    retirement_ready = not schemas_already_retired and all(item["status"] == "pass" for item in retirement_checks)
    deployment_gaps = [item["name"] for item in deployment_checks if item["status"] != "pass"]
    retirement_gaps = [] if schemas_already_retired else [item["name"] for item in retirement_checks if item["status"] != "pass"]
    return {
        "schema_version": SCHEMA_VERSION, "status": "pass" if candidate_deploy_ready else "fail",
        "candidate_deploy_ready": candidate_deploy_ready,
        "legacy_schema_state": "retired" if schemas_already_retired else "present",
        "legacy_schema_retirement_ready": retirement_ready,
        "deployment_checks": deployment_checks, "retirement_checks": retirement_checks,
        "deployment_gaps": deployment_gaps, "retirement_gaps": retirement_gaps,
        "gaps": retirement_gaps if not schemas_already_retired else deployment_gaps,
        "dependencies": dependencies,
    }


async def _scalar(connection: Any, sql: str, *args: Any) -> Any:
    return await connection.fetchval(sql, *args)


async def collect_role_boundary_read_only(connection: Any) -> dict[str, Any]:
    async with connection.transaction(isolation="serializable", readonly=True):
        if await _scalar(connection, "SHOW transaction_read_only") != "on":
            raise PreflightOperationalError("database_transaction_not_read_only")
        identity = await connection.fetchrow("SELECT current_database() AS database, current_user AS role")
        private_select = bool(await _scalar(connection, "SELECT CASE WHEN to_regclass('memory_ingest_private.memory_ingest_outbox') IS NULL OR to_regclass('memory_ingest_private.source_erasure_operation') IS NULL THEN false ELSE has_table_privilege(current_user, 'memory_ingest_private.memory_ingest_outbox', 'SELECT') OR has_any_column_privilege(current_user, 'memory_ingest_private.memory_ingest_outbox', 'SELECT') OR has_table_privilege(current_user, 'memory_ingest_private.source_erasure_operation', 'SELECT') OR has_any_column_privilege(current_user, 'memory_ingest_private.source_erasure_operation', 'SELECT') END"))
        private_write = bool(await _scalar(connection, "SELECT CASE WHEN to_regclass('memory_ingest_private.memory_ingest_outbox') IS NULL OR to_regclass('memory_ingest_private.source_erasure_operation') IS NULL THEN false ELSE has_table_privilege(current_user, 'memory_ingest_private.memory_ingest_outbox', 'INSERT,UPDATE,DELETE,TRUNCATE') OR has_any_column_privilege(current_user, 'memory_ingest_private.memory_ingest_outbox', 'INSERT,UPDATE') OR has_table_privilege(current_user, 'memory_ingest_private.source_erasure_operation', 'INSERT,UPDATE,DELETE,TRUNCATE') OR has_any_column_privilege(current_user, 'memory_ingest_private.source_erasure_operation', 'INSERT,UPDATE') END"))
        evidence_execute = bool(await _scalar(connection, "SELECT CASE WHEN to_regprocedure('memory.legacy_retirement_evidence_v1()') IS NULL THEN false ELSE has_function_privilege(current_user, 'memory.legacy_retirement_evidence_v1()', 'EXECUTE') END"))
        direct_evidence_select = bool(await _scalar(connection, "SELECT EXISTS (SELECT 1 FROM unnest(ARRAY['memory.assistant_transcript_attestation_v1','public.chat_log','chat_integrity.assistant_transcript_attestation_v1','memory_ingest_private.memory_ingest_outbox','memory_ingest_private.source_erasure_operation']) AS relation(name) WHERE to_regclass(relation.name) IS NOT NULL AND (has_table_privilege(current_user, relation.name, 'SELECT') OR has_any_column_privilege(current_user, relation.name, 'SELECT')))"))
        direct_evidence_write = bool(await _scalar(connection, "SELECT EXISTS (SELECT 1 FROM unnest(ARRAY['memory.assistant_transcript_attestation_v1','public.chat_log','chat_integrity.assistant_transcript_attestation_v1','memory_ingest_private.memory_ingest_outbox','memory_ingest_private.source_erasure_operation']) AS relation(name) WHERE to_regclass(relation.name) IS NOT NULL AND (has_table_privilege(current_user, relation.name, 'INSERT,UPDATE,DELETE,TRUNCATE') OR has_any_column_privilege(current_user, relation.name, 'INSERT,UPDATE')))"))
        return {
            "database": identity["database"], "role": identity["role"],
            "legacy_private_select": private_select, "legacy_private_write": private_write,
            "retirement_evidence_execute": evidence_execute,
            "direct_evidence_select": direct_evidence_select,
            "direct_evidence_write": direct_evidence_write,
            "transaction_read_only": True,
        }


async def collect_database_state(connection: Any) -> dict[str, Any]:
    scalar = lambda sql, *args: _scalar(connection, sql, *args)
    outbox = await scalar("SELECT to_regclass('conversation_sync_private.zep_turn_outbox') IS NOT NULL")
    history_oid = await scalar("SELECT to_regprocedure($1::text)::oid", HISTORY_SIGNATURE)
    tail_oid = await scalar("SELECT to_regprocedure($1::text)::oid", TAIL_SIGNATURE)
    memory_schema = await scalar("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'memory')")
    ingest_schema = await scalar("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'memory_ingest_private')")
    ingest_outbox = await scalar("SELECT to_regclass('memory_ingest_private.memory_ingest_outbox') IS NOT NULL")
    erasure_table = await scalar("SELECT to_regclass('memory_ingest_private.source_erasure_operation') IS NOT NULL")
    state: dict[str, Any] = {
        "zep_turn_outbox_present": bool(outbox),
        "clear_history_present": history_oid is not None, "clear_message_tail_present": tail_oid is not None,
        "clear_history_definition": await scalar("SELECT pg_get_functiondef($1::oid)", history_oid) if history_oid else "",
        "clear_message_tail_definition": await scalar("SELECT pg_get_functiondef($1::oid)", tail_oid) if tail_oid else "",
        "legacy_trigger_count": await scalar("SELECT count(*)::integer FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY($1::text[])", list(LEGACY_TRIGGER_NAMES)),
        "telemetry_retention_present": bool(await scalar("SELECT to_regprocedure('ai_operations.enforce_telemetry_retention_v1()') IS NOT NULL")),
        "memory_schema_present": bool(memory_schema), "memory_ingest_schema_present": bool(ingest_schema),
        "memory_table_count": await scalar("SELECT count(*)::integer FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'memory' AND c.relkind = 'r'"),
        "memory_ingest_table_count": await scalar("SELECT count(*)::integer FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'memory_ingest_private' AND c.relkind = 'r'"),
        "external_legacy_function_refs": await scalar("SELECT count(*)::integer FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prokind IN ('f','p') AND n.nspname NOT IN ('memory','memory_ingest_private') AND (pg_get_functiondef(p.oid) ILIKE '%memory.%' OR pg_get_functiondef(p.oid) ILIKE '%memory_ingest_private.%')"),
        "external_legacy_view_refs": await scalar("SELECT count(*)::integer FROM pg_views WHERE schemaname NOT IN ('memory','memory_ingest_private') AND (definition ILIKE '%memory.%' OR definition ILIKE '%memory_ingest_private.%')"),
        "external_legacy_materialized_view_refs": await scalar("SELECT count(*)::integer FROM pg_matviews WHERE schemaname NOT IN ('memory','memory_ingest_private') AND (definition ILIKE '%memory.%' OR definition ILIKE '%memory_ingest_private.%')"),
        "external_legacy_trigger_refs": await scalar("SELECT count(*)::integer FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname NOT IN ('memory','memory_ingest_private') AND (pg_get_triggerdef(t.oid) ILIKE '%memory.%' OR pg_get_triggerdef(t.oid) ILIKE '%memory_ingest_private.%')"),
        "external_legacy_foreign_keys": await scalar("SELECT count(*)::integer FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class r ON r.oid=k.confrelid JOIN pg_namespace rn ON rn.oid=r.relnamespace WHERE k.contype='f' AND ((n.nspname IN ('memory','memory_ingest_private') AND rn.nspname NOT IN ('memory','memory_ingest_private')) OR (rn.nspname IN ('memory','memory_ingest_private') AND n.nspname NOT IN ('memory','memory_ingest_private')))"),
        "external_legacy_catalog_dependencies": await scalar("SELECT count(*)::integer FROM pg_depend d CROSS JOIN LATERAL pg_identify_object(d.refclassid,d.refobjid,d.refobjsubid) ref CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) dep WHERE ref.schema IN ('memory','memory_ingest_private') AND dep.schema IS NOT NULL AND dep.schema NOT IN ('memory','memory_ingest_private') AND d.deptype IN ('n','a')"),
        "nonterminal_ingest": 0, "nonterminal_erasure": 0,
        "attestation_source_rows": 0, "attestation_eligible_rows": 0,
        "attestation_quarantine_rows": 0, "attestation_reconciled_rows": 0,
    }
    evidence_function = await scalar("SELECT to_regprocedure('memory.legacy_retirement_evidence_v1()')") if memory_schema and ingest_outbox and erasure_table else None
    if evidence_function is not None:
        evidence = await connection.fetchrow("SELECT source_rows, eligible_rows, quarantine_rows, reconciled_rows, nonterminal_ingest, nonterminal_erasure FROM memory.legacy_retirement_evidence_v1()")
        state.update(
            attestation_source_rows=int(evidence["source_rows"]),
            attestation_eligible_rows=int(evidence["eligible_rows"]),
            attestation_quarantine_rows=int(evidence["quarantine_rows"]),
            attestation_reconciled_rows=int(evidence["reconciled_rows"]),
            nonterminal_ingest=int(evidence["nonterminal_ingest"]),
            nonterminal_erasure=int(evidence["nonterminal_erasure"]),
        )
    return state


async def collect_database_state_read_only(connection: Any) -> dict[str, Any]:
    async with connection.transaction(isolation="serializable", readonly=True):
        if await connection.fetchval("SHOW transaction_read_only") != "on":
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
    application_dsn = (os.getenv(arguments.dsn_env) or "").strip()
    inspection_dsn = (os.getenv(arguments.inspection_dsn_env) or "").strip()
    if not application_dsn:
        raise PreflightOperationalError(f"{arguments.dsn_env}_missing")
    if not inspection_dsn:
        raise PreflightOperationalError(f"{arguments.inspection_dsn_env}_missing")
    if application_dsn == inspection_dsn:
        raise PreflightOperationalError("application_and_inspection_dsn_must_differ")
    python_version, pins = load_contract(arguments.pyproject)
    dependencies = verify_contract(python_version, pins)
    application_connection = await asyncpg.connect(application_dsn, command_timeout=20)
    try:
        application = await collect_role_boundary_read_only(application_connection)
    finally:
        await application_connection.close()
    inspection_connection = await asyncpg.connect(inspection_dsn, command_timeout=20)
    try:
        inspection = await collect_role_boundary_read_only(inspection_connection)
        database = await collect_database_state_read_only(inspection_connection)
    finally:
        await inspection_connection.close()
    return evaluate_readiness(
        database, crontab_text=read_crontab(arguments.crontab_user), dependencies=dependencies,
        application_boundary=application, inspection_boundary=inspection,
        backup_sha256=arguments.backup_sha256, restore_receipt_sha256=arguments.restore_receipt_sha256,
        attestation_receipt_sha256=arguments.attestation_receipt_sha256,
        quarantine_ciphertext_sha256=arguments.quarantine_ciphertext_sha256,
        key_custody_receipt_sha256=arguments.key_custody_receipt_sha256,
        dependency_catalog_sha256=arguments.dependency_catalog_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="POSTGRES_DSN")
    parser.add_argument("--inspection-dsn-env", default="LEGACY_MEMORY_INSPECTION_DSN")
    parser.add_argument("--crontab-user")
    parser.add_argument("--pyproject", type=Path, default=Path(__file__).resolve().parents[1] / "pyproject.toml")
    parser.add_argument("--backup-sha256")
    parser.add_argument("--restore-receipt-sha256")
    parser.add_argument("--attestation-receipt-sha256")
    parser.add_argument("--quarantine-ciphertext-sha256")
    parser.add_argument("--key-custody-receipt-sha256")
    parser.add_argument("--dependency-catalog-sha256")
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
