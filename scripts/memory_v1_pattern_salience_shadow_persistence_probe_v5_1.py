#!/usr/bin/env python3
"""Rollback-only validation for deterministic V5.1 shadow snapshot packets."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg


REPORT_CONTRACT = "memory_v1_pattern_salience_shadow_report_v5_1"


def stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--other-owner-user-id", required=True)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def load_report(path: Path, owner: uuid.UUID) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("contract_version") != REPORT_CONTRACT:
        raise RuntimeError("shadow report contract mismatch")
    if report.get("owner_user_id_sha256") != sha256_text(str(owner)):
        raise RuntimeError("shadow report owner binding mismatch")
    expected_hash = sha256_text(stable_json({
        key: value for key, value in report.items() if key != "report_sha256"
    }))
    if report.get("report_sha256") != expected_hash:
        raise RuntimeError("shadow report hash mismatch")
    if report.get("pattern_proposals") != []:
        raise RuntimeError("rollback probe accepts no pattern proposals")
    candidates = report.get("target_snapshot_candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32:
        raise RuntimeError("shadow target packet count is outside the bounded range")
    identities = {
        (
            candidate["packet"]["target"]["target_kind"],
            candidate["packet"]["target"]["target_id"],
            candidate["packet"]["target"]["target_revision_number"],
        )
        for candidate in candidates
    }
    if len(identities) != len(candidates):
        raise RuntimeError("shadow report contains duplicate targets")
    return report


async def expect_rejected(conn: asyncpg.Connection, request_id: uuid.UUID,
                          packet_text: str) -> None:
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        await conn.fetchrow(
            "SELECT * FROM memory.persist_epistemic_snapshot_packet_v5_1($1,$2::jsonb)",
            request_id,
            packet_text,
        )
    except asyncpg.PostgresError:
        await savepoint.rollback()
        return
    await savepoint.rollback()
    raise RuntimeError("cross-owner snapshot persistence was accepted")


async def run() -> None:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    other_owner = uuid.UUID(args.other_owner_user_id)
    if owner == other_owner:
        raise RuntimeError("owner and isolation-test owner must differ")
    report = load_report(args.report, owner)
    candidates = report["target_snapshot_candidates"]
    expected_links = sum(
        len(candidate["packet"]["evidence_assessment"][key])
        for candidate in candidates
        for key in (
            "supporting_observation_ids",
            "opposing_observation_ids",
            "qualifying_observation_ids",
            "corrective_observation_ids",
        )
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("rollback probe requires brains_app")

        transaction = conn.transaction(isolation="serializable")
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        applied_ids: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()
        for candidate in candidates:
            request_id = uuid.UUID(candidate["request_id"])
            packet_text = stable_json(candidate["packet"])
            applied = await conn.fetchrow(
                "SELECT * FROM memory.persist_epistemic_snapshot_packet_v5_1($1,$2::jsonb)",
                request_id,
                packet_text,
            )
            replay = await conn.fetchrow(
                "SELECT * FROM memory.persist_epistemic_snapshot_packet_v5_1($1,$2::jsonb)",
                request_id,
                packet_text,
            )
            if applied["apply_outcome"] != "applied":
                raise RuntimeError("first snapshot persistence did not apply")
            if replay["apply_outcome"] != "replayed":
                raise RuntimeError("snapshot replay was not zero-write")
            for key in (
                "target_binding_id",
                "assessment_snapshot_id",
                "feature_snapshot_id",
            ):
                if applied[key] != replay[key]:
                    raise RuntimeError("snapshot replay changed persistent identifiers")
            applied_ids.add((
                applied["target_binding_id"],
                applied["assessment_snapshot_id"],
                applied["feature_snapshot_id"],
            ))
        if len(applied_ids) != len(candidates):
            raise RuntimeError("snapshot persistence reused unexpected identifiers")
        await transaction.rollback()

        other_transaction = conn.transaction()
        await other_transaction.start()
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", str(other_owner)
        )
        first = candidates[0]
        await expect_rejected(
            conn,
            uuid.uuid5(uuid.NAMESPACE_URL, "memory-v5-1-cross-owner-probe"),
            stable_json(first["packet"]),
        )
        await other_transaction.rollback()
    finally:
        await conn.close()

    print(stable_json({
        "outcome": "rollback_only_probe_passed",
        "report_sha256": report["report_sha256"],
        "candidate_count": len(candidates),
        "expected_observation_link_count": expected_links,
        "restricted_api_results_rolled_back": len(candidates),
        "replay_writes": 0,
        "cross_owner_rejected": True,
        "persistent_writes": "verify_with_external_table_hashes",
    }))


if __name__ == "__main__":
    asyncio.run(run())
