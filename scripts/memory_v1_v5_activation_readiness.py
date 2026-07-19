#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "memory_v1_v5_activation_readiness_v4"
REGISTRY_VERSION = "memory_predicate_registry_v5"
REGISTRY_SHA256 = "4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4"
COMPILER_SHA256 = "5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2"

PROTECTED_DURABLE_TABLES = {
    "entity",
    "entity_alias",
    "candidate",
    "claim",
    "claim_revision",
    "claim_evidence",
    "claim_assessment",
    "claim_relation",
}
COMPILER_FUNCTIONS = {
    "plan_owner_v5_local_entity_validation_v1",
    "register_owner_v5_local_entity_validation_v1",
    "plan_owner_v5_local_auto_stage_v1",
    "register_owner_v5_local_auto_stage_v1",
}
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
    "apply_claim_assessment_v5",
    "read_v5_shadow_claims",
    "record_v5_shadow_trace_v1",
    "read_v5_shadow_project_knowledge",
    "record_v5_project_shadow_trace_v1",
    *COMPILER_FUNCTIONS,
}
RESTRICTED_ROLES = {
    "memory_v5_writer",
    "memory_v5_reader",
    "memory_v5_trace_writer",
    "memory_v5_extraction_maintainer",
    "memory_v5_extraction_scheduler_maintainer",
    "memory_v5_local_disposition_maintainer",
    "memory_v5_local_entailment_maintainer",
    "memory_v5_local_entity_validation_maintainer",
    "memory_v5_local_inference_maintainer",
    "memory_v5_local_projection_maintainer",
    "memory_v5_local_review_reader",
}
TRACE_TABLES = {"v5_shadow_trace_event", "v5_project_shadow_trace_event"}
FORBIDDEN_TRACE_COLUMNS = {
    "query_text",
    "message_text",
    "prompt",
    "prompt_text",
    "system_prompt",
    "answer",
    "answer_text",
    "claim_text",
    "canonical_text",
    "evidence_text",
    "content",
    "raw_text",
}
EXPECTED_TIMER_STATES = {
    "memory-v1-consolidation.timer": ("disabled", "inactive"),
    "memory-v1-deferred-reconciliation-scan.timer": ("enabled", "active"),
    "memory-v1-evidence-intake-dispatcher.timer": ("enabled", "active"),
    "memory-v1-governance.timer": ("disabled", "inactive"),
    "memory-v1-projection.timer": ("enabled", "active"),
    "memory-v1-v5-chat-capture.timer": ("enabled", "active"),
    "memory-v1-v5-local-auto-resolution.timer": ("enabled", "active"),
    "memory-v1-v5-local-auto-stage.timer": ("enabled", "active"),
    "memory-v1-v5-local-claim-projection.timer": ("enabled", "active"),
    "memory-v1-v5-local-entailment.timer": ("enabled", "active"),
    "memory-v1-v5-local-entity-validation.timer": ("enabled", "active"),
    "memory-v1-v5-local-inference-health.timer": ("enabled", "active"),
    "memory-v1-v5-local-inference-scheduler.timer": ("enabled", "active"),
    "memory-v1-v5-local-packet-router.timer": ("enabled", "active"),
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only current-invariant gate for Memory V1 V5"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--postgres-container", default="brains-postgres-1")
    parser.add_argument("--maintenance-role", default="sage")
    parser.add_argument("--database", default="memory")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--qdrant-scroll-url",
        default="http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll",
    )
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


def _sql_text_array(values: set[str]) -> str:
    return "ARRAY[" + ",".join(
        "'" + value.replace("'", "''") + "'" for value in sorted(values)
    ) + "]::text[]"


