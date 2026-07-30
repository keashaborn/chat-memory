#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from typing import Any, Sequence

import asyncpg

from scripts.memory_v1_authenticated_owners import (
    resolve_authenticated_owners,
)
from scripts.memory_v1_legacy_context_rebind_v1 import (
    apply_record as apply_rebind_record,
    build_plan as build_rebind_plan,
    plan_contract as rebind_plan_contract,
)
from scripts.memory_v1_v5_local_inference_scheduler import loopback_dsn


WORKER_VERSION = "memory_v1_queue_reconciliation_v1"
APPLY_TOKEN = "memory_v1_queue_reconciliation_apply_v1"
SUPERSEDED_OPERATION_NAMESPACE = uuid.UUID(
    "39f188ac-6ce5-453f-a95b-7fe66b48be9e"
)
SUPERSEDED_RECONCILIATION_NAMESPACE = uuid.UUID(
    "ffeb9240-c851-49e0-8b0e-d0984c545f92"
)
ORPHAN_OPERATION_NAMESPACE = uuid.UUID(
    "1ccbe4f6-92c5-4302-a63a-3c2f4ba104a8"
)
ORPHAN_COMPLETION_NAMESPACE = uuid.UUID(
    "a430939f-234d-4250-8d8b-3070814d716e"
)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply owner-scoped queue reconciliation. "
            "Reports contain counts and hashes only."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 100:
        raise RuntimeError("queue reconciliation limit must be 1..100")
    if args.expected_plan_sha256 is not None and (
        len(args.expected_plan_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in args.expected_plan_sha256
        )
    ):
        raise RuntimeError("expected plan hash is invalid")
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires an expected plan hash")
    if (
        args.apply
        and os.getenv("MEMORY_V1_QUEUE_RECONCILIATION_APPLY")
        != APPLY_TOKEN
    ):
        raise RuntimeError("queue reconciliation apply capability is absent")


async def set_actor(
    connection: asyncpg.Connection,
    owner: uuid.UUID,
) -> None:
    await connection.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner),
    )


async def plan_superseded_owner(
    connection: asyncpg.Connection,
    owner: uuid.UUID,
    limit: int,
) -> list[dict[str, Any]]:
    async with connection.transaction(readonly=True):
        await set_actor(connection, owner)
        rows = await connection.fetch(
            """
            SELECT *
            FROM memory.plan_owner_context_superseded_v1($1)
            """,
            limit,
        )
    return [
        {
            **dict(row),
            "owner_user_id": owner,
        }
        for row in rows
    ]


