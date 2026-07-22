#!/usr/bin/env python3
"""Build an immutable, source-free decision for one V5.2 packet deferral."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_1_review_local_packet import _secure_root, _output_path, _secure_write


CONTRACT = "memory_v1_v5_2_local_review_deferral_v1"
REASON = "ambiguous_transcription"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--reviewer-ref", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-root", default="/home/ubuntu/memory-v1-reviews")
    return parser.parse_args()


def decision_body(value: dict) -> dict:
    return {key: value[key] for key in sorted(value) if key != "decision_sha256"}


async def main() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    packet_id = uuid.UUID(args.packet_id)
    reviewer_ref = args.reviewer_ref.strip()
    if not reviewer_ref or len(reviewer_ref) > 200:
        raise RuntimeError("reviewer reference is invalid")
    root = _secure_root(args.review_root)
    output = _output_path(args.output, root)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("review decision requires brains_app session")
        async with conn.transaction(readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            rows = await conn.fetch(
                "SELECT * FROM memory.read_owner_v5_local_packet_review_v1($1)",
                packet_id,
            )
        if len(rows) != 1:
            raise RuntimeError("owner-scoped packet is absent")
        row = dict(rows[0])
    finally:
        await conn.close()
    packet = row["normalized_packet"]
    if isinstance(packet, str):
        packet = json.loads(packet)
    if (
        packet.get("contract_version") != "memory_v1_relational_extraction_v5_2"
        or packet.get("predicate_registry_version") != "memory_predicate_registry_v5_2"
        or row["provider_id"] != "local_llama_cpp"
        or row["external_model_calls"] != 0
        or row["exact_stage_batch_count"] != 0
        or row["evidence_stage_batch_count"] != 0
    ):
        raise RuntimeError("packet is not eligible for a V5.2 review deferral")
    decision = {
        "contract_version": CONTRACT,
        "decision_sha256": "",
        "owner_user_id": str(owner),
        "packet_id": str(packet_id),
        "packet_storage_sha256": row["packet_storage_sha256"],
        "evidence_content_sha256": row["evidence_content_sha256"],
        "validator_packet_sha256": row["validator_packet_sha256"],
        "review_decision": "deferred",
        "reason_code": REASON,
        "promotion_eligible": False,
        "reviewer_type": "owner_authorized_operator",
        "reviewer_ref": reviewer_ref,
        "source_prose_included": False,
        "external_model_calls": 0,
        "database_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }
    decision["decision_sha256"] = canonical_sha256(decision_body(decision))
    file_sha256 = _secure_write(output, decision)
    print(json.dumps({
        "contract_version": CONTRACT,
        "decision_sha256": decision["decision_sha256"],
        "decision_file_sha256": file_sha256,
        "owner_user_id_sha256": canonical_sha256(str(owner)),
        "packet_id_sha256": canonical_sha256(str(packet_id)),
        "review_decision": "deferred",
        "reason_code": REASON,
        "promotion_eligible": False,
        "source_prose_included": False,
        "database_writes": 0,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