def _database_snapshot(
    *, container: str, maintenance_role: str, database: str
) -> dict[str, Any]:
    protected = _sql_text_array(PROTECTED_DURABLE_TABLES)
    apis = _sql_text_array(REQUIRED_APIS)
    roles = _sql_text_array(RESTRICTED_ROLES)
    compiler_functions = _sql_text_array(COMPILER_FUNCTIONS)
    trace_tables = _sql_text_array(TRACE_TABLES)
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH owner_tables AS (
  SELECT DISTINCT c.table_name,pc.relrowsecurity,pc.relforcerowsecurity
    FROM information_schema.columns AS c
    JOIN pg_class AS pc ON pc.relname=c.table_name
    JOIN pg_namespace AS pn
      ON pn.oid=pc.relnamespace AND pn.nspname=c.table_schema
   WHERE c.table_schema='memory' AND c.column_name='owner_user_id'
     AND pc.relkind='r'
), protected_grants AS (
  SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY table_name,privilege_type),'[]'::jsonb) value
    FROM (
      SELECT c.relname AS table_name, privilege_type
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        CROSS JOIN unnest(ARRAY[
          'INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
        ]::text[]) AS privilege_type
       WHERE n.nspname='memory' AND c.relname=ANY({protected})
         AND has_table_privilege(
           'brains_app',format('memory.%I',c.relname),privilege_type
         )
    ) AS g
), trace_relations AS (
  SELECT coalesce(jsonb_object_agg(c.relname,jsonb_build_object(
           'owner',pg_get_userbyid(c.relowner),
           'row_security',c.relrowsecurity,
           'force_row_security',c.relforcerowsecurity,
           'app_table_privileges',coalesce((
             SELECT jsonb_agg(privilege_type ORDER BY privilege_type)
               FROM unnest(ARRAY[
                 'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
               ]::text[]) AS privilege_type
              WHERE has_table_privilege(
                'brains_app',format('memory.%I',c.relname),privilege_type
              )
           ),'[]'::jsonb),
           'columns',coalesce((
             SELECT jsonb_agg(column_name ORDER BY ordinal_position)
               FROM information_schema.columns
              WHERE table_schema='memory' AND table_name=c.relname
           ),'[]'::jsonb),
           'append_only_trigger',EXISTS(
             SELECT 1 FROM pg_trigger t
              WHERE t.tgrelid=c.oid AND NOT t.tgisinternal AND t.tgenabled<>'D'
                AND (pg_get_triggerdef(t.oid) ILIKE '%BEFORE UPDATE OR DELETE%'
                     OR pg_get_triggerdef(t.oid) ILIKE '%BEFORE DELETE OR UPDATE%')
           )
         ) ORDER BY c.relname),'{{}}'::jsonb) value
    FROM pg_class AS c JOIN pg_namespace AS n ON n.oid=c.relnamespace
   WHERE n.nspname='memory' AND c.relname=ANY({trace_tables})
), restricted_roles AS (
  SELECT coalesce(jsonb_object_agg(rolname,jsonb_build_object(
           'can_login',rolcanlogin,'inherit',rolinherit,'bypass_rls',rolbypassrls,
           'superuser',rolsuper,'create_db',rolcreatedb,'create_role',rolcreaterole
         ) ORDER BY rolname),'{{}}'::jsonb) value
    FROM pg_roles WHERE rolname=ANY({roles})
), controlled_apis AS (
  SELECT coalesce(jsonb_agg(routine_name ORDER BY routine_name),'[]'::jsonb) value
    FROM (
      SELECT DISTINCT routine_name
        FROM information_schema.routine_privileges
       WHERE grantee='brains_app' AND routine_schema='memory'
         AND privilege_type='EXECUTE' AND routine_name=ANY({apis})
    ) AS values
), compiler_gate AS (
  SELECT coalesce(jsonb_object_agg(p.proname,jsonb_build_object(
           'owner',pg_get_userbyid(p.proowner),
           'security_definer',p.prosecdef,
           'current_compiler',position('{COMPILER_SHA256}' in pg_get_functiondef(p.oid))>0
         ) ORDER BY p.proname),'{{}}'::jsonb) value
    FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid=p.pronamespace
   WHERE n.nspname='memory' AND p.proname=ANY({compiler_functions})
), supported_claims AS (
  SELECT coalesce(jsonb_agg(jsonb_build_object(
           'claim_id',claim_id,'owner_user_id',owner_user_id
         ) ORDER BY claim_id),'[]'::jsonb) value
    FROM memory.claim WHERE status='supported'
)
SELECT jsonb_build_object(
  'registry',(SELECT to_jsonb(r) FROM (
     SELECT registry_version,registry_sha256,status::text,runtime_active,
            (SELECT count(*) FROM memory.predicate_contract
              WHERE registry_version='{REGISTRY_VERSION}') contract_rows
       FROM memory.predicate_registry_version
      WHERE registry_version='{REGISTRY_VERSION}'
  ) AS r),
  'owner_tables',jsonb_build_object(
     'count',(SELECT count(*) FROM owner_tables),
     'not_forced',coalesce((SELECT jsonb_agg(table_name ORDER BY table_name)
       FROM owner_tables WHERE NOT relrowsecurity OR NOT relforcerowsecurity),'[]'::jsonb)
  ),
  'protected_durable_write_grants',(SELECT value FROM protected_grants),
  'trace_relations',(SELECT value FROM trace_relations),
  'restricted_roles',(SELECT value FROM restricted_roles),
  'controlled_apis',(SELECT value FROM controlled_apis),
  'compiler_gate',(SELECT value FROM compiler_gate),
  'trace_counts',jsonb_build_object(
     'claim_total',(SELECT count(*) FROM memory.v5_shadow_trace_event),
     'claim_owner_counts',(SELECT coalesce(jsonb_object_agg(owner_user_id::text,n),'{{}}'::jsonb)
        FROM (SELECT owner_user_id,count(*) n FROM memory.v5_shadow_trace_event GROUP BY owner_user_id) x),
     'claim_selected',(SELECT count(*) FROM memory.v5_shadow_trace_event WHERE selected_count>0),
     'claim_zero_influence',(SELECT count(*) FROM memory.v5_shadow_trace_event
       WHERE NOT prompt_injection AND NOT answer_model_exposure AND NOT retrieval_activation
         AND database_writes=0 AND qdrant_writes=0),
     'project_total',(SELECT count(*) FROM memory.v5_project_shadow_trace_event),
     'project_owner_counts',(SELECT coalesce(jsonb_object_agg(owner_user_id::text,n),'{{}}'::jsonb)
        FROM (SELECT owner_user_id,count(*) n FROM memory.v5_project_shadow_trace_event GROUP BY owner_user_id) x),
     'project_selected',(SELECT count(*) FROM memory.v5_project_shadow_trace_event WHERE selected_count>0),
     'project_zero_influence',(SELECT count(*) FROM memory.v5_project_shadow_trace_event
       WHERE NOT prompt_injection AND NOT answer_model_exposure AND NOT retrieval_activation
         AND database_writes=0 AND qdrant_writes=0 AND external_model_calls=0)
  ),
  'governed_counts',jsonb_build_object(
     'entities',(SELECT count(*) FROM memory.entity),
     'claims',(SELECT count(*) FROM memory.claim),
     'supported_claims',(SELECT count(*) FROM memory.claim WHERE status='supported'),
     'claim_owners',(SELECT count(DISTINCT owner_user_id) FROM memory.claim),
     'assessments',(SELECT count(*) FROM memory.claim_assessment),
     'evidence_links',(SELECT count(*) FROM memory.claim_evidence)
  ),
  'supported_claims',(SELECT value FROM supported_claims)
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
        raise RuntimeError(
            f"maintenance read-only snapshot failed: {completed.stderr.strip()}"
        )
    candidates = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if len(candidates) != 1:
        raise RuntimeError("maintenance read-only snapshot returned an invalid payload")
    return json.loads(candidates[0])


def _qdrant_snapshot(url: str) -> dict[str, Any]:
    body = json.dumps(
        {"limit": 10000, "with_payload": True, "with_vector": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    points = payload.get("result", {}).get("points")
    if not isinstance(points, list):
        raise RuntimeError("Qdrant scroll returned an invalid payload")
    values: list[dict[str, str | None]] = []
    for point in points:
        point_payload = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(point_payload, dict):
            values.append({"claim_id": None, "owner_user_id": None})
            continue
        values.append(
            {
                "claim_id": point_payload.get("claim_id"),
                "owner_user_id": point_payload.get("owner_user_id")
                or point_payload.get("user_id"),
            }
        )
    return {"points": values}


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("MEMORY_V1_V5_SHADOW"):
            values[key] = value.strip().strip("'\"")
    return values


def _timer_inventory() -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for unit in sorted(EXPECTED_TIMER_STATES):
        enabled = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True
        )
        active = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True
        )
        values[unit] = {
            "enabled": enabled.stdout.strip() or "unknown",
            "active": active.stdout.strip() or "unknown",
        }
    return values


def _service_state(unit: str) -> str:
    completed = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True
    )
    return completed.stdout.strip() or "unknown"


