#!/usr/bin/env python3
"""Produce a private zero-write review of claim create/reinforce targets."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_2_contract import sha256
from scripts.memory_v1_v5_2_claim_target_resolver import (
    resolve_claim_projection_target,
)
from scripts.memory_v1_v5_2_projection_stage_batch import load_contracts


CONTRACT = "memory_v1_v5_2_claim_target_review_v1"
ID_NAMESPACE = uuid.UUID("4f313962-aa4a-4a15-8811-81fc62137d70")
DISPOSABLE_CLONE_COMMENT = "memory_v1_v5_2_claim_target_review_clone_v1"


class ClaimTargetReviewError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--observation", action="append", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def private_output(value: str) -> Path:
    path = Path(value).resolve()
    if path.exists():
        raise ClaimTargetReviewError("output already exists")
    if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ClaimTargetReviewError("output parent must be a private directory")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def stable_plan_id(owner: str, observation: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{owner}|{observation}"))


async def load_source(conn: Any, observation: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
        uuid.UUID(observation),
    )
    if row is None:
        raise ClaimTargetReviewError(
            "complete owner-scoped projection source was not found"
        )
    source = dict(row)
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    return source


async def verify_runtime(conn: Any) -> bool:
    if os.getenv("MEMORY_V1_DISPOSABLE_CLONE_REQUIRED") != "1":
        return False
    row = await conn.fetchrow(
        """
        SELECT current_database() AS database_name,
               shobj_description(oid,'pg_database') AS database_comment
        FROM pg_database
        WHERE datname=current_database()
        """
    )
    if (
        row is None
        or not str(row["database_name"]).startswith(
            "memory_v5_2_claim_target_review_"
        )
        or row["database_comment"] != DISPOSABLE_CLONE_COMMENT
    ):
        database_name = None if row is None else row["database_name"]
        marker_matches = (
            False
            if row is None
            else row["database_comment"] == DISPOSABLE_CLONE_COMMENT
        )
        raise ClaimTargetReviewError(
            "review requires the marked disposable clone "
            f"(database={database_name}, marker_matches={marker_matches})"
        )
    return True


async def run() -> int:
    args = arguments()
    owner = str(uuid.UUID(args.owner))
    observations = sorted(str(uuid.UUID(value)) for value in args.observation)
    if not 1 <= len(observations) <= 32 or len(set(observations)) != len(
        observations
    ):
        raise ClaimTargetReviewError("observation set is invalid")
    output = private_output(args.output)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ClaimTargetReviewError("POSTGRES_DSN is required")
    _, registry = load_contracts()
    conn = await asyncpg.connect(dsn, command_timeout=60)
    items: list[dict[str, Any]] = []
    disposable_clone_verified = False
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ClaimTargetReviewError("review requires brains_app")
        disposable_clone_verified = await verify_runtime(conn)
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        try:
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)", owner
            )
            for observation in observations:
                source = await load_source(conn, observation)
                resolved = await resolve_claim_projection_target(
                    conn,
                    owner_user_id=owner,
                    source=source,
                    plan_id=stable_plan_id(owner, observation),
                    registry=registry,
                )
                packet = resolved["packet"]
                projection = (
                    packet["projections"][0] if packet is not None else None
                )
                items.append(
                    {
                        "observation_id": observation,
                        "observation_sha256": source["observation_sha256"],
                        "evidence_id": str(source["evidence_id"]),
                        "predicate": source["predicate"],
                        "plan_id": stable_plan_id(owner, observation),
                        "action": resolved["action"],
                        "reason_codes": resolved["reason_codes"],
                        "semantic_key_sha256": resolved[
                            "semantic_key_sha256"
                        ],
                        "target_claim_id": resolved["target_claim_id"],
                        "expected_revision_number": resolved[
                            "expected_revision_number"
                        ],
                        "existing_claim_status": resolved[
                            "existing_claim_status"
                        ],
                        "canonical_text": (
                            projection["payload"]["canonical_text"]
                            if projection is not None
                            else None
                        ),
                        "packet_sha256": resolved["packet_sha256"],
                        "owner_manifest_sha256": resolved[
                            "owner_manifest_sha256"
                        ],
                    }
                )
            if await conn.fetchval("SELECT txid_current_if_assigned()") is not None:
                raise ClaimTargetReviewError(
                    "zero-write review assigned a transaction ID"
                )
        finally:
            await transaction.rollback()
    finally:
        await conn.close()
    action_counts = dict(sorted(Counter(item["action"] for item in items).items()))
    report: dict[str, Any] = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "item_count": len(items),
        "action_counts": action_counts,
        "items": items,
        "proofs": {
            "database_writes": 0,
            "local_model_calls": 0,
            "external_model_calls": 0,
            "qdrant_writes": 0,
            "claim_writes": 0,
            "retrieval_changes": 0,
            "prompt_influence": 0,
            "read_only_transaction": True,
            "disposable_clone_verified": disposable_clone_verified,
        },
    }
    report["report_sha256"] = sha256(report)
    write_private(output, report)
    print(json.dumps({
        "contract_version": CONTRACT,
        "item_count": len(items),
        "action_counts": action_counts,
        "report_sha256": report["report_sha256"],
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
