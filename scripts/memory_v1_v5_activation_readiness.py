#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "memory_v1_v5_activation_readiness_v5"
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
RETRIEVABLE_STATUSES = frozenset({"supported", "uncertain", "disputed"})
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
MAX_QDRANT_READINESS_POINTS = 100_000
ACTIVE_COLLECTION_ALIAS = "memory_claim_v1_active"
EFFECTIVE_MEMORY_SETTINGS = (
    "MEMORY_V1_COLLECTION",
    "MEMORY_V1_V5_SHADOW",
    "MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE",
    "MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED",
    "MEMORY_V1_V5_SHADOW_USER_IDS",
)


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
        "--service-env-file", default="/etc/verbalsage/brains.env"
    )
    parser.add_argument("--service-name", default="brains.service")
    parser.add_argument(
        "--qdrant-collection",
        default=None,
    )
    parser.add_argument(
        "--qdrant-scroll-url",
        default=None,
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
), retrievable_claims AS (
  SELECT coalesce(jsonb_agg(jsonb_build_object(
           'claim_id',claim_id,
           'owner_user_id',owner_user_id,
           'revision_number',revision_number,
           'status',status
         ) ORDER BY owner_user_id,claim_id),'[]'::jsonb) value
    FROM (
      SELECT claim.claim_id,claim.owner_user_id,claim.status::text AS status,
             COALESCE(max(revision.revision_number),0) AS revision_number
        FROM memory.claim AS claim
        LEFT JOIN memory.claim_revision AS revision
          ON revision.owner_user_id=claim.owner_user_id
         AND revision.claim_id=claim.claim_id
       WHERE claim.status::text IN ('supported','uncertain','disputed')
       GROUP BY claim.owner_user_id,claim.claim_id
    ) AS current_claim
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
  'retrievable_claims',(SELECT value FROM retrievable_claims)
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
    suffix = "/points/scroll"
    if not url.endswith(suffix):
        raise RuntimeError("Qdrant scroll URL is invalid")
    collection_url = url[: -len(suffix)]
    with urllib.request.urlopen(collection_url, timeout=30) as response:
        collection_payload = json.load(response)
    result = collection_payload.get("result", {})
    vectors = result.get("config", {}).get("params", {}).get("vectors")
    if not isinstance(vectors, dict):
        raise RuntimeError("Qdrant vector configuration is invalid")
    dimensions = vectors.get("size")
    distance = str(vectors.get("distance") or "").strip().lower()
    reported_count = result.get("points_count")
    if (
        type(dimensions) is not int
        or dimensions < 1
        or distance not in {"cosine", "dot"}
        or type(reported_count) is not int
        or reported_count < 0
        or reported_count > MAX_QDRANT_READINESS_POINTS
    ):
        raise RuntimeError("Qdrant collection configuration is invalid")

    points: list[Any] = []
    offset: Any = None
    seen_offsets: set[str] = set()
    while True:
        body_value: dict[str, Any] = {
            "limit": 10000,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body_value["offset"] = offset
        request = urllib.request.Request(
            url,
            data=json.dumps(body_value).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        page = payload.get("result", {})
        page_points = page.get("points")
        if not isinstance(page_points, list):
            raise RuntimeError("Qdrant scroll returned an invalid payload")
        points.extend(page_points)
        if len(points) > MAX_QDRANT_READINESS_POINTS:
            raise RuntimeError("Qdrant readiness point bound exceeded")
        next_offset = page.get("next_page_offset")
        if next_offset is None:
            break
        marker = _stable_json(next_offset)
        if marker in seen_offsets:
            raise RuntimeError("Qdrant scroll offset repeated")
        seen_offsets.add(marker)
        offset = next_offset
    if len(points) != reported_count:
        raise RuntimeError("Qdrant readiness inventory was truncated")
    values: list[dict[str, Any]] = []
    for point in points:
        point_payload = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(point_payload, dict):
            values.append(
                {
                    "claim_id": None,
                    "owner_user_id": None,
                    "point_id": None,
                    "revision_number": None,
                    "schema_version": None,
                    "status": None,
                }
            )
            continue
        values.append(
            {
                "claim_id": point_payload.get("claim_id"),
                "owner_user_id": point_payload.get("owner_user_id")
                or point_payload.get("user_id"),
                "point_id": point.get("id"),
                "revision_number": point_payload.get("revision_number"),
                "schema_version": point_payload.get("schema_version"),
                "status": point_payload.get("status"),
            }
        )
    return {
        "dimensions": dimensions,
        "distance": distance,
        "points": values,
        "reported_count": reported_count,
    }


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in EFFECTIVE_MEMORY_SETTINGS:
            values[key] = value.strip().strip("'\"")
    return values


def _env_key_occurrences(path: Path, key: str) -> int:
    if not path.is_file():
        return 0
    count = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.split("=", 1)[0].strip() == key:
            count += 1
    return count


def _validate_env_setting_occurrences(
    primary_path: Path, service_path: Path
) -> None:
    for key in EFFECTIVE_MEMORY_SETTINGS:
        if _env_key_occurrences(primary_path, key) != 1:
            raise RuntimeError(
                f"primary env must contain exactly one nonblank {key} setting"
            )
        service_occurrences = _env_key_occurrences(service_path, key)
        if service_occurrences not in {0, 1}:
            raise RuntimeError(
                f"later service env may contain at most one {key} setting"
            )


def _running_service_memory_settings(service_name: str) -> dict[str, str]:
    completed = subprocess.run(
        ["systemctl", "show", service_name, "--property=MainPID", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value.isdigit() or int(value) <= 0:
        raise RuntimeError("Brains service MainPID is unavailable")
    environment = Path(f"/proc/{value}/environ").read_bytes().split(b"\0")
    matches: dict[str, list[str]] = {key: [] for key in EFFECTIVE_MEMORY_SETTINGS}
    for item in environment:
        if b"=" not in item:
            continue
        raw_key, raw_value = item.split(b"=", 1)
        try:
            key = raw_key.decode("utf-8", "strict")
        except UnicodeDecodeError:
            continue
        if key not in matches:
            continue
        try:
            decoded = raw_value.decode("utf-8", "strict").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"running service {key} setting is invalid") from exc
        matches[key].append(decoded)
    resolved: dict[str, str] = {}
    for key, values in matches.items():
        if len(values) != 1 or not values[0]:
            raise RuntimeError(
                f"running service must expose exactly one nonblank {key} setting"
            )
        resolved[key] = values[0]
    return resolved


def _running_service_collection(service_name: str) -> str:
    """Compatibility wrapper; final readiness validates the complete mapping."""
    return _running_service_memory_settings(service_name)["MEMORY_V1_COLLECTION"]


def _resolve_effective_memory_settings(
    primary_env: dict[str, str],
    service_env: dict[str, str],
    process_env: dict[str, str] | os._Environ[str],
) -> dict[str, str]:
    effective: dict[str, str] = {}
    for key in EFFECTIVE_MEMORY_SETTINGS:
        primary = str(primary_env.get(key) or "").strip()
        running = str(process_env.get(key) or "").strip()
        if not primary:
            raise RuntimeError(f"primary {key} setting is missing or blank")
        if not running:
            raise RuntimeError(f"running service {key} setting is missing or blank")
        if primary != running:
            raise RuntimeError("memory settings disagree across runtime sources")
        if key in service_env:
            later = str(service_env.get(key) or "").strip()
            if not later or later != running:
                raise RuntimeError("memory settings disagree across runtime sources")
        effective[key] = running
    return effective


def _resolve_qdrant_target(
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    service_env: dict[str, str] | None = None,
    process_env: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str, str]:
    runtime_env = os.environ if process_env is None else process_env
    effective = _resolve_effective_memory_settings(
        env, service_env or {}, runtime_env
    )
    collection = effective["MEMORY_V1_COLLECTION"]
    explicit = str(args.qdrant_collection or "").strip()
    if explicit:
        if explicit != collection:
            raise RuntimeError("memory settings disagree across runtime sources")
    if collection != ACTIVE_COLLECTION_ALIAS:
        raise RuntimeError(
            "final readiness requires exact alias memory_claim_v1_active"
        )
    expected_scroll_url = (
        f"http://127.0.0.1:6333/collections/{collection}/points/scroll"
    )
    supplied_scroll_url = str(args.qdrant_scroll_url or "").strip()
    if supplied_scroll_url and supplied_scroll_url != expected_scroll_url:
        raise RuntimeError("Qdrant scroll URL differs from the selected collection")
    return collection, expected_scroll_url


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


def _require_stable_database_snapshot(
    opening: dict[str, Any], closing: dict[str, Any]
) -> None:
    if closing != opening:
        raise RuntimeError("PostgreSQL readiness snapshot changed during Qdrant scan")


def _projection_inventories(
    database: dict[str, Any], qdrant: dict[str, Any]
) -> dict[str, Any]:
    postgres_values = database.get("retrievable_claims", [])
    qdrant_values = qdrant.get("points", [])
    postgres_inventory: list[tuple[str, str, str, int]] = []
    qdrant_inventory: list[tuple[str, str, str, int]] = []
    postgres_complete = isinstance(postgres_values, list)
    distance = str(qdrant.get("distance") or "").strip().lower()
    dimensions = qdrant.get("dimensions")
    metric_schema_versions = {
        "cosine": frozenset({"memory_claim_projection_v1"}),
        "dot": frozenset(
            {"memory_claim_projection_v2", "memory_claim_projection_v3"}
        ),
    }
    allowed_schema_versions = metric_schema_versions.get(distance, frozenset())
    qdrant_complete = (
        isinstance(qdrant_values, list)
        and dimensions == 3072
        and bool(allowed_schema_versions)
    )

    for item in postgres_values if isinstance(postgres_values, list) else []:
        try:
            claim_id = str(item["claim_id"])
            owner_user_id = str(item["owner_user_id"])
            status = str(item["status"]).strip().lower()
            revision_number = item["revision_number"]
            uuid.UUID(claim_id)
            uuid.UUID(owner_user_id)
            if (
                status not in RETRIEVABLE_STATUSES
                or type(revision_number) is not int
                or revision_number < 0
            ):
                raise ValueError("PostgreSQL projection metadata rejected")
        except (KeyError, TypeError, ValueError, AttributeError):
            postgres_complete = False
            continue
        postgres_inventory.append(
            (claim_id, owner_user_id, status, revision_number)
        )

    for item in qdrant_values if isinstance(qdrant_values, list) else []:
        try:
            point_id = str(item["point_id"])
            claim_id = str(item["claim_id"])
            owner_user_id = str(item["owner_user_id"])
            status = str(item["status"]).strip().lower()
            revision_number = item["revision_number"]
            schema_version = str(item["schema_version"])
            uuid.UUID(point_id)
            uuid.UUID(claim_id)
            uuid.UUID(owner_user_id)
            if (
                point_id != claim_id
                or status not in RETRIEVABLE_STATUSES
                or schema_version not in allowed_schema_versions
                or type(revision_number) is not int
                or revision_number < 0
            ):
                raise ValueError("Qdrant projection metadata rejected")
        except (KeyError, TypeError, ValueError, AttributeError):
            qdrant_complete = False
            continue
        qdrant_inventory.append(
            (claim_id, owner_user_id, status, revision_number)
        )

    postgres_projection = set(postgres_inventory)
    qdrant_projection = set(qdrant_inventory)
    postgres_unique = len(postgres_projection) == len(postgres_inventory)
    qdrant_unique = len(qdrant_projection) == len(qdrant_inventory)
    return {
        "postgres_complete": postgres_complete,
        "postgres_projection": postgres_projection,
        "postgres_unique": postgres_unique,
        "qdrant_complete": qdrant_complete,
        "qdrant_projection": qdrant_projection,
        "qdrant_unique": qdrant_unique,
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

    projection = _projection_inventories(database, qdrant)
    postgres_projection = projection["postgres_projection"]
    qdrant_values = qdrant.get("points", [])
    qdrant_projection = projection["qdrant_projection"]
    qdrant_complete = projection["qdrant_complete"]
    qdrant_unique = projection["qdrant_unique"]

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
        "qdrant_exactly_projects_retrievable_postgres_claims": projection[
            "postgres_complete"
        ]
        and projection["postgres_unique"]
        and qdrant_projection == postgres_projection,
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
            "qdrant_exactly_projects_retrievable_postgres_claims",
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
            "postgres_retrievable_projection_count": len(postgres_projection),
            "qdrant_projection_count": len(qdrant_values),
            "projection_missing_from_qdrant": len(postgres_projection - qdrant_projection),
            "projection_unknown_to_postgres": len(qdrant_projection - postgres_projection),
        },
    }


def main() -> int:
    args = arguments()
    output_path = Path(args.output).resolve()
    env_path = Path(args.env_file).resolve()
    service_env_path = Path(args.service_env_file).resolve()
    _validate_env_setting_occurrences(env_path, service_env_path)
    env = _parse_env(env_path)
    service_env = _parse_env(service_env_path) if service_env_path.is_file() else {}
    process_env = _running_service_memory_settings(args.service_name)
    effective_env = _resolve_effective_memory_settings(
        env, service_env, process_env
    )
    collection, qdrant_scroll_url = _resolve_qdrant_target(
        args,
        env,
        service_env=service_env,
        process_env=process_env,
    )
    database = _database_snapshot(
        container=args.postgres_container,
        maintenance_role=args.maintenance_role,
        database=args.database,
    )
    qdrant = _qdrant_snapshot(qdrant_scroll_url)
    closing_database = _database_snapshot(
        container=args.postgres_container,
        maintenance_role=args.maintenance_role,
        database=args.database,
    )
    _require_stable_database_snapshot(database, closing_database)
    timers = _timer_inventory()
    tunnel_state = _service_state("memory-v1-v5-local-inference-tunnel.service")
    failed_units = _failed_units()
    git_state = _git_state()
    evaluation = evaluate(
        database,
        qdrant,
        effective_env,
        timers,
        tunnel_state,
        failed_units,
        git_state,
    )

    sanitized_database = dict(database)
    sanitized_database.pop("retrievable_claims", None)
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
            "collection": collection,
            "dimensions": qdrant.get("dimensions"),
            "distance": qdrant.get("distance"),
            "point_count": len(qdrant.get("points", [])),
        },
        "runtime": {
            "timers": timers,
            "private_inference_tunnel": tunnel_state,
            "failed_units": failed_units,
            "shadow_flags": {
                "enabled": effective_env.get("MEMORY_V1_V5_SHADOW"),
                "trace_persistence": effective_env.get(
                    "MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE"
                ),
                "all_authenticated": effective_env.get(
                    "MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED"
                ),
                "allowlist_count": len(
                    {
                        item.strip()
                        for item in effective_env.get(
                            "MEMORY_V1_V5_SHADOW_USER_IDS", ""
                        ).split(",")
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