def _failed_units() -> list[str]:
    completed = subprocess.run(
        ["systemctl", "--failed", "--no-legend", "--no-pager"],
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        return ["systemctl_query_failed"]
    return sorted(line.split()[0] for line in completed.stdout.splitlines() if line.split())


def _git_state() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return {"head": head, "clean": not status, "changed_path_count": len(status)}


def _hashed_owner_counts(values: dict[str, int]) -> dict[str, int]:
    return {
        _sha256_bytes(owner.encode("utf-8")): count
        for owner, count in sorted(values.items())
    }


def evaluate(
    database: dict[str, Any],
    qdrant: dict[str, Any],
    env: dict[str, str],
    timers: dict[str, dict[str, str]],
    tunnel_state: str,
    failed_units: list[str],
    git_state: dict[str, Any],
) -> dict[str, Any]:
    registry = database.get("registry") or {}
    trace_relations = database.get("trace_relations") or {}
    roles = database.get("restricted_roles") or {}
    compiler = database.get("compiler_gate") or {}
    trace_counts = database.get("trace_counts") or {}

    postgres_projection = {
        (str(item["claim_id"]), str(item["owner_user_id"]))
        for item in database.get("supported_claims", [])
    }
    qdrant_values = qdrant.get("points", [])
    qdrant_projection = {
        (str(item.get("claim_id")), str(item.get("owner_user_id")))
        for item in qdrant_values
        if item.get("claim_id") and item.get("owner_user_id")
    }
    qdrant_complete = len(qdrant_projection) == len(qdrant_values)
    qdrant_unique = len(qdrant_projection) == len(qdrant_values)

    allowlist_raw = env.get("MEMORY_V1_V5_SHADOW_USER_IDS", "")
    allowlist = {item.strip() for item in allowlist_raw.split(",") if item.strip()}
    valid_allowlist = bool(allowlist)
    try:
        for item in allowlist:
            uuid.UUID(item)
    except ValueError:
        valid_allowlist = False
    claim_trace_owners = set(trace_counts.get("claim_owner_counts", {}))
    project_trace_owners = set(trace_counts.get("project_owner_counts", {}))

    trace_schema_sanitized = set(trace_relations) == TRACE_TABLES and all(
        not (set(value.get("columns", [])) & FORBIDDEN_TRACE_COLUMNS)
        for value in trace_relations.values()
    )
    trace_storage_restricted = set(trace_relations) == TRACE_TABLES and all(
        value.get("owner") == "memory_v5_trace_writer"
        and value.get("row_security") is True
        and value.get("force_row_security") is True
        and not value.get("app_table_privileges")
        and value.get("append_only_trigger") is True
        for value in trace_relations.values()
    )
    roles_restricted = set(roles) == RESTRICTED_ROLES and all(
        value.get("can_login") is False
        and value.get("inherit") is False
        and value.get("bypass_rls") is False
        and value.get("superuser") is False
        and value.get("create_db") is False
        and value.get("create_role") is False
        for value in roles.values()
    )
    compiler_current = set(compiler) == COMPILER_FUNCTIONS and all(
        value.get("current_compiler") is True
        and value.get("security_definer") is True
        and value.get("owner")
        in {"memory_v5_local_entity_validation_maintainer", "memory_v5_local_disposition_maintainer"}
        for value in compiler.values()
    )
    timers_exact = set(timers) == set(EXPECTED_TIMER_STATES) and all(
        (value.get("enabled"), value.get("active")) == EXPECTED_TIMER_STATES[unit]
        for unit, value in timers.items()
    )
    shadow_rows_zero_influence = (
        int(trace_counts.get("claim_total", -1)) > 0
        and trace_counts.get("claim_total") == trace_counts.get("claim_zero_influence")
        and int(trace_counts.get("project_total", -1)) > 0
        and trace_counts.get("project_total") == trace_counts.get("project_zero_influence")
    )

    checks = {
        "git_checkout_clean": git_state.get("clean") is True,
        "predicate_registry_exact_and_inactive": registry.get("registry_version") == REGISTRY_VERSION
        and registry.get("registry_sha256") == REGISTRY_SHA256
        and registry.get("status") == "proposed"
        and registry.get("runtime_active") is False
        and registry.get("contract_rows") == 44,
        "all_owner_tables_force_rls": int(database.get("owner_tables", {}).get("count", 0)) > 0
        and not database.get("owner_tables", {}).get("not_forced"),
        "protected_durable_tables_have_no_direct_app_writes": not database.get(
            "protected_durable_write_grants"
        ),
        "restricted_roles_least_privilege": roles_restricted,
        "controlled_apis_available": set(database.get("controlled_apis", [])) == REQUIRED_APIS,
        "current_compiler_gate_installed": compiler_current,
        "trace_schema_contains_no_prose": trace_schema_sanitized,
        "trace_storage_forced_rls_append_only": trace_storage_restricted,
        "qdrant_payloads_are_owner_scoped_and_unique": qdrant_complete and qdrant_unique,
        "qdrant_exactly_projects_supported_postgres_claims": qdrant_projection == postgres_projection,
        "shadow_feature_is_allowlisted_only": env.get("MEMORY_V1_V5_SHADOW") == "1"
        and env.get("MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE") == "1"
        and env.get("MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED") == "0"
        and valid_allowlist,
        "every_allowlisted_owner_has_claim_traces": valid_allowlist and allowlist <= claim_trace_owners,
        "project_trace_owners_are_allowlisted": project_trace_owners <= allowlist,
        "all_persisted_shadow_rows_assert_zero_influence": shadow_rows_zero_influence,
        "automation_timer_contract_exact": timers_exact,
        "private_inference_tunnel_active": tunnel_state == "active",
        "no_failed_systemd_units": not failed_units,
    }

    database_boundary = all(
        checks[key]
        for key in (
            "predicate_registry_exact_and_inactive",
            "all_owner_tables_force_rls",
            "protected_durable_tables_have_no_direct_app_writes",
            "restricted_roles_least_privilege",
            "controlled_apis_available",
            "trace_schema_contains_no_prose",
            "trace_storage_forced_rls_append_only",
        )
    )
    automation_current = all(
        checks[key]
        for key in (
            "git_checkout_clean",
            "current_compiler_gate_installed",
            "automation_timer_contract_exact",
            "private_inference_tunnel_active",
            "no_failed_systemd_units",
        )
    )
    projection_consistent = all(
        checks[key]
        for key in (
            "qdrant_payloads_are_owner_scoped_and_unique",
            "qdrant_exactly_projects_supported_postgres_claims",
        )
    )
    allowlisted_shadow_observed = all(
        checks[key]
        for key in (
            "shadow_feature_is_allowlisted_only",
            "every_allowlisted_owner_has_claim_traces",
            "project_trace_owners_are_allowlisted",
            "all_persisted_shadow_rows_assert_zero_influence",
        )
    )
    ready_for_multi_owner_shadow = (
        database_boundary
        and automation_current
        and projection_consistent
        and allowlisted_shadow_observed
    )
    return {
        "checks": checks,
        "readiness": {
            "database_boundary": database_boundary,
            "automation_current": automation_current,
            "projection_consistent": projection_consistent,
            "allowlisted_zero_influence_shadow_observed": allowlisted_shadow_observed,
            "ready_for_multi_owner_zero_influence_shadow": ready_for_multi_owner_shadow,
            "prompt_influence": False,
            "general_account_activation": False,
        },
        "blockers": {
            "ready_for_multi_owner_zero_influence_shadow": [
                key for key, passed in checks.items() if not passed
            ],
            "prompt_influence": [
                "predicate_registry_runtime_inactive",
                "separate_bounded_prompt_cutover_not_performed",
            ],
            "general_account_activation": [
                "multi_owner_zero_influence_shadow_not_yet_observed"
            ],
        },
        "diagnostics": {
            "owner_table_count": database.get("owner_tables", {}).get("count", 0),
            "governed_counts": database.get("governed_counts", {}),
            "trace_counts": {
                "claim_total": trace_counts.get("claim_total", 0),
                "claim_selected": trace_counts.get("claim_selected", 0),
                "claim_owner_counts_sha256": _hashed_owner_counts(
                    trace_counts.get("claim_owner_counts", {})
                ),
                "project_total": trace_counts.get("project_total", 0),
                "project_selected": trace_counts.get("project_selected", 0),
                "project_owner_counts_sha256": _hashed_owner_counts(
                    trace_counts.get("project_owner_counts", {})
                ),
            },
            "shadow_allowlist_owner_sha256": sorted(
                _sha256_bytes(item.encode("utf-8")) for item in allowlist
            ),
            "postgres_supported_projection_count": len(postgres_projection),
            "qdrant_projection_count": len(qdrant_values),
            "projection_missing_from_qdrant": len(postgres_projection - qdrant_projection),
            "projection_unknown_to_postgres": len(qdrant_projection - postgres_projection),
        },
    }


def main() -> int:
    args = arguments()
    output_path = Path(args.output).resolve()
    env_path = Path(args.env_file).resolve()
    database = _database_snapshot(
        container=args.postgres_container,
        maintenance_role=args.maintenance_role,
        database=args.database,
    )
    qdrant = _qdrant_snapshot(args.qdrant_scroll_url)
    env = _parse_env(env_path)
    timers = _timer_inventory()
    tunnel_state = _service_state("memory-v1-v5-local-inference-tunnel.service")
    failed_units = _failed_units()
    git_state = _git_state()
    evaluation = evaluate(
        database, qdrant, env, timers, tunnel_state, failed_units, git_state
    )

    sanitized_database = dict(database)
    sanitized_database.pop("supported_claims", None)
    trace_counts = dict(sanitized_database.get("trace_counts", {}))
    trace_counts["claim_owner_counts"] = _hashed_owner_counts(
        trace_counts.get("claim_owner_counts", {})
    )
    trace_counts["project_owner_counts"] = _hashed_owner_counts(
        trace_counts.get("project_owner_counts", {})
    )
    sanitized_database["trace_counts"] = trace_counts
    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": "seebx",
        "mode": "read_only_zero_write",
        "git": git_state,
        "database": sanitized_database,
        "qdrant": {
            "collection": "memory_claim_v1",
            "point_count": len(qdrant.get("points", [])),
        },
        "runtime": {
            "timers": timers,
            "private_inference_tunnel": tunnel_state,
            "failed_units": failed_units,
            "shadow_flags": {
                "enabled": env.get("MEMORY_V1_V5_SHADOW"),
                "trace_persistence": env.get("MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE"),
                "all_authenticated": env.get("MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED"),
                "allowlist_count": len(
                    {
                        item.strip()
                        for item in env.get("MEMORY_V1_V5_SHADOW_USER_IDS", "").split(",")
                        if item.strip()
                    }
                ),
            },
        },
        "evaluation": evaluation,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "prompt_influence": 0,
    }
    report_sha256 = _secure_write(output_path, report)
    print(
        _stable_json(
            {
                "version": VERSION,
                "output": str(output_path),
                "sha256": report_sha256,
                "readiness": evaluation["readiness"],
                "failed_checks": [
                    key for key, passed in evaluation["checks"].items() if not passed
                ],
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
                "prompt_influence": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
