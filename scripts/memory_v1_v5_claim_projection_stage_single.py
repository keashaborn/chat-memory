#!/usr/bin/env python3
"""Preflight, stage, or replay one hash-locked generic V5.1 claim plan."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any
import uuid

from memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    sha256,
    stable_json,
    validate_packet,
)
from memory_v1_v5_claim_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    load_source,
)
from memory_v1_v5_claim_projection_stage_batch import private_review_path


BUNDLE_CONTRACT = "memory_v1_claim_projection_stage_bundle_v5_1"
RESULT_CONTRACT = "memory_v1_claim_projection_stage_single_result_v1"
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


class StageSingleError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load_bundle(path_value: str) -> dict[str, Any]:
    path = private_review_path(path_value, must_exist=True)
    bundle = json.loads(path.read_text())
    expected = {
        "contract_version",
        "owner_user_id",
        "plan_id",
        "projection_ref",
        "packet",
        "packet_text",
        "lane_scope",
        "source_snapshot",
        "database_writes",
        "external_model_calls",
        "qdrant_writes",
        "packet_text_sha256",
        "semantic_key_sha256",
        "projection_sha256",
        "packet_sha256",
        "owner_manifest_sha256",
        "bundle_sha256",
    }
    if not isinstance(bundle, dict) or set(bundle) != expected:
        raise StageSingleError("generic claim bundle fields mismatch")
    if (
        bundle["contract_version"] != BUNDLE_CONTRACT
        or bundle["projection_ref"] != "p01"
        or bundle["lane_scope"] != {}
        or bundle["database_writes"] != 0
        or bundle["external_model_calls"] != 0
        or bundle["qdrant_writes"] != 0
    ):
        raise StageSingleError("generic claim bundle boundary mismatch")
    uuid.UUID(bundle["owner_user_id"])
    uuid.UUID(bundle["plan_id"])
    if bundle["bundle_sha256"] != sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    ):
        raise StageSingleError("generic claim bundle hash mismatch")
    if stable_json(bundle["packet"]) != bundle["packet_text"]:
        raise StageSingleError("generic claim packet text mismatch")
    return bundle


async def run() -> int:
    import asyncpg

    args = arguments()
    bundle = load_bundle(args.bundle)
    output = private_review_path(args.output, must_exist=False)
    if (
        not HEAD_RE.fullmatch(args.required_head)
        or os.environ.get("MEMORY_V1_REQUIRED_HEAD") != args.required_head
    ):
        raise StageSingleError("runtime head does not match required head")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_CLAIM_PROJECTION_STAGE_SINGLE_APPLY"
    ) != "authorized":
        raise StageSingleError("generic claim stage capability is absent")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise StageSingleError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcome = "preflight"
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise StageSingleError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(
            isolation="serializable", readonly=args.mode == "preflight"
        )
        await transaction.start()
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", bundle["owner_user_id"]
        )
        source = await load_source(
            conn, uuid.UUID(bundle["source_snapshot"]["observation_id"])
        )
        projection = build_projection(bundle["owner_user_id"], source)
        packet = build_packet(projection)
        validate_packet(packet, bundle["owner_user_id"], load_contract())
        packet_text = stable_json(packet)
        if (
            packet != bundle["packet"]
            or packet_text != bundle["packet_text"]
            or packet["packet_sha256"] != bundle["packet_sha256"]
            or sha256(projection) != bundle["projection_sha256"]
            or projection["identity"]["semantic_key_sha256"]
            != bundle["semantic_key_sha256"]
            or owner_manifest_sha256(
                bundle["owner_user_id"], packet["packet_sha256"]
            )
            != bundle["owner_manifest_sha256"]
            or str(source["observation_id"])
            != bundle["source_snapshot"]["observation_id"]
            or source["observation_sha256"]
            != bundle["source_snapshot"]["observation_sha256"]
        ):
            raise StageSingleError("generic claim bundle source drifted")
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
            uuid.UUID(bundle["plan_id"]),
            packet_text,
        )
        expected_existing = 1 if args.mode == "replay" else 0
        if (
            preflight["owner_manifest_sha256"] != bundle["owner_manifest_sha256"]
            or preflight["existing_claims"] != 0
            or preflight["existing_plans"] != expected_existing
        ):
            raise StageSingleError("generic claim database preflight is stale")
        if args.mode in {"apply", "replay"}:
            staged = await conn.fetchrow(
                "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                uuid.UUID(bundle["plan_id"]),
                packet_text,
                bundle["owner_manifest_sha256"],
            )
            outcome = staged["outcome"]
            rows_written = int(staged["rows_written"])
        if args.mode == "preflight":
            txid = await conn.fetchval("SELECT txid_current_if_assigned()")
            await transaction.rollback()
            if txid is not None:
                raise StageSingleError("generic claim preflight assigned a txid")
        else:
            expected_rows = 4 if args.mode == "apply" else 0
            expected_outcome = "applied" if args.mode == "apply" else "replayed"
            if rows_written != expected_rows or outcome != expected_outcome:
                raise StageSingleError("generic claim stage row budget failed")
            await transaction.commit()
    finally:
        await conn.close()
    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": bundle["owner_user_id"],
        "required_head_commit": args.required_head,
        "bundle_sha256": bundle["bundle_sha256"],
        "plan_id": bundle["plan_id"],
        "observation_id": bundle["source_snapshot"]["observation_id"],
        "predicate": bundle["packet"]["projections"][0]["identity"]["predicate"],
        "outcome": outcome,
        "rows_written": rows_written,
        "claims_written": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"outcome={outcome}")
    print(f"rows_written={rows_written}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
