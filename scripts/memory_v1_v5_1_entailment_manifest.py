#!/usr/bin/env python3
"""Create a private hash-locked manifest for reviewed V5.1 entailment decisions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import uuid

from memory_v1_v5_1_entailment_batch import (
    CONTRACT_VERSION,
    POLICY_VERSION,
    expected_reason,
    private_path,
    sha256,
    stable_json,
    validate_source_spans,
)


IDENTITY_NAMESPACE = uuid.UUID("a1da23ad-3443-55a8-90f5-9cf0d03b0f32")
REVIEW_CONTRACT = "memory_v1_observation_entailment_review_v5_1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--assessor-ref", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load_review(path_value: str, owner: str) -> dict:
    path = private_path(path_value, must_exist=True)
    review = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(review, dict) or set(review) != {
        "contract_version", "owner_user_id", "items", "review_sha256"
    }:
        raise RuntimeError("review fields mismatch")
    if review["contract_version"] != REVIEW_CONTRACT or review["owner_user_id"] != owner:
        raise RuntimeError("review boundary mismatch")
    if review["review_sha256"] != sha256(
        {key: value for key, value in review.items() if key != "review_sha256"}
    ):
        raise RuntimeError("review hash mismatch")
    if not isinstance(review["items"], list) or not 1 <= len(review["items"]) <= 20:
        raise RuntimeError("review item count is invalid")
    expected_keys = {"observation_id", "decision", "reason_code", "source_spans"}
    seen: set[uuid.UUID] = set()
    for item in review["items"]:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise RuntimeError("review item fields mismatch")
        observation_id = uuid.UUID(item["observation_id"])
        if observation_id in seen:
            raise RuntimeError("review contains duplicate observations")
        seen.add(observation_id)
        if not expected_reason(item["decision"], item["reason_code"]):
            raise RuntimeError("review decision and reason_code mismatch")
        validate_source_spans(item["source_spans"])
    return review


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if not args.assessor_ref or args.assessor_ref.strip() != args.assessor_ref or len(args.assessor_ref) > 500:
        raise RuntimeError("assessor_ref is invalid")
    review = load_review(args.review, owner)
    output = private_path(args.output, must_exist=False)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    items = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for review_item in review["items"]:
            observation_id = uuid.UUID(review_item["observation_id"])
            decision = review_item["decision"]
            reason = review_item["reason_code"]
            spans = review_item["source_spans"]
            preflight = await conn.fetchrow(
                """
                SELECT * FROM memory.preflight_observation_entailment_v5(
                  $1,$2::memory.observation_entailment_decision_v5,$3,$4::jsonb,
                  'system',$5
                )
                """,
                observation_id,
                decision,
                reason,
                stable_json(spans),
                args.assessor_ref,
            )
            request_id = uuid.uuid5(
                IDENTITY_NAMESPACE,
                f"{owner}|{observation_id}|{POLICY_VERSION}|{decision}|{reason}",
            )
            items.append({
                "observation_id": str(observation_id),
                "request_id": str(request_id),
                "decision": decision,
                "reason_code": reason,
                "source_spans": spans,
                "authorization_manifest_sha256": preflight["authorization_manifest_sha256"],
            })
        await transaction.rollback()
    finally:
        await conn.close()
    items.sort(key=lambda item: item["observation_id"])
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        "assessor_type": "system",
        "assessor_ref": args.assessor_ref,
        "review_sha256": review["review_sha256"],
        "expected_new_rows": len(items) * 2,
        "expected_table_rows": {
            "observation_entailment_v5": len(items),
            "relational_operation_request": len(items),
        },
        "items": items,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    print(f"expected_new_rows={manifest['expected_new_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
