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


MANIFEST_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_apply_manifest_v1"
)
RESULT_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_apply_result_v1"
)
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = "5261da41-f863-42cd-8e3f-6e947f9743f2"
CLAIM = "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9"
CLAIM_REVISION = 2
EXPECTED_TABLE_ROWS = {
    "claim_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
}


class ApplyError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("preflight", "apply", "replay"), required=True
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply-result")
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
        raise ApplyError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ApplyError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyError("input must be a private regular file")
    return path


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ApplyError("artifact content hash mismatch")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    expected_keys = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "review_manifest_path",
        "review_manifest_file_sha256",
        "review_manifest_sha256",
        "review_result_path",
        "review_result_file_sha256",
        "review_result_sha256",
        "expected_rows",
        "expected_table_rows",
        "item",
        "manifest_sha256",
    }
    if (
        set(value) != expected_keys
        or value.get("contract_version") != MANIFEST_CONTRACT
        or value.get("owner_user_id") != OWNER
        or value.get("expected_rows") != 3
        or value.get("expected_table_rows") != EXPECTED_TABLE_ROWS
    ):
        raise ApplyError("manifest contract or row budget mismatch")
    for field in ("review_manifest_path", "review_result_path"):
        source = private_path(value[field])
        if file_sha256(source) != value[field.replace("_path", "_file_sha256")]:
            raise ApplyError("review artifact file hash mismatch")
    item = value.get("item")
    if (
        not isinstance(item, dict)
        or item.get("projection_ref") != "p01"
        or item.get("observation_id") != OBSERVATION
        or item.get("claim_id") != CLAIM
        or item.get("claim_revision_number") != CLAIM_REVISION
    ):
        raise ApplyError("manifest item is outside the exact boundary")
    for field in ("plan_id", "review_id", "request_id"):
        uuid.UUID(item[field])
    for field in (
        "semantic_key_sha256",
        "projection_sha256",
        "apply_manifest_sha256",
    ):
        if (
            not isinstance(item.get(field), str)
            or len(item[field]) != 64
            or any(character not in "0123456789abcdef" for character in item[field])
        ):
            raise ApplyError("manifest hash is invalid")
    return value


def validate_apply_result(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "result_sha256")
    if (
        value.get("contract_version") != RESULT_CONTRACT
        or value.get("mode") != "apply"
        or value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("rows_written") != 3
        or value.get("claim_id") != CLAIM
    ):
        raise ApplyError("apply result cannot authorize replay")
    return value


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest)
    output = private_path(args.output, output=True)
    manifest = load_manifest(manifest_path)
    item = manifest["item"]
    if (
        os.environ.get("MEMORY_V1_REQUIRED_HEAD", "")
        != manifest["required_head_commit"]
    ):
        raise ApplyError("runtime head does not match manifest")
    if (
        args.mode == "apply"
        and os.environ.get(
            "MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_APPLY", ""
        )
        != "authorized"
    ):
        raise ApplyError("apply mode requires the bounded authorization gate")
    prior_path: Path | None = None
    if args.mode == "replay":
        if not args.apply_result:
            raise ApplyError("replay requires the immutable apply result")
        prior_path = private_path(args.apply_result)
        validate_apply_result(prior_path, manifest)
    elif args.apply_result:
        raise ApplyError("apply result is accepted only in replay mode")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ApplyError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    apply_event_id: str | None = None
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ApplyError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(
            readonly=args.mode == "preflight", isolation="serializable"
        )
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", OWNER
        )
        claim_before = await connection.fetchval(
            "SELECT to_jsonb(stored)::text FROM memory.claim AS stored "
            "WHERE owner_user_id=$1 AND claim_id=$2",
            uuid.UUID(OWNER),
            uuid.UUID(CLAIM),
        )
        revisions_before = await connection.fetchval(
            "SELECT count(*) FROM memory.claim_revision "
            "WHERE owner_user_id=$1 AND claim_id=$2",
            uuid.UUID(OWNER),
            uuid.UUID(CLAIM),
        )
        if claim_before is None or revisions_before != CLAIM_REVISION:
            raise ApplyError("target claim or revision count drifted")
        if args.mode != "replay":
            state = await connection.fetchrow(
                "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
                uuid.UUID(item["plan_id"]),
                "p01",
                uuid.UUID(item["review_id"]),
            )
            if (
                state is None
                or state["lane"] != "claim"
                or state["target_action"] != "reinforce"
                or state["review_state"] != "manual_review_required"
                or state["current_revision_number"] != CLAIM_REVISION
                or str(state["review_id"]) != item["review_id"]
                or state["apply_manifest_sha256"]
                != item["apply_manifest_sha256"]
            ):
                raise ApplyError("projection apply preflight drifted")
        if args.mode in {"apply", "replay"}:
            applied = await connection.fetchrow(
                "SELECT * FROM memory.apply_projection_v5($1,$2,$3,$4,$5)",
                uuid.UUID(item["request_id"]),
                uuid.UUID(item["plan_id"]),
                "p01",
                uuid.UUID(item["review_id"]),
                item["apply_manifest_sha256"],
            )
            expected_outcome = "applied" if args.mode == "apply" else "replayed"
            expected_rows = 3 if args.mode == "apply" else 0
            if (
                applied is None
                or applied["outcome"] != expected_outcome
                or applied["lane"] != "claim"
                or str(applied["aggregate_id"]) != CLAIM
                or applied["revision_number"] != CLAIM_REVISION
                or applied["rows_written"] != expected_rows
            ):
                raise ApplyError("projection apply outcome mismatch")
            rows_written = applied["rows_written"]
            apply_event_id = str(applied["apply_event_id"])
            if (
                await connection.fetchval(
                    "SELECT count(*) FROM memory.claim_observation "
                    "WHERE owner_user_id=$1 AND claim_id=$2 "
                    "AND observation_id=$3 AND stance='supports'",
                    uuid.UUID(OWNER),
                    uuid.UUID(CLAIM),
                    uuid.UUID(OBSERVATION),
                )
                != 1
            ):
                raise ApplyError("supporting observation link is absent")
        claim_after = await connection.fetchval(
            "SELECT to_jsonb(stored)::text FROM memory.claim AS stored "
            "WHERE owner_user_id=$1 AND claim_id=$2",
            uuid.UUID(OWNER),
            uuid.UUID(CLAIM),
        )
        revisions_after = await connection.fetchval(
            "SELECT count(*) FROM memory.claim_revision "
            "WHERE owner_user_id=$1 AND claim_id=$2",
            uuid.UUID(OWNER),
            uuid.UUID(CLAIM),
        )
        if (
            claim_after != claim_before
            or revisions_after != revisions_before
        ):
            raise ApplyError("reinforcement mutated claim content or revisions")
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            await transaction.commit()
    finally:
        await connection.close()

    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": OWNER,
        "manifest_sha256": manifest["manifest_sha256"],
        "apply_result_file_sha256": file_sha256(prior_path) if prior_path else None,
        "claim_id": CLAIM,
        "claim_revision_number": CLAIM_REVISION,
        "observation_id": OBSERVATION,
        "apply_event_id": apply_event_id,
        "rows_written": rows_written,
        "claims_written": 0,
        "claim_revisions_written": 0,
        "claim_observation_links_written": 1 if args.mode == "apply" else 0,
        "projection_apply_events_written": 1 if args.mode == "apply" else 0,
        "projection_dispatch_rows_written": 1 if args.mode == "apply" else 0,
        "projection_outbox_rows_written": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
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
