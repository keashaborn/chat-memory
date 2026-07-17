#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "memory_v1_v5_component_binding_selector_v1"
OPERATION_NAMESPACE = uuid.UUID("9af5dc06-3c05-4cb1-9f63-1aa2a1dc4f52")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
MAX_CANDIDATE_THREADS = 25
MAX_MATCHED_EVIDENCE = 100


class SelectorError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Zero-write selector for explicit registered-component thread bindings"
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--component-key", required=True)
    parser.add_argument("--expected-candidates", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--postgres-container", default="brains-postgres-1")
    parser.add_argument("--maintenance-role", default="sage")
    parser.add_argument("--database", default="memory")
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
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
    return hashlib.sha256(payload).hexdigest()


def validate_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SelectorError(f"{label} must be a UUID") from exc


def validate_key(value: str, label: str) -> str:
    if not KEY_RE.fullmatch(value) or "--" in value or value.endswith("-"):
        raise SelectorError(f"{label} is invalid")
    return value


def query_database(
    *,
    container: str,
    role: str,
    database: str,
    owner: uuid.UUID,
    project_key: str,
    component_key: str,
) -> dict[str, Any]:
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH registry AS (
  SELECT
    project.project_id,
    project.project_key,
    component.component_id,
    component.component_key,
    component.display_name
  FROM memory.project_space AS project
  JOIN memory.project_component_v5 AS component
    ON component.owner_user_id=project.owner_user_id
   AND component.project_id=project.project_id
  WHERE project.owner_user_id='{owner}'::uuid
    AND project.project_key='{project_key}'
    AND component.component_key='{component_key}'
), aliases AS (
  SELECT alias.normalized_alias
  FROM registry
  JOIN memory.project_component_alias_v5 AS alias
    ON alias.owner_user_id='{owner}'::uuid
   AND alias.project_id=registry.project_id
   AND alias.component_id=registry.component_id
), owner_pending AS (
  SELECT
    job.job_id,
    job.evidence_content_sha256,
    evidence.metadata->>'thread_id' AS thread_text,
    evidence.content
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=job.owner_user_id
   AND evidence.evidence_id=job.evidence_id
  WHERE job.owner_user_id='{owner}'::uuid
    AND job.route='relational_extraction'
    AND job.status='pending'
), typed_pending AS (
  SELECT
    owner_pending.*,
    CASE
      WHEN owner_pending.thread_text ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[1-5][0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'
      THEN owner_pending.thread_text::uuid
      ELSE NULL
    END AS thread_id
  FROM owner_pending
), valid_thread_pending AS (
  SELECT typed_pending.*
  FROM typed_pending
  JOIN public.threads AS thread
    ON thread.owner_user_id='{owner}'::uuid
   AND thread.id=typed_pending.thread_id
  WHERE typed_pending.thread_id IS NOT NULL
), exact_matches AS (
  SELECT DISTINCT
    pending.thread_id,
    pending.job_id,
    pending.evidence_content_sha256
  FROM valid_thread_pending AS pending
  WHERE EXISTS (
    SELECT 1
    FROM aliases AS alias
    WHERE lower(pending.content) ~ (
      '(^|[^a-z0-9])'
      || replace(alias.normalized_alias,'-','[^a-z0-9]+')
      || '([^a-z0-9]|$)'
    )
  )
), thread_totals AS (
  SELECT thread_id,count(*) AS pending_record_count
  FROM valid_thread_pending
  GROUP BY thread_id
), candidates AS (
  SELECT
    matched.thread_id,
    count(*) AS matched_record_count,
    array_agg(
      DISTINCT matched.evidence_content_sha256
      ORDER BY matched.evidence_content_sha256
    ) AS matched_evidence_sha256,
    totals.pending_record_count,
    binding.project_id AS current_project_id,
    binding.project_key AS current_project_key
  FROM exact_matches AS matched
  JOIN thread_totals AS totals USING(thread_id)
  LEFT JOIN memory.current_project_thread_binding_v5 AS binding
    ON binding.owner_user_id='{owner}'::uuid
   AND binding.thread_id=matched.thread_id
  GROUP BY
    matched.thread_id,totals.pending_record_count,
    binding.project_id,binding.project_key
)
SELECT jsonb_build_object(
  'registry_rows',(SELECT count(*) FROM registry),
  'registry',(
    SELECT jsonb_build_object(
      'project_id',project_id,
      'project_key',project_key,
      'component_id',component_id,
      'component_key',component_key,
      'display_name',display_name,
      'aliases',(SELECT jsonb_agg(normalized_alias ORDER BY normalized_alias)
                 FROM aliases)
    )
    FROM registry
  ),
  'summary',jsonb_build_object(
    'pending_jobs',(SELECT count(*) FROM owner_pending),
    'pending_threads',(SELECT count(DISTINCT thread_id) FROM valid_thread_pending),
    'matched_evidence',(SELECT count(*) FROM exact_matches),
    'candidate_threads',(SELECT count(*) FROM candidates)
  ),
  'candidates',coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'thread_id',thread_id,
      'thread_sha256',encode(public.digest(convert_to(thread_id::text,'UTF8'),'sha256'),'hex'),
      'pending_record_count',pending_record_count,
      'matched_record_count',matched_record_count,
      'matched_evidence_sha256',to_jsonb(matched_evidence_sha256),
      'current_project_id',current_project_id,
      'current_project_key',current_project_key
    ) ORDER BY thread_id)
    FROM candidates
  ),'[]'::jsonb)
)::text;
ROLLBACK;
"""
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            role,
            "-d",
            database,
        ],
        input=sql,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SelectorError(f"read-only selector query failed: {completed.stderr.strip()}")
    payloads = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if len(payloads) != 1:
        raise SelectorError("read-only selector returned an invalid payload")
    return json.loads(payloads[0])


def build_report(
    *,
    owner: uuid.UUID,
    project_key: str,
    component_key: str,
    expected_candidates: int,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if snapshot.get("registry_rows") != 1 or not isinstance(snapshot.get("registry"), dict):
        raise SelectorError("exactly one owner-scoped project component is required")
    registry = snapshot["registry"]
    aliases = registry.get("aliases")
    if not isinstance(aliases, list) or not aliases:
        raise SelectorError("registered component aliases are absent")
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATE_THREADS:
        raise SelectorError("candidate thread count is invalid")
    if len(candidates) != expected_candidates:
        raise SelectorError(
            f"candidate count changed: expected {expected_candidates}, got {len(candidates)}"
        )

    actions: list[dict[str, Any]] = []
    for candidate in candidates:
        thread_id = validate_uuid(candidate["thread_id"], "thread_id")
        evidence_hashes = candidate.get("matched_evidence_sha256")
        if (
            not isinstance(evidence_hashes, list)
            or not evidence_hashes
            or len(evidence_hashes) > MAX_MATCHED_EVIDENCE
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(item)) for item in evidence_hashes)
        ):
            raise SelectorError("matched evidence hashes are invalid")
        current_project_id = candidate.get("current_project_id")
        target_project_id = str(validate_uuid(registry["project_id"], "project_id"))
        if current_project_id is None:
            action = "propose_bind"
        elif current_project_id == target_project_id:
            action = "already_bound_same_project"
        else:
            action = "binding_conflict"
        operation_id = uuid.uuid5(
            OPERATION_NAMESPACE,
            ":".join(
                [
                    VERSION,
                    str(owner),
                    str(thread_id),
                    target_project_id,
                    component_key,
                    "explicit_registered_component_name",
                ]
            ),
        )
        actions.append(
            {
                "action": action,
                "operation_id": str(operation_id),
                "thread_id": str(thread_id),
                "thread_sha256": candidate["thread_sha256"],
                "project_id": target_project_id,
                "project_key": project_key,
                "component_key": component_key,
                "reason_code": "explicit_registered_component_name",
                "pending_record_count": candidate["pending_record_count"],
                "matched_record_count": candidate["matched_record_count"],
                "matched_evidence_sha256": evidence_hashes,
            }
        )
    if any(item["action"] == "binding_conflict" for item in actions):
        raise SelectorError("an exact-name candidate conflicts with an existing binding")

    return {
        "report_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "owner_user_id": str(owner),
        "owner_sha256": sha256_text(str(owner)),
        "selector": {
            "rule": "exact_registered_component_alias_with_token_boundaries",
            "automatic_inference": False,
            "project_key": project_key,
            "component_key": component_key,
            "registered_aliases": aliases,
        },
        "summary": snapshot["summary"],
        "actions": actions,
        "controls": {
            "database_writes": 0,
            "qdrant_reads": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
            "candidate_writes": 0,
            "claim_writes": 0,
            "projection_writes": 0,
            "prompt_influence": 0,
            "raw_content_in_report": False,
        },
    }


def main() -> int:
    args = arguments()
    owner = validate_uuid(args.owner_user_id, "owner_user_id")
    project_key = validate_key(args.project_key, "project_key")
    component_key = validate_key(args.component_key, "component_key")
    if not 0 <= args.expected_candidates <= MAX_CANDIDATE_THREADS:
        raise SelectorError("expected_candidates is outside the bounded range")
    snapshot = query_database(
        container=args.postgres_container,
        role=args.maintenance_role,
        database=args.database,
        owner=owner,
        project_key=project_key,
        component_key=component_key,
    )
    report = build_report(
        owner=owner,
        project_key=project_key,
        component_key=component_key,
        expected_candidates=args.expected_candidates,
        snapshot=snapshot,
    )
    report_sha = secure_write(args.output, report)
    print(
        stable_json(
            {
                "status": "passed",
                "report_path": str(args.output),
                "report_sha256": report_sha,
                "summary": report["summary"],
                "proposed_bindings": sum(
                    item["action"] == "propose_bind" for item in report["actions"]
                ),
                "write_counts": report["controls"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
