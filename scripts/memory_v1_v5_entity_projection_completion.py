#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "memory_v1_v5_entity_projection_completion_v1"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OCCUPATION_OBSERVATION = "9bf1e6b2-1840-4524-98dc-142567ebe013"
CORRECTION_OBSERVATION = "93024235-89a8-49d5-88fa-7e4a143b68f3"
SELF_RESOLUTION = "fc1859aa-f280-4aa1-b75a-ee882e969c87"
TRAINER_RESOLUTION = "a8befb71-413b-49b0-a460-c683ea52038e"
CORRECTION_RESOLUTION = "80b0821a-b9a5-41e5-8008-9db72b2ebfe6"
PROJECTION_PLAN = "33000000-0000-4000-8000-000000000001"
OCCUPATION_CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"
OCCUPATION_TEXT = "The user works as a personal trainer."


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only completion gate for the initial V5 entity/projection phase"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--postgres-container", default="brains-postgres-1")
    parser.add_argument("--maintenance-role", default="sage")
    parser.add_argument("--database", default="memory")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-collection", default="memory_claim_v1")
    return parser.parse_args()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _secure_write(path: Path, value: dict[str, Any]) -> str:
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
    return _sha256(payload)


def database_snapshot(
    *,
    container: str,
    maintenance_role: str,
    database: str,
) -> dict[str, Any]:
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT jsonb_build_object(
  'occupation', jsonb_build_object(
    'observation_rows', (
      SELECT count(*) FROM memory.observation
       WHERE owner_user_id='{OWNER}'::uuid
         AND observation_id='{OCCUPATION_OBSERVATION}'::uuid
         AND predicate='occupation.works_as'
    ),
    'self_resolution_apply_rows', (
      SELECT count(*) FROM memory.entity_resolution_apply
       WHERE owner_user_id='{OWNER}'::uuid
         AND resolution_id='{SELF_RESOLUTION}'::uuid
    ),
    'trainer_resolution_apply_rows', (
      SELECT count(*) FROM memory.entity_resolution_apply
       WHERE owner_user_id='{OWNER}'::uuid
         AND resolution_id='{TRAINER_RESOLUTION}'::uuid
    ),
    'binding_rows', (
      SELECT count(*) FROM memory.observation_entity_binding
       WHERE owner_user_id='{OWNER}'::uuid
         AND observation_id='{OCCUPATION_OBSERVATION}'::uuid
    ),
    'projection_plan_rows', (
      SELECT count(*) FROM memory.projection_plan
       WHERE owner_user_id='{OWNER}'::uuid
         AND plan_id='{PROJECTION_PLAN}'::uuid
    ),
    'projection_item_rows', (
      SELECT count(*) FROM memory.projection_plan_item
       WHERE owner_user_id='{OWNER}'::uuid
         AND plan_id='{PROJECTION_PLAN}'::uuid
         AND projection_ref='p01'
    ),
    'projection_review_rows', (
      SELECT count(*) FROM memory.projection_review
       WHERE owner_user_id='{OWNER}'::uuid
         AND plan_id='{PROJECTION_PLAN}'::uuid
         AND projection_ref='p01'
         AND decision='authorized'
    ),
    'projection_apply_rows', (
      SELECT count(*) FROM memory.projection_apply_event
       WHERE owner_user_id='{OWNER}'::uuid
         AND plan_id='{PROJECTION_PLAN}'::uuid
         AND projection_ref='p01'
         AND resulting_claim_id='{OCCUPATION_CLAIM}'::uuid
    ),
    'projection_dispatch_rows', (
      SELECT count(*) FROM memory.projection_dispatch_v5
       WHERE owner_user_id='{OWNER}'::uuid
         AND resulting_claim_id='{OCCUPATION_CLAIM}'::uuid
    ),
    'candidate_claim_rows', (
      SELECT count(*) FROM memory.claim
       WHERE owner_user_id='{OWNER}'::uuid
         AND claim_id='{OCCUPATION_CLAIM}'::uuid
         AND status='candidate'
         AND canonical_text='{OCCUPATION_TEXT}'
    )
  ),
  'correction', jsonb_build_object(
    'observation_rows', (
      SELECT count(*) FROM memory.observation
       WHERE owner_user_id='{OWNER}'::uuid
         AND observation_id='{CORRECTION_OBSERVATION}'::uuid
         AND predicate='identity.name_canonical'
         AND projection_class='correction'
    ),
    'deferred_plan_rows', (
      SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='{OWNER}'::uuid
         AND resolution_id='{CORRECTION_RESOLUTION}'::uuid
         AND action='defer'
         AND decision_state='deferred'
         AND review_reason_codes ? 'correction_target_resolution_required'
    ),
    'resolution_apply_rows', (
      SELECT count(*) FROM memory.entity_resolution_apply
       WHERE owner_user_id='{OWNER}'::uuid
         AND resolution_id='{CORRECTION_RESOLUTION}'::uuid
    ),
    'binding_rows', (
      SELECT count(*) FROM memory.observation_entity_binding
       WHERE owner_user_id='{OWNER}'::uuid
         AND observation_id='{CORRECTION_OBSERVATION}'::uuid
    ),
    'projection_input_rows', (
      SELECT count(*) FROM memory.projection_plan_observation
       WHERE owner_user_id='{OWNER}'::uuid
         AND observation_id='{CORRECTION_OBSERVATION}'::uuid
    )
  ),
  'isolation', jsonb_build_object(
    'other_owner_v5_rows', (
      SELECT
        (SELECT count(*) FROM memory.entity_mention
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.entity_resolution_plan
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.entity_resolution_review
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.entity_resolution_apply
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.observation
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.observation_entity_binding
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.projection_plan
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.projection_review
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.projection_apply_event
          WHERE owner_user_id<>'{OWNER}'::uuid)
        + (SELECT count(*) FROM memory.projection_dispatch_v5
          WHERE owner_user_id<>'{OWNER}'::uuid)
    )
  ),
  'registry', (
    SELECT jsonb_build_object(
      'status', status::text,
      'runtime_active', runtime_active,
      'contract_rows', (
        SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version='memory_predicate_registry_v5'
      )
    )
      FROM memory.predicate_registry_version
     WHERE registry_version='memory_predicate_registry_v5'
  )
);
ROLLBACK;
"""
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            maintenance_role,
            "-d",
            database,
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"read-only database snapshot failed: {completed.stderr.strip()}")
    payloads = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if len(payloads) != 1:
        raise RuntimeError("read-only database snapshot returned an invalid payload")
    return json.loads(payloads[0])


def qdrant_snapshot(*, url: str, collection: str) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/collections/{collection}/points/scroll"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {"limit": 10000, "with_payload": True, "with_vector": False}
        ).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    points = value.get("result", {}).get("points", [])
    canonical = sorted(
        (
            {"id": point.get("id"), "payload": point.get("payload", {})}
            for point in points
        ),
        key=lambda point: str(point["id"]),
    )
    return {
        "point_count": len(points),
        "payload_snapshot_sha256": _sha256(_stable_json(canonical).encode()),
        "v5_candidate_claim_present": any(
            OCCUPATION_CLAIM in _stable_json(point.get("payload", {}))
            for point in points
        ),
    }


def evaluate(database: dict[str, Any], qdrant: dict[str, Any]) -> dict[str, Any]:
    occupation = database.get("occupation", {})
    correction = database.get("correction", {})
    isolation = database.get("isolation", {})
    registry = database.get("registry") or {}
    occupation_one = {
        "observation_rows",
        "self_resolution_apply_rows",
        "trainer_resolution_apply_rows",
        "binding_rows",
        "projection_plan_rows",
        "projection_item_rows",
        "projection_review_rows",
        "projection_apply_rows",
        "projection_dispatch_rows",
        "candidate_claim_rows",
    }
    checks = {
        "occupation_path_complete": set(occupation) == occupation_one
        and all(occupation[key] == 1 for key in occupation_one),
        "correction_preserved_and_deferred": correction
        == {
            "observation_rows": 1,
            "deferred_plan_rows": 1,
            "resolution_apply_rows": 0,
            "binding_rows": 0,
            "projection_input_rows": 0,
        },
        "no_other_owner_v5_rows": isolation.get("other_owner_v5_rows") == 0,
        "registry_remains_inactive": registry.get("status") == "proposed"
        and registry.get("runtime_active") is False
        and registry.get("contract_rows") == 44,
        "v5_candidate_absent_from_qdrant": qdrant.get(
            "v5_candidate_claim_present"
        )
        is False,
    }
    return {
        "checks": checks,
        "phase_complete": all(checks.values()),
        "next_boundary": (
            "build_read_only_v5_shadow_retrieval_without_prompt_exposure"
            if all(checks.values())
            else "stop_and_investigate_failed_completion_check"
        ),
    }


def main() -> int:
    args = arguments()
    database = database_snapshot(
        container=args.postgres_container,
        maintenance_role=args.maintenance_role,
        database=args.database,
    )
    qdrant = qdrant_snapshot(
        url=args.qdrant_url,
        collection=args.qdrant_collection,
    )
    evaluation = evaluate(database, qdrant)
    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": "seebx",
        "mode": "read_only_zero_write",
        "owner_user_id": OWNER,
        "database_snapshot": database,
        "qdrant_snapshot": qdrant,
        "evaluation": evaluation,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "prompt_influence": False,
    }
    report_path = Path(args.output).resolve()
    report_sha256 = _secure_write(report_path, report)
    print(
        _stable_json(
            {
                "version": VERSION,
                "output": str(report_path),
                "sha256": report_sha256,
                "phase_complete": evaluation["phase_complete"],
                "next_boundary": evaluation["next_boundary"],
            }
        )
    )
    return 0 if evaluation["phase_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
