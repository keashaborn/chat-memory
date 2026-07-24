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
STAGE_CONTRACT = "memory_v1_v5_2_projection_stage_batch_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
REVIEWER_TYPE = "system"
REVIEWER_REF = "memory_v1_v5_2_stance_review_20260724"
VALID_DECISIONS = {"authorized", "rejected", "deferred"}


class ManifestError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--stage-manifest", required=True)
    parser.add_argument("--decisions", required=True)
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


def load_hashed_json(path: Path, contract: str, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("contract_version") != contract:
        raise ManifestError("artifact contract mismatch")
    expected = sha256({key: item for key, item in value.items() if key != hash_field})
    if value.get(hash_field) != expected:
        raise ManifestError("artifact hash mismatch")
    return value


def validate_decisions(
    value: dict[str, Any], owner: str, evidence_id: str
) -> dict[str, dict[str, Any]]:
    if set(value) != {
        "contract_version",
        "owner_user_id",
        "evidence_id",
        "decisions",
        "decisions_sha256",
    }:
        raise ManifestError("decision fields mismatch")
    if value["owner_user_id"] != owner or value["evidence_id"] != evidence_id:
        raise ManifestError("decision owner or evidence mismatch")
    decisions = value["decisions"]
    if not isinstance(decisions, list) or not 1 <= len(decisions) <= 32:
        raise ManifestError("decision count is outside bounds")
    by_observation: dict[str, dict[str, Any]] = {}
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {
            "observation_id",
            "decision",
            "reason",
            "reason_codes",
        }:
            raise ManifestError("decision item fields mismatch")
        observation_id = str(uuid.UUID(item["observation_id"]))
        reason_codes = item["reason_codes"]
        if (
            observation_id in by_observation
            or item["decision"] not in VALID_DECISIONS
            or not isinstance(item["reason"], str)
            or not item["reason"].strip()
            or len(item["reason"]) > 2000
            or not isinstance(reason_codes, list)
            or not reason_codes
            or len(reason_codes) > 20
            or len(set(reason_codes)) != len(reason_codes)
            or any(
                not isinstance(code, str)
                or not code
                or len(code) > 100
                for code in reason_codes
            )
        ):
            raise ManifestError("invalid or duplicate review decision")
        by_observation[observation_id] = item
    return by_observation


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ManifestError("required head must be a full lowercase commit")
    stage_path = private_path(args.stage_manifest)
    decisions_path = private_path(args.decisions)
    output = private_path(args.output, output=True)
    stage = load_hashed_json(stage_path, STAGE_CONTRACT, "manifest_sha256")
    if stage.get("owner_user_id") != owner:
        raise ManifestError("stage owner mismatch")
    evidence_id = str(uuid.UUID(stage["evidence_id"]))
    decisions_value = load_hashed_json(
        decisions_path, DECISION_CONTRACT, "decisions_sha256"
    )
    decisions = validate_decisions(decisions_value, owner, evidence_id)
    stage_by_observation = {
        str(uuid.UUID(item["observation_id"])): item for item in stage["items"]
    }
    if set(decisions) != set(stage_by_observation):
        raise ManifestError("review decisions must cover the exact staged observations")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(readonly=True, isolation="serializable")
        await transaction.start()
        await connection.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for observation_id in sorted(decisions):
            staged = stage_by_observation[observation_id]
            decision = decisions[observation_id]
            projections = staged.get("packet", {}).get("projections")
            if not isinstance(projections, list) or len(projections) != 1:
                raise ManifestError("staged item projection count mismatch")
            projection = projections[0]
            if (
                projection.get("projection_ref") != "p01"
                or projection.get("review", {}).get("state")
                != "manual_review_required"
                or projection.get("observation_inputs")
                != [
                    {
                        "observation_id": observation_id,
                        "observation_sha256": staged["observation_sha256"],
                        "stance": "supports",
                    }
                ]
            ):
                raise ManifestError("staged projection boundary mismatch")
            preflight = await connection.fetchrow(
                """
                SELECT * FROM memory.preflight_projection_review_v5(
                  $1,$2,$3::memory.projection_review_decision_v5,
                  $4,$5,$6,$7::jsonb
                )
                """,
                uuid.UUID(staged["plan_id"]),
                projection["projection_ref"],
                decision["decision"],
                REVIEWER_TYPE,
                REVIEWER_REF,
                decision["reason"],
                json.dumps(decision["reason_codes"], separators=(",", ":")),
            )
            if (
                preflight["review_number"] != 1
                or preflight["projection_sha256"] != sha256(projection)
                or preflight["semantic_key_sha256"]
                != projection["identity"]["semantic_key_sha256"]
            ):
                raise ManifestError("projection review preflight drifted")
            items.append(
                {
                    "plan_id": staged["plan_id"],
                    "projection_ref": projection["projection_ref"],
                    "observation_id": observation_id,
                    "observation_sha256": staged["observation_sha256"],
                    "predicate": projection["identity"]["predicate"],
                    "canonical_text_sha256": hashlib.sha256(
                        projection["payload"]["canonical_text"].encode()
                    ).hexdigest(),
                    "projection_sha256": preflight["projection_sha256"],
                    "semantic_key_sha256": preflight["semantic_key_sha256"],
                    "review_number": preflight["review_number"],
                    "decision": decision["decision"],
                    "reason": decision["reason"],
                    "reason_codes": decision["reason_codes"],
                    "authorization_manifest_sha256": preflight[
                        "authorization_manifest_sha256"
                    ],
                }
            )
        await transaction.rollback()
    finally:
        await connection.close()

    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "evidence_id": evidence_id,
        "required_head_commit": args.required_head,
        "reviewer_type": REVIEWER_TYPE,
        "reviewer_ref": REVIEWER_REF,
        "source_stage_manifest": str(stage_path),
        "source_stage_manifest_file_sha256": file_sha256(stage_path),
        "source_stage_manifest_sha256": stage["manifest_sha256"],
        "source_decisions": str(decisions_path),
        "source_decisions_file_sha256": file_sha256(decisions_path),
        "source_decisions_sha256": decisions_value["decisions_sha256"],
        "expected_new_rows": len(items),
        "items": sorted(items, key=lambda item: item["plan_id"]),
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
