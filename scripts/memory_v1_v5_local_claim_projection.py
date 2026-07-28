#!/usr/bin/env python3
"""Stage one accepted local observation as a manual-review claim plan."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    stable_json,
    validate_packet,
)
from memory_v1_v5_claim_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    load_source,
)
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json as stable_output,
)


WORKER_VERSION = "memory_v1_v5_local_claim_projection_v1"
POLICY_VERSION = "memory_v1_v5_local_claim_projection_policy_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_claim_projection_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("c545c915-b70d-5c7c-9504-1f3ad8868104")


class LocalClaimProjectionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def operation_ids(
    owner: uuid.UUID,
    assessment_id: uuid.UUID,
    observation_id: uuid.UUID,
    observation_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{assessment_id}|{observation_id}|"
        f"{observation_sha256}|{POLICY_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"plan|{identity}"),
    )


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_claim_projection_v1(1)"
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise LocalClaimProjectionError("read-only projection plan assigned a txid")
    if len(rows) > 1:
        raise LocalClaimProjectionError("projection planner exceeded row budget")
    return dict(rows[0]) if rows else None


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "observation_id_sha256": (
            sha256_text(str(row["observation_id"])) if row else None
        ),
        "route": "stage_manual_review_claim_plan" if row else "no_work",
    }


async def apply_once(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    assessment_id = uuid.UUID(str(target["assessment_id"]))
    observation_id = uuid.UUID(str(target["observation_id"]))
    admission_id, plan_id = operation_ids(
        owner,assessment_id,observation_id,str(target["observation_sha256"])
    )
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        source = await load_source(conn, observation_id)
        projection = build_projection(str(owner), source)
        packet = build_packet(projection)
        validate_packet(packet, str(owner), load_contract())
        packet_text = stable_json(packet)
        manifest = owner_manifest_sha256(str(owner), packet["packet_sha256"])
        first = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_claim_projection_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            admission_id,assessment_id,observation_id,plan_id,packet_text,
            packet["packet_sha256"],manifest,POLICY_VERSION,
        )
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        replay = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_claim_projection_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            admission_id,assessment_id,observation_id,plan_id,packet_text,
            packet["packet_sha256"],manifest,POLICY_VERSION,
        )
    if first is None or replay is None:
        raise LocalClaimProjectionError("projection register returned no row")
    return dict(first),dict(replay)


async def run() -> int:
    args = arguments()
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_CLAIM_PROJECTION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise LocalClaimProjectionError("claim projection capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalClaimProjectionError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    conn = await asyncpg.connect(loopback_dsn(dsn),command_timeout=60,ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalClaimProjectionError("projection worker requires brains_app")
        plans = [(owner,await plan_owner(conn,owner)) for owner in owners]
        selected = next(((owner,row) for owner,row in plans if row),None)
        safe_plans = [sanitized_plan(owner,row) for owner,row in plans]
        if not args.apply:
            print(stable_output({
                "worker_version":WORKER_VERSION,"apply":False,"plans":safe_plans,
                "database_writes":0,"external_model_calls":0,"local_model_calls":0,
                "claims":0,"qdrant":0,"prompt_influence":0,"filesystem_writes":0,
            }))
            return 0
        if selected is None:
            print(stable_output({
                "worker_version":WORKER_VERSION,"apply":True,"outcome":"no_work",
                "plans":safe_plans,"database_rows_created":0,
                "external_model_calls":0,"local_model_calls":0,"claims":0,
                "qdrant":0,"prompt_influence":0,"filesystem_writes":0,
                "zero_write_replay_proved":True,
            }))
            return 0
        owner,target=selected
        first,replay=await apply_once(conn,owner=owner,target=target)
        if (first["outcome"]!="applied" or int(first["rows_written"])!=5
            or replay["outcome"]!="replayed" or int(replay["rows_written"])!=0
            or first["plan_id"]!=replay["plan_id"]):
            raise LocalClaimProjectionError("projection apply/replay invariant failed")
        print(stable_output({
            "worker_version":WORKER_VERSION,"apply":True,
            "outcome":"manual_review_claim_plan_staged","plans":safe_plans,
            "database_rows_created":5,"zero_write_replay_proved":True,
            "write_counts":{"admission":1,"projection_plan_rows":4,
              "claims":0,"qdrant":0,"prompt_influence":0},
            "external_model_calls":0,"local_model_calls":0,"filesystem_writes":0,
        }))
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(stable_output({
            "worker_version":WORKER_VERSION,"apply":False,
            "outcome":"local_claim_projection_error",
            "error_class":type(exc).__name__,"error_sha256":sha256_text(str(exc)),
            "external_model_calls":0,"local_model_calls":0,"claims":0,
            "qdrant":0,"prompt_influence":0,
        }))
        return 1


if __name__=="__main__":
    raise SystemExit(guarded_main())
