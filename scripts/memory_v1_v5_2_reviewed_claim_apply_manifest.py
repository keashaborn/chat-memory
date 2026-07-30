#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from memory_v1_projection_v5_contract_test import sha256


CONTRACT = "memory_v1_v5_2_reviewed_claim_apply_manifest_v1"
REVIEW_MANIFEST_CONTRACT = "memory_v1_v5_2_claim_target_review_manifest_v1"
REVIEW_RESULT_CONTRACT = "memory_v1_v5_2_claim_target_review_result_v1"
STAGE_MANIFEST_CONTRACT = "memory_v1_v5_2_claim_target_stage_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
ASSESSMENT = {
    "action": "promote_supported",
    "support_score": "1.000",
    "opposition_score": "0.000",
    "claim_confidence": "0.920",
    "assessment_confidence": "0.950",
    "reason_codes": [
        "accepted_observation_entailment",
        "active_owner_evidence",
        "authorized_projection_review",
        "no_active_opposition",
    ],
    "rationale": (
        "Direct owner-authored evidence has an accepted predicate entailment, "
        "an authorized deterministic projection review, and no active opposition."
    ),
    "reviewer_type": "system",
    "reviewer_ref": "memory_v1_v5_2_reviewed_claim_apply_v1",
}
CREATE_ROWS = {
    "claim": 1,
    "claim_revision": 2,
    "claim_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
    "claim_assessment_review_v5": 1,
    "claim_assessment": 1,
    "claim_assessment_apply_v5": 1,
    "relational_operation_request": 2,
}
REINFORCE_ROWS = {
    "claim_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
}


class ManifestError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--review-result", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ManifestError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ManifestError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ManifestError("input must be a private regular file")
    return path


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ManifestError(f"{path.name} content hash mismatch")
    return value


def request_id(owner: str, plan_id: str, operation: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memory-v1-v5-2-reviewed-claim-apply|{owner}|{plan_id}|p01|{operation}",
        )
    )


