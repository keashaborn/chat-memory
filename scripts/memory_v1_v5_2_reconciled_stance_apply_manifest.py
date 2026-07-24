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


CONTRACT = "memory_v1_claim_projection_apply_batch_manifest_v1"
STAGE_CONTRACT = "memory_v1_v5_2_reconciled_stance_stage_manifest_v1"
REVIEW_CONTRACT = "memory_v1_v5_2_projection_review_manifest_v1"
REVIEW_RESULT_CONTRACT = "memory_v1_v5_2_projection_review_batch_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
ID_NAMESPACE = uuid.UUID("e94ec954-5de0-49b2-84c0-d7cc71ff5b61")
ASSESSMENT = {
    "action": "promote_supported",
    "support_score": "1.000",
    "opposition_score": "0.000",
    "claim_confidence": "0.930",
    "assessment_confidence": "0.950",
    "reason_codes": [
        "accepted_primary_observation",
        "accepted_context_observation",
        "authorized_reconciled_projection_review",
        "no_active_opposition",
    ],
    "rationale": (
        "The owner-authored primary stance and contextual stance have accepted "
        "entailment, an authorized attributed projection, and no active opposition."
    ),
    "reviewer_type": "system",
    "reviewer_ref": "memory_v1_reconciled_stance_apply_20260724",
}
EXPECTED_TABLE_ROWS = {
    "claim": 1,
    "claim_revision": 2,
    "claim_observation": 2,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
    "claim_assessment_review_v5": 1,
    "claim_assessment": 1,
    "claim_assessment_apply_v5": 1,
    "relational_operation_request": 2,
    "projection_outbox": 0,
}


class ApplyManifestError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--stage-manifest", required=True)
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--review-result", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ApplyManifestError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ApplyManifestError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyManifestError("input must be a private mode-0600 file")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_hashed(
    path: Path, contract: str, hash_field: str
) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("contract_version") != contract or value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ApplyManifestError("source artifact contract or hash mismatch")
    return value


def request_id(owner: str, plan_id: str, operation: str) -> str:
    return str(
        uuid.uuid5(
            ID_NAMESPACE,
            "|".join((owner, plan_id, "p01", operation)),
        )
    )


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ApplyManifestError("required head must be a full commit")
    stage_path = private_path(args.stage_manifest)
    review_manifest_path = private_path(args.review_manifest)
    review_result_path = private_path(args.review_result)
    output = private_path(args.output, output=True)
    stage = load_hashed(stage_path, STAGE_CONTRACT, "manifest_sha256")
    review_manifest = load_hashed(
        review_manifest_path, REVIEW_CONTRACT, "manifest_sha256"
    )
    review_result = load_hashed(
        review_result_path, REVIEW_RESULT_CONTRACT, "result_sha256"
    )
    if (
        stage["owner_user_id"] != owner
        or review_manifest["owner_user_id"] != owner
        or review_result["owner_user_id"] != owner
        or stage["required_head_commit"] != args.required_head
        or review_manifest["required_head_commit"] != args.required_head
        or review_manifest["source_stage_manifest_sha256"]
        != stage["manifest_sha256"]
        or review_result["manifest_sha256"] != review_manifest["manifest_sha256"]
        or review_result["mode"] != "apply"
        or review_result["rows_written"] != 1
        or review_result["decision_counts"]["authorized"] != 1
        or len(review_result["outcomes"]) != 1
    ):
        raise ApplyManifestError("stage and review artifacts are not an authorized set")
    review = review_result["outcomes"][0]
    if (
        review["plan_id"] != stage["plan_id"]
        or review["decision"] != "authorized"
        or review["outcome"] != "applied"
        or review["rows_written"] != 1
    ):
        raise ApplyManifestError("review outcome is not authorized")
    review_id = str(uuid.UUID(review["review_id"]))

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ApplyManifestError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ApplyManifestError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
            uuid.UUID(stage["plan_id"]),
            uuid.UUID(review_id),
        )
        await tx.rollback()
    finally:
        await conn.close()
    if (
        preflight is None
        or preflight["lane"] != "claim"
        or preflight["target_action"] != "create"
        or preflight["review_state"] != "manual_review_required"
        or preflight["current_revision_number"] != 0
        or str(preflight["review_id"]) != review_id
        or len(
            stage["packet"]["projections"][0]["observation_inputs"]
        ) != 2
    ):
        raise ApplyManifestError("projection apply preflight drifted")

    item = {
        "plan_id": stage["plan_id"],
        "projection_ref": "p01",
        "predicate": "stance.reported",
        "canonical_text_sha256": stage["canonical_text_sha256"],
        "projection_sha256": stage["projection_sha256"],
        "semantic_key_sha256": stage["semantic_key_sha256"],
        "observation_count": 2,
        "review_id": review_id,
        "projection_request_id": request_id(
            owner, stage["plan_id"], "projection-apply"
        ),
        "projection_apply_manifest_sha256": preflight["apply_manifest_sha256"],
        "assessment_review_request_id": request_id(
            owner, stage["plan_id"], "assessment-review"
        ),
        "assessment_apply_request_id": request_id(
            owner, stage["plan_id"], "assessment-apply"
        ),
    }
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
        "assessment": ASSESSMENT,
        "expected_insert_rows": sum(EXPECTED_TABLE_ROWS.values()),
        "expected_mutated_rows": sum(EXPECTED_TABLE_ROWS.values()) + 1,
        "expected_table_rows": EXPECTED_TABLE_ROWS,
        "defer_projection_outbox": True,
        "items": [item],
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
