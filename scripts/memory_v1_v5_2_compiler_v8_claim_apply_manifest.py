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
    "reviewer_ref": "memory_v1_v5_2_compiler_v8_claim_materialization_20260725",
}
EXPECTED_ROWS_PER_ITEM = {
    "claim": 1,
    "claim_revision": 2,
    "claim_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
    "claim_assessment_review_v5": 1,
    "claim_assessment": 1,
    "claim_assessment_apply_v5": 1,
    "relational_operation_request": 2,
    "projection_outbox": 0,
}
TARGETS = {
    "8f7b5442-9b8e-5417-b630-794eaa1429fd": {
        "observation_id": "70d55f38-1e33-418f-8ec6-6bfd2051f4e6",
        "predicate": "relationship.caregiver_for",
        "canonical_text": "The user is a caregiver for Monika.",
    },
    "974f7beb-f6a3-5231-ad52-0a7468d43533": {
        "observation_id": "bbd94cc7-e9d5-429f-8af1-1a029b119db0",
        "predicate": "relationship.spouse_of",
        "canonical_text": "The user is a spouse of Monika.",
    },
    "9e94e09c-dcb2-53e9-a0a5-77dd2971d6d1": {
        "observation_id": "a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc",
        "predicate": "occupation.works_as",
        "canonical_text": "The user formerly worked as BCBA.",
    },
    "fe8110e2-2ff5-5e44-add5-f8380331041d": {
        "observation_id": "c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb",
        "predicate": "occupation.works_as",
        "canonical_text": "The user formerly worked as clinical psychologist.",
    },
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
    expected = value.get(hash_field)
    if expected != sha256({key: item for key, item in value.items() if key != hash_field}):
        raise ManifestError(f"{path.name} content hash mismatch")
    return value


def request_id(owner: str, plan_id: str, operation: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memory-v1-v5-2-compiler-v8-claim|{owner}|{plan_id}|p01|{operation}",
        )
    )


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if len(args.required_head) != 40 or any(c not in "0123456789abcdef" for c in args.required_head):
        raise ManifestError("required head must be a full lowercase commit hash")
    review_manifest_path = private_path(args.review_manifest)
    review_result_path = private_path(args.review_result)
    output = private_path(args.output, output=True)
    review_manifest = load_hashed(review_manifest_path, "manifest_sha256")
    review_result = load_hashed(review_result_path, "result_sha256")
    if (
        review_manifest.get("contract_version")
        != "memory_v1_v5_2_compiler_v8_claim_review_manifest_v1"
        or review_result.get("contract_version")
        != "memory_v1_v5_2_compiler_v8_claim_review_result_v1"
        or review_result.get("mode") != "apply"
        or review_manifest.get("owner_user_id") != owner
        or review_result.get("owner_user_id") != owner
        or review_result.get("manifest_sha256") != review_manifest.get("manifest_sha256")
        or review_result.get("rows_written")
        != len(review_manifest.get("items", []))
    ):
        raise ManifestError("review artifacts do not describe the authorized batch")
    reviewed = {item["plan_id"]: item for item in review_result.get("outcomes", [])}
    staged = {item["plan_id"]: item for item in review_manifest.get("items", [])}
    if reviewed.keys() != staged.keys() or reviewed.keys() != TARGETS.keys():
        raise ManifestError("review and staged plan identities differ")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for plan_id in sorted(reviewed):
            review = reviewed[plan_id]
            source = staged[plan_id]
            target = TARGETS[plan_id]
            if (
                review.get("outcome") != "applied"
                or review.get("rows_written") != 1
                or review.get("decision") != "authorized"
                or review.get("observation_id") != target["observation_id"]
                or source.get("decision") != "authorized"
                or source.get("projection_ref") != "p01"
                or source.get("observation_id") != target["observation_id"]
                or source.get("predicate") != target["predicate"]
                or source.get("canonical_text_sha256")
                != hashlib.sha256(target["canonical_text"].encode()).hexdigest()
            ):
                raise ManifestError("review outcome is not an applied initial review")
            review_id = str(uuid.UUID(review["review_id"]))
            preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
                uuid.UUID(plan_id), "p01", uuid.UUID(review_id),
            )
            if (
                preflight["lane"] != "claim"
                or preflight["target_action"] != "create"
                or preflight["review_state"] != "manual_review_required"
                or preflight["current_revision_number"] != 0
                or str(preflight["review_id"]) != review_id
            ):
                raise ManifestError("projection apply preflight is outside the initial claim boundary")
            items.append(
                {
                    "plan_id": plan_id,
                    "projection_ref": "p01",
                    "predicate": target["predicate"],
                    "canonical_text_sha256": source["canonical_text_sha256"],
                    "projection_sha256": source["projection_sha256"],
                    "semantic_key_sha256": source["semantic_key_sha256"],
                    "review_id": review_id,
                    "projection_request_id": request_id(owner, plan_id, "projection-apply"),
                    "projection_apply_manifest_sha256": preflight["apply_manifest_sha256"],
                    "assessment_review_request_id": request_id(owner, plan_id, "assessment-review"),
                    "assessment_apply_request_id": request_id(owner, plan_id, "assessment-apply"),
                    "observation_count": 1,
                }
            )
        await tx.rollback()
    finally:
        await conn.close()

    expected_table_rows = {
        table: rows * len(items)
        for table, rows in EXPECTED_ROWS_PER_ITEM.items()
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
        "expected_insert_rows": sum(expected_table_rows.values()),
        "expected_mutated_rows": sum(expected_table_rows.values()) + len(items),
        "expected_table_rows": expected_table_rows,
        "items": items,
        "defer_projection_outbox": True,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