def add_rows(target: dict[str, int], source: dict[str, int], multiplier: int) -> None:
    for table, count in source.items():
        target[table] = target.get(table, 0) + count * multiplier


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ManifestError("required head must be a full lowercase commit hash")

    review_manifest_path = private_path(args.review_manifest)
    review_result_path = private_path(args.review_result)
    output = private_path(args.output, output=True)
    review_manifest = load_hashed(review_manifest_path, "manifest_sha256")
    review_result = load_hashed(review_result_path, "result_sha256")
    stage_manifest_path = private_path(review_manifest["source_stage_manifest"])
    stage_manifest = load_hashed(stage_manifest_path, "manifest_sha256")
    if (
        review_manifest.get("contract_version") != REVIEW_MANIFEST_CONTRACT
        or review_result.get("contract_version") != REVIEW_RESULT_CONTRACT
        or review_result.get("mode") != "apply"
        or review_manifest.get("owner_user_id") != owner
        or review_result.get("owner_user_id") != owner
        or review_result.get("manifest_sha256") != review_manifest.get("manifest_sha256")
        or review_result.get("rows_written") != len(review_manifest.get("items", []))
        or review_result.get("claims_written") != 0
        or review_result.get("qdrant_writes") != 0
        or review_result.get("retrieval_changes") != 0
        or review_result.get("prompt_influence") != 0
        or stage_manifest.get("contract_version") != STAGE_MANIFEST_CONTRACT
        or stage_manifest.get("owner_user_id") != owner
        or stage_manifest.get("manifest_sha256")
        != review_manifest.get("source_stage_manifest_sha256")
        or file_sha256(stage_manifest_path)
        != review_manifest.get("source_stage_manifest_file_sha256")
    ):
        raise ManifestError("review artifacts do not describe the authorized batch")

    reviewed_items = review_manifest.get("items")
    outcomes = review_result.get("outcomes")
    if (
        not isinstance(reviewed_items, list)
        or not isinstance(outcomes, list)
        or not 1 <= len(reviewed_items) <= 32
    ):
        raise ManifestError("reviewed batch size is invalid")
    reviewed = {item["plan_id"]: item for item in reviewed_items}
    result_by_plan = {item["plan_id"]: item for item in outcomes}
    staged_items = stage_manifest.get("stage_items")
    if not isinstance(staged_items, list):
        raise ManifestError("stage manifest item set is invalid")
    staged = {item["plan_id"]: item for item in staged_items}
    if (
        len(reviewed) != len(reviewed_items)
        or reviewed.keys() != result_by_plan.keys()
        or not reviewed.keys() <= staged.keys()
    ):
        raise ManifestError("reviewed plan identities differ")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    expected_table_rows: dict[str, int] = {}
    create_count = 0
    reinforce_count = 0
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(readonly=True, isolation="serializable")
        await transaction.start()
        await connection.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for plan_id in sorted(reviewed):
            source = reviewed[plan_id]
            outcome = result_by_plan[plan_id]
            staged_item = staged[plan_id]
            action = source.get("target_action")
            if action not in {"create", "reinforce"}:
                raise ManifestError("reviewed action is outside the claim boundary")
            if (
                source.get("decision") != "authorized"
                or source.get("projection_ref") != "p01"
                or source.get("review_number") != 1
                or outcome.get("outcome") != "applied"
                or outcome.get("rows_written") != 1
                or outcome.get("target_action") != action
            ):
                raise ManifestError("review outcome is not an applied authorization")
            review_id = str(uuid.UUID(outcome["review_id"]))
            state = await connection.fetchrow(
                "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
                uuid.UUID(plan_id),
                "p01",
                uuid.UUID(review_id),
            )
            projections = staged_item.get("packet", {}).get("projections", [])
            if len(projections) != 1:
                raise ManifestError("staged claim projection cardinality drifted")
            projection = projections[0]
            observation_inputs = projection.get("observation_inputs")
            if not isinstance(observation_inputs, list):
                raise ManifestError("staged observation input set is invalid")
            current_revision = 0 if action == "create" else source["expected_revision_number"]
            if (
                state is None
                or state["lane"] != "claim"
                or state["target_action"] != action
                or state["review_state"] != "manual_review_required"
                or state["current_revision_number"] != current_revision
                or str(state["review_id"]) != review_id
                or staged_item.get("action") != action
                or staged_item.get("predicate") != source["predicate"]
                or staged_item.get("semantic_key_sha256")
                != source["semantic_key_sha256"]
                or staged_item.get("observation_id") != source["observation_id"]
                or staged_item.get("observation_sha256")
                != source["observation_sha256"]
                or hashlib.sha256(
                    staged_item.get("canonical_text", "").encode()
                ).hexdigest()
                != source["canonical_text_sha256"]
                or projection.get("projection_ref") != "p01"
                or projection.get("target", {}).get("action") != action
                or projection.get("identity", {}).get("predicate")
                != source["predicate"]
                or projection.get("identity", {}).get("semantic_key_sha256")
                != source["semantic_key_sha256"]
                or not 1 <= len(observation_inputs) <= 100
                or (
                    action == "create"
                    and staged_item.get("target_claim_id") is not None
                )
                or (
                    action == "reinforce"
                    and (
                        staged_item.get("target_claim_id") is None
                        or staged_item.get("expected_revision_number")
                        != current_revision
                    )
                )
            ):
                raise ManifestError("projection apply preflight drifted")

            item = {
                "plan_id": plan_id,
                "projection_ref": "p01",
                "predicate": source["predicate"],
                "target_action": action,
                "current_revision_number": current_revision,
                "target_claim_id": staged_item.get("target_claim_id"),
                "observation_count": len(observation_inputs),
                "canonical_text_sha256": source["canonical_text_sha256"],
                "projection_sha256": source["projection_sha256"],
                "semantic_key_sha256": source["semantic_key_sha256"],
                "review_id": review_id,
                "projection_request_id": request_id(owner, plan_id, "projection-apply"),
                "projection_apply_manifest_sha256": state["apply_manifest_sha256"],
                "assessment_review_request_id": (
                    request_id(owner, plan_id, "assessment-review")
                    if action == "create"
                    else None
                ),
                "assessment_apply_request_id": (
                    request_id(owner, plan_id, "assessment-apply")
                    if action == "create"
                    else None
                ),
            }
            items.append(item)
            if action == "create":
                create_count += 1
                add_rows(expected_table_rows, CREATE_ROWS, 1)
            else:
                reinforce_count += 1
                add_rows(expected_table_rows, REINFORCE_ROWS, 1)
        await transaction.rollback()
    finally:
        await connection.close()

    expected_table_rows["projection_outbox"] = 0
    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        "review_manifest_path": str(review_manifest_path),
        "review_manifest_file_sha256": file_sha256(review_manifest_path),
        "review_manifest_sha256": review_manifest["manifest_sha256"],
        "review_result_path": str(review_result_path),
        "review_result_file_sha256": file_sha256(review_result_path),
        "review_result_sha256": review_result["result_sha256"],
        "source_stage_manifest_path": str(stage_manifest_path),
        "source_stage_manifest_file_sha256": file_sha256(stage_manifest_path),
        "source_stage_manifest_sha256": stage_manifest["manifest_sha256"],
        "assessment": ASSESSMENT,
        "action_counts": {"create": create_count, "reinforce": reinforce_count},
        "expected_insert_rows": sum(expected_table_rows.values()),
        "expected_mutated_rows": sum(expected_table_rows.values()) + create_count,
        "expected_table_rows": dict(sorted(expected_table_rows.items())),
        "items": items,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    print(f"create={create_count}")
    print(f"reinforce={reinforce_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
