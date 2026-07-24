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


CONTRACT = "memory_v1_v5_2_projection_review_manifest_v1"
DECISION_CONTRACT = "memory_v1_v5_2_projection_review_decisions_v1"
STAGE_CONTRACT = "memory_v1_v5_2_reconciled_stance_stage_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
REVIEWER_TYPE = "system"
REVIEWER_REF = "memory_v1_v5_2_stance_review_20260724"
REASON = (
    "Authorize the attributed primary stance as a governed claim and retain the "
    "second accepted stance only as contextual support."
)
REASON_CODES = [
    "accepted_primary_observation",
    "accepted_context_observation",
    "attributed_owner_wording",
    "reconciled_stance_projection",
]


class ReviewManifestError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--stage-manifest", required=True)
    parser.add_argument("--decisions-output", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ReviewManifestError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ReviewManifestError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ReviewManifestError("input must be a private mode-0600 file")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_hashed(path: Path, contract: str, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("contract_version") != contract or value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ReviewManifestError("source artifact contract or hash mismatch")
    return value


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ReviewManifestError("required head must be a full commit")
    stage_path = private_path(args.stage_manifest)
    decisions_path = private_path(args.decisions_output, output=True)
    output = private_path(args.output, output=True)
    stage = load_hashed(stage_path, STAGE_CONTRACT, "manifest_sha256")
    if (
        stage["owner_user_id"] != owner
        or stage["required_head_commit"] != args.required_head
    ):
        raise ReviewManifestError("stage owner or head mismatch")

    decisions = {
        "contract_version": DECISION_CONTRACT,
        "owner_user_id": owner,
        "evidence_id": stage["evidence_id"],
        "decisions": [
            {
                "observation_id": stage["primary_observation_id"],
                "decision": "authorized",
                "reason": REASON,
                "reason_codes": REASON_CODES,
            }
        ],
    }
    decisions["decisions_sha256"] = sha256(decisions)
    write_private(decisions_path, decisions)

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewManifestError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ReviewManifestError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        preflight = await conn.fetchrow(
            """
            SELECT * FROM memory.preflight_projection_review_v5(
              $1,'p01','authorized'::memory.projection_review_decision_v5,
              $2,$3,$4,$5::jsonb
            )
            """,
            uuid.UUID(stage["plan_id"]),
            REVIEWER_TYPE,
            REVIEWER_REF,
            REASON,
            json.dumps(REASON_CODES, separators=(",", ":")),
        )
        await tx.rollback()
    finally:
        await conn.close()
    if (
        preflight is None
        or preflight["review_number"] != 1
        or preflight["projection_sha256"] != stage["projection_sha256"]
        or preflight["semantic_key_sha256"] != stage["semantic_key_sha256"]
    ):
        raise ReviewManifestError("projection review preflight drifted")

    item = {
        "plan_id": stage["plan_id"],
        "projection_ref": "p01",
        "observation_id": stage["primary_observation_id"],
        "observation_sha256": stage["packet"]["projections"][0][
            "observation_inputs"
        ][0]["observation_sha256"],
        "predicate": "stance.reported",
        "canonical_text_sha256": stage["canonical_text_sha256"],
        "projection_sha256": preflight["projection_sha256"],
        "semantic_key_sha256": preflight["semantic_key_sha256"],
        "review_number": preflight["review_number"],
        "decision": "authorized",
        "reason": REASON,
        "reason_codes": REASON_CODES,
        "authorization_manifest_sha256": preflight[
            "authorization_manifest_sha256"
        ],
    }
    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "evidence_id": stage["evidence_id"],
        "required_head_commit": args.required_head,
        "reviewer_type": REVIEWER_TYPE,
        "reviewer_ref": REVIEWER_REF,
        "source_stage_manifest": str(stage_path),
        "source_stage_manifest_file_sha256": file_sha256(stage_path),
        "source_stage_manifest_sha256": stage["manifest_sha256"],
        "source_decisions": str(decisions_path),
        "source_decisions_file_sha256": file_sha256(decisions_path),
        "source_decisions_sha256": decisions["decisions_sha256"],
        "expected_new_rows": 1,
        "items": [item],
    }
    manifest["manifest_sha256"] = sha256(manifest)
    write_private(output, manifest)
    print(f"decisions={decisions_path}")
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