async def build_superseded_plan(
    connection: asyncpg.Connection,
    owners: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for owner in owners:
        rows.extend(
            await plan_superseded_owner(connection, owner, limit)
        )
    rows.sort(
        key=lambda row: (
            row["source_created_at"],
            str(row["owner_user_id"]),
            str(row["source_job_id"]),
        )
    )
    return rows[:limit]


def superseded_contract(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "superseded",
        "owner_user_id": str(row["owner_user_id"]),
        "source_job_id": str(row["source_job_id"]),
        "source_evidence_id": str(row["source_evidence_id"]),
        "terminal_job_id": str(row["terminal_job_id"]),
        "terminal_evidence_id": str(row["terminal_evidence_id"]),
        "source_envelope_sha256": row["source_envelope_sha256"],
    }


async def plan_orphans_owner(
    connection: asyncpg.Connection,
    owner: uuid.UUID,
    limit: int,
) -> list[dict[str, Any]]:
    async with connection.transaction(readonly=True):
        await set_actor(connection, owner)
        rows = await connection.fetch(
            """
            SELECT *
            FROM memory.plan_owner_v5_local_orphan_v1($1)
            """,
            limit,
        )
    return [
        {
            **dict(row),
            "owner_user_id": owner,
        }
        for row in rows
    ]


async def build_orphan_plan(
    connection: asyncpg.Connection,
    owners: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for owner in owners:
        rows.extend(await plan_orphans_owner(connection, owner, limit))
    rows.sort(
        key=lambda row: (
            row["job_created_at"],
            str(row["owner_user_id"]),
            str(row["job_id"]),
        )
    )
    return rows[:limit]


def orphan_contract(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "orphan_recovery",
        "owner_user_id": str(row["owner_user_id"]),
        "job_id": str(row["job_id"]),
        "reservation_event_id": str(row["reservation_event_id"]),
        "evidence_content_sha256": row["evidence_content_sha256"],
    }


async def apply_superseded(
    connection: asyncpg.Connection,
    row: dict[str, Any],
) -> str:
    owner = row["owner_user_id"]
    await set_actor(connection, owner)
    operation_id = uuid.uuid5(
        SUPERSEDED_OPERATION_NAMESPACE,
        f"{owner}|{row['source_job_id']}|{row['terminal_job_id']}",
    )
    reconciliation_id = uuid.uuid5(
        SUPERSEDED_RECONCILIATION_NAMESPACE,
        f"{owner}|{row['source_job_id']}|{row['source_envelope_sha256']}",
    )
    values = (
        reconciliation_id,
        operation_id,
        row["source_job_id"],
        row["source_evidence_id"],
        row["terminal_job_id"],
        row["terminal_evidence_id"],
        row["source_envelope_sha256"],
    )
    applied = await connection.fetchrow(
        """
        SELECT * FROM memory.finalize_owner_context_superseded_v1(
          $1,$2,$3,$4,$5,$6,$7
        )
        """,
        *values,
    )
    replayed = await connection.fetchrow(
        """
        SELECT * FROM memory.finalize_owner_context_superseded_v1(
          $1,$2,$3,$4,$5,$6,$7
        )
        """,
        *values,
    )
    if (
        applied is None
        or applied["apply_outcome"] not in {"applied", "replayed"}
        or replayed is None
        or replayed["apply_outcome"] != "replayed"
        or replayed["rows_written"] != 0
    ):
        raise RuntimeError("context supersession replay failed")
    return str(applied["apply_outcome"])


async def apply_orphan(
    connection: asyncpg.Connection,
    row: dict[str, Any],
) -> str:
    owner = row["owner_user_id"]
    await set_actor(connection, owner)
    operation_id = uuid.uuid5(
        ORPHAN_OPERATION_NAMESPACE,
        f"{owner}|{row['job_id']}|{row['reservation_event_id']}",
    )
    completion_operation_id = uuid.uuid5(
        ORPHAN_COMPLETION_NAMESPACE,
        f"{owner}|{row['job_id']}|{row['reservation_event_id']}",
    )
    values = (
        operation_id,
        completion_operation_id,
        row["job_id"],
        row["reservation_event_id"],
        row["evidence_content_sha256"],
    )
    applied = await connection.fetchrow(
        """
        SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
          $1,$2,$3,$4,$5
        )
        """,
        *values,
    )
    replayed = await connection.fetchrow(
        """
        SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
          $1,$2,$3,$4,$5
        )
        """,
        *values,
    )
    if (
        applied is None
        or applied["apply_outcome"] not in {"applied", "replayed"}
        or replayed is None
        or replayed["apply_outcome"] != "replayed"
    ):
        raise RuntimeError("local orphan recovery replay failed")
    return str(applied["apply_outcome"])


async def run() -> int:
    args = arguments()
    validate_arguments(args)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(
        dsn,
        args.owner_user_id,
    )
    connection = await asyncpg.connect(
        loopback_dsn(dsn),
        command_timeout=180,
        ssl=False,
    )
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("queue reconciliation requires brains_app")

        superseded = await build_superseded_plan(
            connection, owners, args.limit
        )
        rebind = await build_rebind_plan(connection, owners, args.limit)
        orphans = await build_orphan_plan(connection, owners, args.limit)
        contracts = (
            [superseded_contract(row) for row in superseded]
            + [
                {
                    "operation": "context_rebind",
                    **rebind_plan_contract(row),
                }
                for row in rebind
            ]
            + [orphan_contract(row) for row in orphans]
        )
        contracts.sort(key=stable_json)
        plan_sha256 = sha256_text(stable_json(contracts))
        if (
            args.expected_plan_sha256 is not None
            and args.expected_plan_sha256 != plan_sha256
        ):
            raise RuntimeError("queue reconciliation plan changed")

        report: dict[str, Any] = {
            "worker_version": WORKER_VERSION,
            "apply": args.apply,
            "owner_count": len(owners),
            "candidate_count": len(contracts),
            "candidate_counts": {
                "superseded": len(superseded),
                "context_rebind": len(rebind),
                "orphan_recovery": len(orphans),
            },
            "plan_sha256": plan_sha256,
            "new_local_model_calls": 0,
            "external_model_calls": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
        }
        if not args.apply:
            report.update(
                {
                    "outcome": "planned",
                    "zero_write_replay_proved": True,
                }
            )
            print(stable_json(report))
            return 0

        outcomes: dict[str, list[str]] = {
            "superseded": [],
            "context_rebind": [],
            "orphan_recovery": [],
        }
        async with connection.transaction(isolation="serializable"):
            for row in superseded:
                outcomes["superseded"].append(
                    await apply_superseded(connection, row)
                )
            for row in rebind:
                result = await apply_rebind_record(connection, row)
                outcomes["context_rebind"].append(
                    str(result["finalize_outcome"])
                )
            for row in orphans:
                outcomes["orphan_recovery"].append(
                    await apply_orphan(connection, row)
                )

        applied_counts = {
            name: sum(outcome == "applied" for outcome in values)
            for name, values in outcomes.items()
        }
        if sum(len(values) for values in outcomes.values()) != len(contracts):
            raise RuntimeError("queue reconciliation write count changed")
        report.update(
            {
                "outcome": "applied",
                "applied_counts": applied_counts,
                "write_counts": {
                    "supersession_records": applied_counts["superseded"],
                    "rebound_evidence": applied_counts["context_rebind"],
                    "rebound_jobs": applied_counts["context_rebind"],
                    "rebind_links": applied_counts["context_rebind"],
                    "rebind_terminals": applied_counts["context_rebind"],
                    "orphan_recoveries": applied_counts["orphan_recovery"],
                },
                "zero_write_replay_proved": True,
            }
        )
        print(stable_json(report))
        return 0
    finally:
        await connection.close()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
