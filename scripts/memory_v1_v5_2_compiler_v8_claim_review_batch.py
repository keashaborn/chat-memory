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


MANIFEST_CONTRACT = "memory_v1_v5_2_compiler_v8_claim_review_manifest_v1"
RESULT_CONTRACT = "memory_v1_v5_2_compiler_v8_claim_review_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
VALID_DECISIONS = {"authorized", "rejected", "deferred"}


class ReviewBatchError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
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
        raise ReviewBatchError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ReviewBatchError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ReviewBatchError("input must be a private regular file")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    keys = {
        "contract_version",
        "owner_user_id",
        "evidence_ids",
        "required_head_commit",
        "reviewer_type",
        "reviewer_ref",
        "source_stage_manifest",
        "source_stage_manifest_file_sha256",
        "source_stage_manifest_sha256",
        "source_decisions",
        "source_decisions_file_sha256",
        "source_decisions_sha256",
        "expected_new_rows",
        "items",
        "manifest_sha256",
    }
    if set(value) != keys or value.get("contract_version") != MANIFEST_CONTRACT:
        raise ReviewBatchError("manifest contract or fields mismatch")
    if value["manifest_sha256"] != sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    ):
        raise ReviewBatchError("manifest hash mismatch")
    uuid.UUID(value["owner_user_id"])
    if not isinstance(value["evidence_ids"], list) or len(value["evidence_ids"]) != 2:
        raise ReviewBatchError("exact evidence set is required")
    for evidence_id in value["evidence_ids"]:
        uuid.UUID(evidence_id)
    if (
        value["reviewer_type"] != "system"
        or value["reviewer_ref"]
        != "memory_v1_v5_2_compiler_v8_bound_claim_review_20260725"
        or value["expected_new_rows"] != len(value["items"])
        or not 1 <= len(value["items"]) <= 32
    ):
        raise ReviewBatchError("manifest review boundary mismatch")
    for path_field, hash_field in (
        ("source_stage_manifest", "source_stage_manifest_file_sha256"),
        ("source_decisions", "source_decisions_file_sha256"),
    ):
        source = private_path(value[path_field])
        if file_sha256(source) != value[hash_field]:
            raise ReviewBatchError("source artifact file hash mismatch")
    return value


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest)
    output = private_path(args.output, output=True)
    manifest = load_manifest(manifest_path)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD", "") != manifest["required_head_commit"]:
        raise ReviewBatchError("runtime head does not match manifest")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_V5_2_COMPILER_V8_CLAIM_REVIEW_APPLY", ""
    ) != "authorized":
        raise ReviewBatchError("apply mode requires the bounded authorization gate")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewBatchError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ReviewBatchError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(
            readonly=args.mode == "preflight", isolation="serializable"
        )
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"]
        )
        for item in manifest["items"]:
            if (
                item.get("decision") not in VALID_DECISIONS
                or item.get("projection_ref") != "p01"
                or item.get("review_number") != 1
            ):
                raise ReviewBatchError("invalid reviewed item")
            preflight = await connection.fetchrow(
                """
                SELECT * FROM memory.preflight_projection_review_v5(
                  $1,$2,$3::memory.projection_review_decision_v5,
                  $4,$5,$6,$7::jsonb
                )
                """,
                uuid.UUID(item["plan_id"]),
                item["projection_ref"],
                item["decision"],
                manifest["reviewer_type"],
                manifest["reviewer_ref"],
                item["reason"],
                json.dumps(item["reason_codes"], separators=(",", ":")),
            )
            if args.mode != "replay" and (
                preflight["review_number"] != item["review_number"]
                or preflight["projection_sha256"] != item["projection_sha256"]
                or preflight["semantic_key_sha256"] != item["semantic_key_sha256"]
                or preflight["authorization_manifest_sha256"]
                != item["authorization_manifest_sha256"]
            ):
                raise ReviewBatchError("projection review preflight drifted")
            if args.mode in {"apply", "replay"}:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM memory.review_projection_v5(
                      $1,$2,$3::memory.projection_review_decision_v5,
                      $4,$5,$6,$7::jsonb,$8
                    )
                    """,
                    uuid.UUID(item["plan_id"]),
                    item["projection_ref"],
                    item["decision"],
                    manifest["reviewer_type"],
                    manifest["reviewer_ref"],
                    item["reason"],
                    json.dumps(item["reason_codes"], separators=(",", ":")),
                    item["authorization_manifest_sha256"],
                )
                rows_written += row["rows_written"]
                outcomes.append(
                    {
                        "plan_id": item["plan_id"],
                        "observation_id": item["observation_id"],
                        "decision": item["decision"],
                        "review_id": str(row["review_id"]),
                        "outcome": row["outcome"],
                        "rows_written": row["rows_written"],
                    }
                )
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            expected = manifest["expected_new_rows"] if args.mode == "apply" else 0
            if rows_written != expected:
                raise ReviewBatchError(
                    f"review batch wrote {rows_written}; expected {expected}"
                )
            expected_outcome = "applied" if args.mode == "apply" else "replayed"
            if any(item["outcome"] != expected_outcome for item in outcomes):
                raise ReviewBatchError("review outcome mismatch")
            await transaction.commit()
    finally:
        await connection.close()

    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "evidence_ids": manifest["evidence_ids"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(manifest["items"]),
        "decision_counts": {
            decision: sum(
                item["decision"] == decision for item in manifest["items"]
            )
            for decision in sorted(VALID_DECISIONS)
        },
        "rows_written": rows_written,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "claims_written": 0,
        "projection_apply_events_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"rows_written={rows_written}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
