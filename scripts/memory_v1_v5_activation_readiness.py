#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "memory_v1_v5_activation_readiness_v1"
REGISTRY_VERSION = "memory_predicate_registry_v5"
REQUIRED_APIS = {
    "stage_relational_packet_v5",
    "preflight_entity_resolution_review_v5",
    "review_entity_resolution_v5",
    "preflight_entity_resolution_apply_v5",
    "apply_entity_resolution_v5",
    "preflight_projection_review_v5",
    "review_projection_v5",
    "preflight_projection_apply_v5",
    "apply_projection_v5",
}
SHARED_DURABLE_TARGETS = {"entity", "claim", "claim_revision"}
WRITE_PRIVILEGES = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only production readiness gate for Memory V1 V5"
    )
    parser.add_argument(
        "--manifest",
        default="ops/manifests/memory_v1_projection_v5_production_install_plan_20260715.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--postgres-container", default="brains-postgres-1")
    parser.add_argument("--maintenance-role", default="sage")
    parser.add_argument("--database", default="memory")
    return parser.parse_args()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _table_names(manifest: dict[str, Any]) -> list[str]:
    values = manifest["postinstall_requirements"]["empty_tables"]
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.startswith("memory."):
            raise RuntimeError("manifest empty_tables contains an invalid relation")
        name = value.removeprefix("memory.")
        if not name.replace("_", "").isalnum():
            raise RuntimeError("manifest relation name is unsafe")
        names.append(name)
    if len(names) != len(set(names)):
        raise RuntimeError("manifest empty_tables contains duplicates")
    return names


def _sql_text_array(values: list[str]) -> str:
    return "ARRAY[" + ",".join("'" + value.replace("'", "''") + "'" for value in values) + "]::text[]"


