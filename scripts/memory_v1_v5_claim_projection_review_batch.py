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


MANIFEST_CONTRACT = "memory_v1_claim_projection_review_batch_manifest_v1"
RESULT_CONTRACT = "memory_v1_claim_projection_review_batch_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")


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
        "contract_version", "owner_user_id", "required_head_commit", "decision",
        "reviewer_type", "reviewer_ref", "reason", "reason_codes",
        "expected_new_rows", "items", "manifest_sha256",
    }
    if set(value) != keys or value.get("contract_version") != MANIFEST_CONTRACT:
        raise ReviewBatchError("manifest contract or fields mismatch")
    if value["manifest_sha256"] != sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    ):
        raise ReviewBatchError("manifest hash mismatch")
    uuid.UUID(value["owner_user_id"])
    if (
        value["decision"] != "authorized"
        or value["reviewer_type"] != "system"
        or not isinstance(value["reviewer_ref"], str)
        or not value["reviewer_ref"].strip()
        or not isinstance(value["reason"], str)
        or not value["reason"].strip()
        or not isinstance(value["reason_codes"], list)
        or len(value["reason_codes"]) < 1
        or len(set(value["reason_codes"])) != len(value["reason_codes"])
        or value["expected_new_rows"] != 4
        or not isinstance(value["items"], list)
        or len(value["items"]) != 4
    ):
        raise ReviewBatchError("manifest review boundary mismatch")
    return value


def load_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {
        "bundle_path", "bundle_file_sha256", "bundle_sha256", "plan_id",
        "projection_ref", "predicate", "canonical_text_sha256",
        "projection_sha256", "semantic_key_sha256", "review_number",
        "authorization_manifest_sha256",
    }
    seen: set[str] = set()
    loaded: list[dict[str, Any]] = []
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != keys:
            raise ReviewBatchError("manifest item fields mismatch")
        plan_id = str(uuid.UUID(item["plan_id"]))
        if plan_id in seen or item["projection_ref"] != "p01" or item["review_number"] != 1:
            raise ReviewBatchError("duplicate or invalid review identity")
        seen.add(plan_id)
        path = private_path(item["bundle_path"])
        if file_sha256(path) != item["bundle_file_sha256"]:
            raise ReviewBatchError("bundle file hash mismatch")
        bundle = json.loads(path.read_text())
        if (
            bundle.get("bundle_sha256") != item["bundle_sha256"]
            or bundle.get("bundle_sha256")
            != sha256({key: value for key, value in bundle.items() if key != "bundle_sha256"})
            or bundle.get("owner_user_id") != manifest["owner_user_id"]
            or bundle.get("plan_id") != plan_id
        ):
            raise ReviewBatchError("bundle identity or content hash mismatch")
        projections = bundle.get("packet", {}).get("projections")
        if not isinstance(projections, list) or len(projections) != 1:
            raise ReviewBatchError("bundle projection count mismatch")
        projection = projections[0]
        if (
            projection.get("projection_ref") != item["projection_ref"]
            or projection.get("identity", {}).get("predicate") != item["predicate"]
            or projection.get("identity", {}).get("semantic_key_sha256")
            != item["semantic_key_sha256"]
            or sha256(projection) != item["projection_sha256"]
            or hashlib.sha256(projection.get("payload", {}).get("canonical_text", "").encode()).hexdigest()
            != item["canonical_text_sha256"]
            or projection.get("review", {}).get("state") != "manual_review_required"
        ):
            raise ReviewBatchError("bundle no longer matches reviewed projection")
        loaded.append({**item, "bundle_path": path})
    return sorted(loaded, key=lambda item: item["plan_id"])


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest)
    output = private_path(args.output, output=True)
    manifest = load_manifest(manifest_path)
    items = load_items(manifest)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD", "") != manifest["required_head_commit"]:
        raise ReviewBatchError("runtime head does not match manifest")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_CLAIM_PROJECTION_REVIEW_BATCH_APPLY", ""
    ) != "authorized":
        raise ReviewBatchError("apply mode requires the bounded authorization gate")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewBatchError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ReviewBatchError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=args.mode == "preflight", isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"])
        prepared: list[tuple[dict[str, Any], Any]] = []
        for item in items:
            row = await conn.fetchrow(
                """SELECT * FROM memory.preflight_projection_review_v5(
                    $1,$2,'authorized'::memory.projection_review_decision_v5,
                    $3,$4,$5,$6::jsonb
                )""",
                uuid.UUID(item["plan_id"]), item["projection_ref"],
                manifest["reviewer_type"], manifest["reviewer_ref"],
                manifest["reason"], json.dumps(manifest["reason_codes"], separators=(",", ":")),
            )
            if (
                row["review_number"] != item["review_number"]
                or row["projection_sha256"] != item["projection_sha256"]
                or row["semantic_key_sha256"] != item["semantic_key_sha256"]
                or row["authorization_manifest_sha256"]
                != item["authorization_manifest_sha256"]
            ):
                raise ReviewBatchError("projection review preflight drifted")
            prepared.append((item, row))
        if args.mode in {"apply", "replay"}:
            for item, _preflight in prepared:
                row = await conn.fetchrow(
                    """SELECT * FROM memory.review_projection_v5(
                        $1,$2,'authorized'::memory.projection_review_decision_v5,
                        $3,$4,$5,$6::jsonb,$7
                    )""",
                    uuid.UUID(item["plan_id"]), item["projection_ref"],
                    manifest["reviewer_type"], manifest["reviewer_ref"],
                    manifest["reason"], json.dumps(manifest["reason_codes"], separators=(",", ":")),
                    item["authorization_manifest_sha256"],
                )
                rows_written += row["rows_written"]
                outcomes.append(
                    {
                        "plan_id": item["plan_id"],
                        "projection_ref": item["projection_ref"],
                        "predicate": item["predicate"],
                        "review_id": str(row["review_id"]),
                        "outcome": row["outcome"],
                        "rows_written": row["rows_written"],
                    }
                )
        if args.mode == "preflight":
            await tx.rollback()
        else:
            expected = manifest["expected_new_rows"] if args.mode == "apply" else 0
            if rows_written != expected:
                raise ReviewBatchError(f"review batch wrote {rows_written}; expected {expected}")
            expected_outcome = "applied" if args.mode == "apply" else "replayed"
            if any(item["outcome"] != expected_outcome for item in outcomes):
                raise ReviewBatchError("review outcome mismatch")
            await tx.commit()
    finally:
        await conn.close()

    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(items),
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
