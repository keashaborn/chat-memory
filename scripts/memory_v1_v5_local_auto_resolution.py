#!/usr/bin/env python3
"""Apply at most one exact owner-scoped V5 entity link per cycle."""

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


WORKER_VERSION = "memory_v1_v5_local_auto_resolution_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_auto_resolution_apply_v1"
POLICY_VERSION = "memory_v1_v5_local_auto_resolution_policy_v1"
IDENTITY_NAMESPACE = uuid.UUID("74f32b96-680c-5d84-a376-0e03a8619168")


class LocalAutoResolutionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply at most one exact existing-entity link."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def operation_ids(
    owner: uuid.UUID,
    stage_admission_id: uuid.UUID,
    resolution_id: uuid.UUID,
    decision_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{stage_admission_id}|{resolution_id}|"
        f"{decision_sha256}|{POLICY_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"apply|{identity}"),
    )


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_auto_resolution_v1(1)"
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise LocalAutoResolutionError("read-only resolution planning assigned a txid")
    if len(rows) > 1:
        raise LocalAutoResolutionError("resolution planner exceeded its row budget")
    return dict(rows[0]) if rows else None


async def apply_once(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage_admission_id = uuid.UUID(str(target["stage_admission_id"]))
    resolution_id = uuid.UUID(str(target["resolution_id"]))
    selected_entity_id = uuid.UUID(str(target["selected_entity_id"]))
    operation_id, admission_id, apply_request_id = operation_ids(
        owner,
        stage_admission_id,
        resolution_id,
        target["decision_sha256"],
    )
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{owner}|local_auto_resolution_v1",
        )
        preflight = await conn.fetchrow(
            """
            SELECT resolution_id,action::text,decision_state::text,review_id,
                   prospective_entity_id,apply_manifest_sha256
            FROM memory.preflight_entity_resolution_apply_v5($1::uuid,NULL)
            """,
            resolution_id,
        )
        if preflight is None:
            raise LocalAutoResolutionError("resolution preflight returned no row")
        if (
            preflight["action"] != "link_existing"
            or preflight["decision_state"] != "auto_link_eligible"
            or preflight["review_id"] is not None
            or preflight["prospective_entity_id"] != selected_entity_id
        ):
            raise LocalAutoResolutionError("resolution preflight is not an exact link")
        admission = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_auto_resolution_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            admission_id,
            stage_admission_id,
            resolution_id,
            apply_request_id,
            selected_entity_id,
            target["decision_sha256"],
            preflight["apply_manifest_sha256"],
            POLICY_VERSION,
        )
        applied = await conn.fetchrow(
            """
            SELECT applied_entity_id,outcome,bindings_created,result
            FROM memory.apply_entity_resolution_v5($1,$2,NULL,$3)
            """,
            apply_request_id,
            resolution_id,
            preflight["apply_manifest_sha256"],
        )
    if admission is None or applied is None:
        raise LocalAutoResolutionError("controlled resolution API returned no row")
    return dict(admission), dict(applied)


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "resolution_id_sha256": (
            sha256_text(str(row["resolution_id"])) if row else None
        ),
        "route": "auto_apply_exact_existing_link" if row else "no_work",
    }


async def run() -> int:
    args = arguments()
    owners = canonical_owners(args.owner_user_id)
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_AUTO_RESOLUTION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise LocalAutoResolutionError("local auto-resolution capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalAutoResolutionError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalAutoResolutionError("local auto-resolution requires brains_app")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        safe_plans = [sanitized_plan(owner, row) for owner, row in plans]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "plans": safe_plans,
                        "database_writes": 0,
                        "external_model_calls": 0,
                        "qdrant_writes": 0,
                        "claim_writes": 0,
                        "projection_writes": 0,
                        "prompt_influence": 0,
                        "filesystem_writes": 0,
                    }
                )
            )
            return 0
        if selected is None:
            outcome = "no_work"
            bindings_created = 0
            created_rows = 0
        else:
            owner, target = selected
            admission, applied = await apply_once(
                conn, owner=owner, target=target
            )
            replay_admission, replay_applied = await apply_once(
                conn, owner=owner, target=target
            )
            if (
                admission["apply_outcome"] != "applied"
                or applied["outcome"] != "applied"
                or applied["applied_entity_id"] != target["selected_entity_id"]
                or replay_admission["apply_outcome"] != "replayed"
                or replay_applied["outcome"] != "replayed"
                or int(replay_applied["bindings_created"]) != 0
                or replay_applied["applied_entity_id"]
                != applied["applied_entity_id"]
            ):
                raise LocalAutoResolutionError(
                    "auto-resolution apply or replay invariant failed"
                )
            outcome = "exact_existing_link_applied"
            bindings_created = int(applied["bindings_created"])
            created_rows = 3 + bindings_created
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": outcome,
                    "plans": safe_plans,
                    "bindings_created": bindings_created,
                    "database_rows_created": created_rows,
                    "zero_write_replay_proved": True,
                    "write_counts": {
                        "resolution_admission": int(outcome != "no_work"),
                        "resolution_apply_and_request": (
                            2 if outcome != "no_work" else 0
                        ),
                        "entity_bindings": bindings_created,
                        "entities": 0,
                        "claims": 0,
                        "projections": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "external_model_calls": 0,
                    "filesystem_writes": 0,
                }
            )
        )
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "auto_resolution_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "claims": 0,
                    "projections": 0,
                    "qdrant": 0,
                    "prompt_influence": 0,
                    "filesystem_writes": 0,
                }
            ),
            file=os.sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
