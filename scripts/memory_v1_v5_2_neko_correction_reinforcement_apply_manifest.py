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


CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_apply_manifest_v1"
)
REVIEW_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_review_manifest_v1"
)
REVIEW_RESULT_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_review_result_v1"
)
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = "5261da41-f863-42cd-8e3f-6e947f9743f2"
CLAIM = "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9"
CLAIM_REVISION = 2
SEMANTIC_KEY = (
    "d6ecf2322b47c85b98d5776c85252b80992bf7c3e78f4a3759054555cb4a8124"
)
CANONICAL_TEXT = "Neko's canonical name is Neko."


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


def load_hashed(
    path: Path, contract: str, hash_field: str
) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("contract_version") != contract:
        raise ManifestError("artifact contract mismatch")
    expected = sha256(
        {key: item for key, item in value.items() if key != hash_field}
    )
    if value.get(hash_field) != expected:
        raise ManifestError("artifact content hash mismatch")
    return value


def request_id(owner: str, plan_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                "memory-v1-v5-2-neko-correction-reinforcement|"
                f"{owner}|{plan_id}|p01|apply"
            ),
        )
    )


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if owner != OWNER:
        raise ManifestError("owner is outside the exact reinforcement boundary")
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ManifestError("required head must be a full lowercase commit")
    review_manifest_path = private_path(args.review_manifest)
    review_result_path = private_path(args.review_result)
    output = private_path(args.output, output=True)
    review_manifest = load_hashed(
        review_manifest_path, REVIEW_CONTRACT, "manifest_sha256"
    )
    review_result = load_hashed(
        review_result_path, REVIEW_RESULT_CONTRACT, "result_sha256"
    )
    items = review_manifest.get("items")
    outcomes = review_result.get("outcomes")
    if (
        review_manifest.get("owner_user_id") != owner
        or review_result.get("owner_user_id") != owner
        or review_result.get("mode") != "apply"
        or review_result.get("manifest_sha256")
        != review_manifest.get("manifest_sha256")
        or review_result.get("rows_written") != 1
        or not isinstance(items, list)
        or len(items) != 1
        or not isinstance(outcomes, list)
        or len(outcomes) != 1
    ):
        raise ManifestError("review artifacts are outside the exact boundary")
    reviewed = items[0]
    outcome = outcomes[0]
    plan_id = str(uuid.UUID(reviewed["plan_id"]))
    review_id = str(uuid.UUID(outcome["review_id"]))
    if (
        reviewed.get("projection_ref") != "p01"
        or reviewed.get("observation_id") != OBSERVATION
        or reviewed.get("predicate") != "identity.name_canonical"
        or reviewed.get("semantic_key_sha256") != SEMANTIC_KEY
        or reviewed.get("canonical_text_sha256")
        != hashlib.sha256(CANONICAL_TEXT.encode()).hexdigest()
        or outcome.get("plan_id") != plan_id
        or outcome.get("observation_id") != OBSERVATION
        or outcome.get("decision") != "authorized"
        or outcome.get("outcome") != "applied"
        or outcome.get("rows_written") != 1
    ):
        raise ManifestError("authorized review semantics drifted")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(readonly=True, isolation="serializable")
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", owner
        )
        state = await connection.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
            uuid.UUID(plan_id),
            "p01",
            uuid.UUID(review_id),
        )
        source = await connection.fetchrow(
            """
            SELECT item.target_action::text AS target_action,
                   item.expected_revision_number,
                   item.semantic_key_sha256,
                   item.projection_sha256,
                   item.target_reason_codes,
                   item.review_reason_codes,
                   payload.target_claim_id,
                   payload.claim_class,
                   payload.canonical_text,
                   payload.surface_policy::text AS surface_policy,
                   link.observation_id
            FROM memory.projection_plan_item AS item
            JOIN memory.projection_claim_payload AS payload
              USING(owner_user_id,plan_id,projection_ref)
            JOIN memory.projection_plan_observation AS link
              USING(owner_user_id,plan_id,projection_ref)
            WHERE item.owner_user_id=$1
              AND item.plan_id=$2
              AND item.projection_ref='p01'
            """,
            uuid.UUID(owner),
            uuid.UUID(plan_id),
        )
        if state is None or source is None:
            raise ManifestError("reviewed reinforcement is absent")
        source = dict(source)
        for field in ("target_reason_codes", "review_reason_codes"):
            if isinstance(source[field], str):
                source[field] = json.loads(source[field])
        if (
            state["lane"] != "claim"
            or state["target_action"] != "reinforce"
            or state["review_state"] != "manual_review_required"
            or state["current_revision_number"] != CLAIM_REVISION
            or str(state["review_id"]) != review_id
            or source["target_action"] != "reinforce"
            or source["expected_revision_number"] != CLAIM_REVISION
            or source["semantic_key_sha256"] != SEMANTIC_KEY
            or source["projection_sha256"] != reviewed["projection_sha256"]
            or source["target_reason_codes"]
            != [
                "additional_supporting_observation",
                "canonical_name_correction_normalized",
            ]
            or source["review_reason_codes"]
            != ["v5_2_canonical_name_reinforcement_requires_review"]
            or str(source["target_claim_id"]) != CLAIM
            or source["claim_class"] != "direct_claim"
            or source["canonical_text"] != CANONICAL_TEXT
            or source["surface_policy"] != "direct_or_relevant"
            or str(source["observation_id"]) != OBSERVATION
        ):
            raise ManifestError("projection apply preflight drifted")
        await transaction.rollback()
    finally:
        await connection.close()

    expected_table_rows = {
        "claim_observation": 1,
        "projection_apply_event": 1,
        "projection_dispatch_v5": 1,
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
        "expected_rows": sum(expected_table_rows.values()),
        "expected_table_rows": expected_table_rows,
        "item": {
            "plan_id": plan_id,
            "projection_ref": "p01",
            "observation_id": OBSERVATION,
            "claim_id": CLAIM,
            "claim_revision_number": CLAIM_REVISION,
            "semantic_key_sha256": SEMANTIC_KEY,
            "projection_sha256": reviewed["projection_sha256"],
            "review_id": review_id,
            "request_id": request_id(owner, plan_id),
            "apply_manifest_sha256": state["apply_manifest_sha256"],
        },
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
