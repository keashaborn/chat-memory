#!/usr/bin/env python3
"""Build a private zero-write V5.1 death-event claim projection bundle."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import uuid

from memory_v1_life_event_claim_projection_v5_1 import (
    build_packet,
    build_projection,
    load_source,
    owner_manifest,
    stable_json,
    validate_packet,
)
from memory_v1_v5_1_entailment_batch import private_path, sha256


BUNDLE_CONTRACT = "memory_v1_life_event_claim_stage_bundle_v5_1"
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    observation_id = uuid.UUID(args.observation_id)
    plan_id = uuid.UUID(args.plan_id)
    if not HEAD_RE.fullmatch(args.required_head):
        raise RuntimeError("required head is invalid")
    output = private_path(args.output, must_exist=False)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        source = await load_source(conn, observation_id)
        projection = build_projection(owner, source)
        packet = build_packet(projection, source["predicate_registry_version"])
        validate_packet(packet, owner)
        packet_text = stable_json(packet)
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
            plan_id,
            packet_text,
        )
        txid = await conn.fetchval("SELECT txid_current_if_assigned()")
        await transaction.rollback()
    finally:
        await conn.close()
    expected_owner_manifest = owner_manifest(owner, packet)
    if (
        preflight["owner_manifest_sha256"] != expected_owner_manifest
        or preflight["packet_sha256"] != packet["packet_sha256"]
        or preflight["existing_claims"] != 0
        or preflight["existing_plans"] != 0
        or txid is not None
    ):
        raise RuntimeError("death-event claim preflight is stale or wrote data")
    bundle = {
        "contract_version": BUNDLE_CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        "observation_id": str(observation_id),
        "observation_sha256": source["observation_sha256"],
        "plan_id": str(plan_id),
        "predicate": source["predicate"],
        "canonical_text_sha256": sha256(source["canonical_text"]),
        "packet": packet,
        "packet_text": packet_text,
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": expected_owner_manifest,
        "expected_new_rows": 4,
        "database_writes": 0,
        "external_model_calls": 0,
        "local_model_calls": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
        "bundle_sha256": "",
    }
    bundle["bundle_sha256"] = sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    )
    output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"bundle={output}")
    print(f"bundle_sha256={bundle['bundle_sha256']}")
    print("database_writes=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
