#!/usr/bin/env python3
"""Admit one fully reviewed owner-scoped V5 packet to private entailment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_local_packet_disposition import (
    canonical_owners,
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_reviewed_stage_admission_v1"
POLICY_VERSION = "memory_v1_v5_reviewed_stage_admission_policy_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_reviewed_stage_admission_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("25dd82af-87b2-51ac-a7e7-265fedcd4dcc")


class ReviewedStageAdmissionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def operation_ids(
    owner: uuid.UUID,
    artifact_id: uuid.UUID,
    batch_id: uuid.UUID,
    stage_manifest_sha256: str,
    resolution_state_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{artifact_id}|{batch_id}|{stage_manifest_sha256}|"
        f"{resolution_state_sha256}|{POLICY_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
    )


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_reviewed_stage_admission_v1(1)"
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise ReviewedStageAdmissionError("read-only plan assigned a txid")
    if len(rows) > 1:
        raise ReviewedStageAdmissionError("planner exceeded one-row budget")
    return dict(rows[0]) if rows else None


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "artifact_id_sha256": (
            sha256_text(str(row["artifact_id"])) if row else None
        ),
        "batch_id_sha256": sha256_text(str(row["batch_id"])) if row else None,
        "route": "admit_reviewed_stage" if row else "no_work",
    }


async def apply_once(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_id = uuid.UUID(str(target["artifact_id"]))
    batch_id = uuid.UUID(str(target["batch_id"]))
    operation_id, admission_id = operation_ids(
        owner,
        artifact_id,
        batch_id,
        str(target["stage_manifest_sha256"]),
        str(target["resolution_state_sha256"]),
    )
    values = (
        operation_id,
        admission_id,
        artifact_id,
        batch_id,
        target["stage_manifest_sha256"],
        target["resolution_state_sha256"],
        POLICY_VERSION,
    )
    sql = """
        SELECT * FROM memory.register_owner_v5_reviewed_stage_admission_v1(
          $1,$2,$3,$4,$5,$6,$7
        )
    """
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        first = await conn.fetchrow(sql, *values)
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        replay = await conn.fetchrow(sql, *values)
    if first is None or replay is None:
        raise ReviewedStageAdmissionError("register returned no row")
    return dict(first), dict(replay)


async def run() -> int:
    args = arguments()
    owners = canonical_owners(args.owner_user_id)
    if args.apply and os.getenv(
        "MEMORY_V1_V5_REVIEWED_STAGE_ADMISSION_APPLY"
    ) != APPLY_ENABLE_TOKEN:
        raise ReviewedStageAdmissionError("reviewed-stage capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ReviewedStageAdmissionError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ReviewedStageAdmissionError("worker requires brains_app")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        safe_plans = [sanitized_plan(owner, row) for owner, row in plans]
        base = {
            "worker_version": WORKER_VERSION,
            "plans": safe_plans,
            "external_model_calls": 0,
            "local_model_calls": 0,
            "claims": 0,
            "qdrant": 0,
            "prompt_influence": 0,
            "filesystem_writes": 0,
        }
        if not args.apply:
            print(stable_json({
                **base,
                "apply": False,
                "database_writes": 0,
            }))
            return 0
        if selected is None:
            print(stable_json({
                **base,
                "apply": True,
                "outcome": "no_work",
                "database_rows_created": 0,
                "zero_write_replay_proved": True,
            }))
            return 0
        owner, target = selected
        first, replay = await apply_once(conn, owner=owner, target=target)
        if (
            first["outcome"] != "applied"
            or int(first["rows_written"]) != 2
            or first["decision"] != "reviewed_entity_stage"
            or replay["outcome"] != "replayed"
            or int(replay["rows_written"]) != 0
            or first["admission_id"] != replay["admission_id"]
        ):
            raise ReviewedStageAdmissionError("apply/replay invariant failed")
        print(stable_json({
            **base,
            "apply": True,
            "outcome": "reviewed_stage_admitted",
            "database_rows_created": 2,
            "zero_write_replay_proved": True,
            "write_counts": {
                "packet_stage_admission": 1,
                "reviewed_stage_audit": 1,
                "claims": 0,
                "qdrant": 0,
                "prompt_influence": 0,
            },
        }))
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(stable_json({
            "worker_version": WORKER_VERSION,
            "apply": False,
            "outcome": "reviewed_stage_admission_error",
            "error_class": type(exc).__name__,
            "error_sha256": sha256_text(str(exc)),
            "external_model_calls": 0,
            "local_model_calls": 0,
            "claims": 0,
            "qdrant": 0,
            "prompt_influence": 0,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