def _snapshot(
    *,
    tables: list[str],
    expected_registry: dict[str, Any],
    container: str,
    maintenance_role: str,
    database: str,
) -> dict[str, Any]:
    table_array = _sql_text_array(tables)
    shared_array = _sql_text_array(sorted(SHARED_DURABLE_TARGETS))
    write_array = _sql_text_array(sorted(WRITE_PRIVILEGES))
    api_array = _sql_text_array(sorted(REQUIRED_APIS))
    count_pairs = ",".join(
        f"'{table}',(SELECT count(*) FROM memory.\"{table}\")" for table in tables
    )
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH registry AS (
  SELECT registry_version, registry_sha256, status::text, runtime_active,
         (SELECT count(*) FROM memory.predicate_contract
           WHERE registry_version='{REGISTRY_VERSION}') AS contract_rows
    FROM memory.predicate_registry_version
   WHERE registry_version='{REGISTRY_VERSION}'
), writer_role AS (
  SELECT rolname, rolcanlogin, rolinherit, rolbypassrls,
         rolsuper, rolcreatedb, rolcreaterole
    FROM pg_roles WHERE rolname='memory_v5_writer'
), relations AS (
  SELECT coalesce(jsonb_object_agg(
           c.relname,
           jsonb_build_object(
             'relname', c.relname,
             'relrowsecurity', c.relrowsecurity,
             'relforcerowsecurity', c.relforcerowsecurity
           ) ORDER BY c.relname
         ), '{{}}'::jsonb) AS value
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid=c.relnamespace
   WHERE n.nspname='memory' AND c.relname=ANY({table_array})
), protected_grants AS (
  SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY table_name, privilege_type), '[]'::jsonb) AS value
    FROM (
      SELECT table_name, privilege_type
        FROM information_schema.role_table_grants
       WHERE grantee='brains_app' AND table_schema='memory'
         AND table_name=ANY({table_array})
    ) AS g
), shared_grants AS (
  SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY table_name, privilege_type), '[]'::jsonb) AS value
    FROM (
      SELECT table_name, privilege_type
        FROM information_schema.role_table_grants
       WHERE grantee='brains_app' AND table_schema='memory'
         AND table_name=ANY({shared_array})
         AND privilege_type=ANY({write_array})
    ) AS g
), controlled_apis AS (
  SELECT coalesce(jsonb_agg(routine_name ORDER BY routine_name), '[]'::jsonb) AS value
    FROM (
      SELECT DISTINCT routine_name
        FROM information_schema.routine_privileges
       WHERE grantee='brains_app' AND routine_schema='memory'
         AND privilege_type='EXECUTE' AND routine_name=ANY({api_array})
    ) AS api
)
SELECT jsonb_build_object(
  'registry', (SELECT to_jsonb(registry) FROM registry),
  'writer_role', (SELECT to_jsonb(writer_role) FROM writer_role),
  'relations', (SELECT value FROM relations),
  'exact_row_counts', jsonb_build_object({count_pairs}),
  'brains_app_protected_table_grants', (SELECT value FROM protected_grants),
  'brains_app_shared_durable_write_grants', (SELECT value FROM shared_grants),
  'brains_app_controlled_apis', (SELECT value FROM controlled_apis)
);
ROLLBACK;
"""
    completed = subprocess.run(
        [
            "docker", "exec", "-i", container,
            "psql", "-U", maintenance_role, "-d", database,
            "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
        ],
        input=sql,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"maintenance read-only snapshot failed: {completed.stderr.strip()}")
    candidates = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if len(candidates) != 1:
        raise RuntimeError("maintenance read-only snapshot returned an invalid payload")
    value = json.loads(candidates[0])
    return {
        "registry": value["registry"],
        "expected_registry": expected_registry,
        "writer_role": value["writer_role"],
        "relations": value["relations"],
        "exact_row_counts": value["exact_row_counts"],
        "brains_app_protected_table_grants": value["brains_app_protected_table_grants"],
        "brains_app_shared_durable_write_grants": value["brains_app_shared_durable_write_grants"],
        "brains_app_controlled_apis": value["brains_app_controlled_apis"],
    }


def evaluate(snapshot: dict[str, Any], tables: list[str]) -> dict[str, Any]:
    expected = snapshot["expected_registry"]
    registry = snapshot["registry"] or {}
    role = snapshot["writer_role"] or {}
    relations = snapshot["relations"]
    counts = snapshot["exact_row_counts"]

    checks = {
        "all_expected_relations_exist": set(relations) == set(tables),
        "all_owner_relations_force_rls": all(
            bool(value["relrowsecurity"]) and bool(value["relforcerowsecurity"])
            for value in relations.values()
        ) and set(relations) == set(tables),
        "all_v5_owner_relations_empty": set(counts) == set(tables)
        and all(value == 0 for value in counts.values()),
        "registry_exact_and_inactive": bool(registry)
        and registry.get("registry_version") == expected["registry_version"]
        and registry.get("registry_sha256") == expected["registry_sha256"]
        and registry.get("status") == expected["status"]
        and registry.get("runtime_active") is expected["runtime_active"]
        and int(registry.get("contract_rows", -1)) == expected["expected_contract_rows"],
        "writer_role_restricted": bool(role)
        and role.get("rolcanlogin") is False
        and role.get("rolinherit") is False
        and role.get("rolbypassrls") is False
        and role.get("rolsuper") is False
        and role.get("rolcreatedb") is False
        and role.get("rolcreaterole") is False,
        "brains_app_has_no_direct_v5_table_grants": not snapshot[
            "brains_app_protected_table_grants"
        ],
        "all_controlled_apis_available": set(snapshot["brains_app_controlled_apis"])
        == REQUIRED_APIS,
        "shared_durable_targets_have_no_direct_app_writes": not snapshot[
            "brains_app_shared_durable_write_grants"
        ],
    }
    schema_ready = all(
        checks[key]
        for key in (
            "all_expected_relations_exist",
            "all_owner_relations_force_rls",
            "registry_exact_and_inactive",
            "writer_role_restricted",
            "brains_app_has_no_direct_v5_table_grants",
            "all_controlled_apis_available",
        )
    )
    shadow_stage_ready = schema_ready and checks["all_v5_owner_relations_empty"]
    durable_apply_ready = (
        shadow_stage_ready
        and checks["shared_durable_targets_have_no_direct_app_writes"]
    )
    return {
        "checks": checks,
        "readiness": {
            "schema_installed_and_restricted": schema_ready,
            "manual_shadow_stage": shadow_stage_ready,
            "durable_apply": durable_apply_ready,
            "shadow_retrieval": False,
            "prompt_influence": False,
        },
        "blockers": {
            "manual_shadow_stage": []
            if shadow_stage_ready
            else [key for key, passed in checks.items() if not passed],
            "durable_apply": []
            if durable_apply_ready
            else [
                key
                for key in (
                    "shared_durable_targets_have_no_direct_app_writes",
                )
                if not checks[key]
            ],
            "shadow_retrieval": ["no_v5_shadow_retrieval_adapter_or_trace_gate"],
            "prompt_influence": [
                "v5_registry_runtime_inactive",
                "shadow_retrieval_and_cross_owner_trace_audit_not_complete",
            ],
        },
    }


def _timer_inventory() -> list[str]:
    completed = subprocess.run(
        ["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return sorted(
        line.strip()
        for line in completed.stdout.splitlines()
        if "memory-v1-" in line
    )


def main() -> int:
    args = arguments()
    manifest_path = Path(args.manifest).resolve()
    output_path = Path(args.output).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    tables = _table_names(manifest)
    expected_registry = manifest["postinstall_requirements"]["predicate_registry"]

    snapshot = _snapshot(
        tables=tables,
        expected_registry=expected_registry,
        container=args.postgres_container,
        maintenance_role=args.maintenance_role,
        database=args.database,
    )

    evaluation = evaluate(snapshot, tables)
    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": "seebx",
        "database": "memory",
        "mode": "read_only_zero_write",
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha256_bytes(manifest_bytes),
        },
        "snapshot": snapshot,
        "evaluation": evaluation,
        "active_memory_timers": _timer_inventory(),
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    report_sha256 = _secure_write(output_path, report)
    print(
        _stable_json(
            {
                "version": VERSION,
                "output": str(output_path),
                "sha256": report_sha256,
                "readiness": evaluation["readiness"],
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
