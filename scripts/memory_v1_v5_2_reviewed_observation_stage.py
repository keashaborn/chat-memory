#!/usr/bin/env python3
"""Admit fully reviewed V5.2 batches to the governed entailment funnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_2_reviewed_observation_stage_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_reviewed_observation_stage_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("8b3b2efb-b880-5a41-a97d-1e5d1dd872de")
POLICIES = {
    "atom_apply": "memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1",
    "reviewed_route": "memory_v1_v5_2_reviewed_route_stage_admission_policy_v1",
}


class ReviewedObservationStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Admit a bounded owner-fair batch of reviewed V5.2 observations."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--max-records", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.max_records <= 20:
        raise ReviewedObservationStageError("record limit must be between 1 and 20")
    if args.apply and os.getenv(
        "MEMORY_V1_V5_2_REVIEWED_OBSERVATION_STAGE_APPLY"
    ) != APPLY_ENABLE_TOKEN:
        raise ReviewedObservationStageError(
            "reviewed-observation stage capability is absent"
        )


def operation_ids(
    owner: uuid.UUID, target: dict[str, Any]
) -> tuple[uuid.UUID, uuid.UUID]:
    source_kind = str(target["source_kind"])
    policy = POLICIES.get(source_kind)
    if policy is None:
        raise ReviewedObservationStageError("planner returned an unknown source kind")
    identity = "|".join(
        (
            str(owner),
            str(target["route_event_id"]),
            str(target["atom_apply_id"] or ""),
            str(target["batch_id"]),
            str(target["stage_manifest_sha256"]),
            str(target["resolution_state_sha256"]),
            str(target["observation_state_sha256"]),
            policy,
        )
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
    )


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1($1)",
            limit,
        )
        txid = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid is not None:
        raise ReviewedObservationStageError("read-only planning assigned a txid")
    if len(rows) > limit:
        raise ReviewedObservationStageError("planner exceeded its row budget")
    return [dict(row) for row in rows]


def bounded_targets(
    plans: list[tuple[uuid.UUID, list[dict[str, Any]]]], max_records: int
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    selected: list[tuple[uuid.UUID, dict[str, Any]]] = []
    seen: set[tuple[uuid.UUID, str]] = set()
    depth = max((len(rows) for _, rows in plans), default=0)
    for index in range(depth):
        for owner, rows in plans:
            if index >= len(rows):
                continue
            row = rows[index]
            key = (owner, str(row["route_event_id"]))
            if key in seen:
                raise ReviewedObservationStageError(
                    "planner returned a duplicate route"
                )
            seen.add(key)
            selected.append((owner, row))
            if len(selected) == max_records:
                return selected
    return selected


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "route_event_id_sha256": sha256_text(str(row["route_event_id"])),
        "batch_id_sha256": sha256_text(str(row["batch_id"])),
        "source_kind": row["source_kind"],
        "observation_count": int(row["observation_count"]),
    }


async def register_targets(
    conn: asyncpg.Connection,
    targets: list[tuple[uuid.UUID, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values: list[tuple[Any, ...]] = []
    for owner, target in targets:
        admission_id, operation_id = operation_ids(owner, target)
        policy = POLICIES[str(target["source_kind"])]
        values.append(
            (
                owner,
                admission_id,
                operation_id,
                uuid.UUID(str(target["route_event_id"])),
                (
                    uuid.UUID(str(target["atom_apply_id"]))
                    if target["atom_apply_id"] is not None
                    else None
                ),
                uuid.UUID(str(target["batch_id"])),
                str(target["stage_manifest_sha256"]),
                str(target["resolution_state_sha256"]),
                str(target["observation_state_sha256"]),
                policy,
            )
        )
    sql = """
        SELECT * FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
          $1,$2,$3,$4,$5,$6,$7,$8,$9
        )
    """

    async def execute() -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        async with conn.transaction(isolation="serializable"):
            for value in values:
                owner, *parameters = value
                await conn.execute(
                    "SELECT set_config('app.user_id',$1,true)", str(owner)
                )
                result = await conn.fetchrow(sql, *parameters)
                if result is None:
                    raise ReviewedObservationStageError("register returned no row")
                outputs.append(dict(result))
        return outputs

    return await execute(), await execute()


def verify_results(
    targets: list[tuple[uuid.UUID, dict[str, Any]]],
    first: list[dict[str, Any]],
    replay: list[dict[str, Any]],
) -> None:
    if len(first) != len(targets) or len(replay) != len(targets):
        raise ReviewedObservationStageError("registration result count drifted")
    for (_, target), applied, repeated in zip(targets, first, replay, strict=True):
        expected_decision = {
            "atom_apply": "v5_2_atom_reviewed_stage",
            "reviewed_route": "v5_2_reviewed_route_stage",
        }[str(target["source_kind"])]
        if (
            applied["outcome"] != "applied"
            or int(applied["rows_written"]) != 2
            or applied["decision"] != expected_decision
            or repeated["outcome"] != "replayed"
            or int(repeated["rows_written"]) != 0
            or repeated["admission_id"] != applied["admission_id"]
            or repeated["route_event_id"] != applied["route_event_id"]
            or repeated["batch_id"] != applied["batch_id"]
        ):
            raise ReviewedObservationStageError("apply/replay invariant failed")


async def run() -> int:
    args = arguments()
    validate_arguments(args)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ReviewedObservationStageError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ReviewedObservationStageError("worker requires brains_app")
        plans = [
            (owner, await plan_owner(conn, owner, args.max_records))
            for owner in owners
        ]
        targets = bounded_targets(plans, args.max_records)
        base = {
            "worker_version": WORKER_VERSION,
            "apply": bool(args.apply),
            "plans": [sanitized_plan(owner, row) for owner, row in targets],
            "selected_records": len(targets),
            "local_model_calls": 0,
            "external_model_calls": 0,
            "claims": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
            "filesystem_writes": 0,
        }
        if not args.apply:
            print(stable_json({**base, "database_rows_created": 0}))
            return 0
        if not targets:
            print(
                stable_json(
                    {
                        **base,
                        "outcome": "no_work",
                        "database_rows_created": 0,
                        "zero_write_replay_proved": True,
                    }
                )
            )
            return 0
        first, replay = await register_targets(conn, targets)
        verify_results(targets, first, replay)
        print(
            stable_json(
                {
                    **base,
                    "outcome": "reviewed_observations_admitted",
                    "database_rows_created": 2 * len(targets),
                    "zero_write_replay_proved": True,
                    "write_counts": {
                        "packet_stage_admission": len(targets),
                        "reviewed_stage_admission": len(targets),
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
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
                    "outcome": "reviewed_observation_stage_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "local_model_calls": 0,
                    "external_model_calls": 0,
                    "claims": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
