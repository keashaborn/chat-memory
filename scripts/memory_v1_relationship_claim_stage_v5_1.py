#!/usr/bin/env python3
"""Preflight, stage, or replay one hash-locked V5.1 relationship claim plan."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
import uuid

from memory_v1_relationship_claim_bundle_v5_1 import BUNDLE_CONTRACT
from memory_v1_relationship_claim_projection_v5_1 import (
    build_packet,
    build_projection,
    load_source,
    owner_manifest,
    stable_json,
    validate_packet,
)
from memory_v1_v5_1_entailment_batch import private_path, sha256


RESULT_CONTRACT = "memory_v1_relationship_claim_stage_result_v5_1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load_bundle(path_value: str) -> dict[str, Any]:
    path = private_path(path_value, must_exist=True)
    bundle = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "contract_version", "owner_user_id", "required_head_commit",
        "observation_id", "observation_sha256", "plan_id", "predicate",
        "canonical_text_sha256", "packet", "packet_text", "packet_sha256",
        "owner_manifest_sha256", "expected_new_rows", "database_writes",
        "external_model_calls", "local_model_calls", "qdrant_writes",
        "prompt_influence", "bundle_sha256",
    }
    if not isinstance(bundle, dict) or set(bundle) != expected:
        raise RuntimeError("relationship claim bundle fields mismatch")
    if bundle["contract_version"] != BUNDLE_CONTRACT:
        raise RuntimeError("relationship claim bundle contract mismatch")
    uuid.UUID(bundle["owner_user_id"])
    uuid.UUID(bundle["observation_id"])
    uuid.UUID(bundle["plan_id"])
    if bundle["expected_new_rows"] != 4 or any(
        bundle[field] != 0 for field in (
            "database_writes", "external_model_calls", "local_model_calls",
            "qdrant_writes", "prompt_influence",
        )
    ):
        raise RuntimeError("relationship claim bundle boundary mismatch")
    if bundle["bundle_sha256"] != sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    ):
        raise RuntimeError("relationship claim bundle hash mismatch")
    if stable_json(bundle["packet"]) != bundle["packet_text"]:
        raise RuntimeError("relationship claim packet text mismatch")
    return bundle


async def run() -> int:
    import asyncpg

    args = arguments()
    bundle = load_bundle(args.bundle)
    output = private_path(args.output, must_exist=False)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != bundle["required_head_commit"]:
        raise RuntimeError("runtime head does not match relationship claim bundle")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_RELATIONSHIP_CLAIM_STAGE_APPLY"
    ) != "authorized":
        raise RuntimeError("relationship claim stage capability is absent")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcome = "preflight"
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(isolation="serializable", readonly=args.mode == "preflight")
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", bundle["owner_user_id"])
        source = await load_source(conn, uuid.UUID(bundle["observation_id"]))
        projection = build_projection(bundle["owner_user_id"], source)
        packet = build_packet(projection)
        validate_packet(packet, bundle["owner_user_id"])
        packet_text = stable_json(packet)
        if (
            packet != bundle["packet"]
            or packet_text != bundle["packet_text"]
            or packet["packet_sha256"] != bundle["packet_sha256"]
            or owner_manifest(bundle["owner_user_id"], packet)
                != bundle["owner_manifest_sha256"]
            or source["observation_sha256"] != bundle["observation_sha256"]
            or source["predicate"] != bundle["predicate"]
            or sha256(source["canonical_text"]) != bundle["canonical_text_sha256"]
        ):
            raise RuntimeError("relationship claim bundle source drifted")
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_relationship_claim_packet_v5_1($1,$2)",
            uuid.UUID(bundle["plan_id"]),
            packet_text,
        )
        expected_existing = 1 if args.mode == "replay" else 0
        if (
            preflight["owner_manifest_sha256"] != bundle["owner_manifest_sha256"]
            or preflight["existing_claims"] != 0
            or preflight["existing_plans"] != expected_existing
        ):
            raise RuntimeError("relationship claim database preflight is stale")
        if args.mode in {"apply", "replay"}:
            staged = await conn.fetchrow(
                "SELECT * FROM memory.stage_relationship_claim_plan_v5_1($1,$2,$3)",
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
                raise RuntimeError("relationship claim preflight assigned a txid")
        else:
            expected_rows = 4 if args.mode == "apply" else 0
            expected_outcome = "applied" if args.mode == "apply" else "replayed"
            if rows_written != expected_rows or outcome != expected_outcome:
                raise RuntimeError("relationship claim stage row budget failed")
            await transaction.commit()
    finally:
        await conn.close()
    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": bundle["owner_user_id"],
        "bundle_sha256": bundle["bundle_sha256"],
        "plan_id": bundle["plan_id"],
        "observation_id": bundle["observation_id"],
        "predicate": bundle["predicate"],
        "outcome": outcome,
        "rows_written": rows_written,
        "claims_written": 0,
        "external_model_calls": 0,
        "local_model_calls": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"outcome={outcome}")
    print(f"rows_written={rows_written}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
