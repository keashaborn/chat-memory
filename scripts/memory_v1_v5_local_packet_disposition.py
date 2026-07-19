#!/usr/bin/env python3
"""Plan or finalize one owner-scoped local V5 deferral-only packet."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from typing import Any, Sequence
from urllib.parse import urlparse
import uuid

import asyncpg


WORKER_VERSION = "memory_v1_v5_local_packet_disposition_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_packet_disposition_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("81b0cd6c-758a-52b2-9a16-094d6261ea45")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("local packet disposition DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("local packet disposition DSN must be loopback-only")
    return value


def canonical_owners(values: Sequence[str]) -> list[uuid.UUID]:
    if not values:
        raise RuntimeError("at least one explicit owner UUID is required")
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if len(owners) > 6:
        raise RuntimeError("owner allowlist exceeds six entries")
    return owners


def operation_ids(
    owner: uuid.UUID,
    packet_id: uuid.UUID,
    packet_storage_sha256: str,
    reason_code: str = "deferral_only_no_stage",
) -> tuple[uuid.UUID, uuid.UUID]:
    if reason_code not in {
        "deferral_only_no_stage",
        "deferral_only_review_unresolved",
    }:
        raise RuntimeError("unsupported terminal deferral reason code")
    identity = f"{owner}|{packet_id}|{packet_storage_sha256}|{reason_code}"
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"disposition|{identity}"),
    )


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Finalize at most one verified deferral-only local V5 packet. "
            "Output contains identifiers only as hashes."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            "SELECT * FROM memory.plan_owner_v5_local_packet_disposition_v1(1)"
        )
    return dict(row) if row else None


async def finalize(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    reason_code = str(target["reason_code"])
    operation_id, disposition_id = operation_ids(
        owner,
        packet_id,
        str(target["packet_storage_sha256"]),
        reason_code,
    )

    async def invoke() -> dict[str, Any]:
        async with conn.transaction():
            await set_actor(conn, owner)
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.finalize_owner_v5_local_deferral_v1(
                  $1,$2,$3,$4,$5
                )
                """,
                operation_id,
                disposition_id,
                packet_id,
                target["packet_storage_sha256"],
                reason_code,
            )
        if row is None:
            raise RuntimeError("local packet disposition returned no row")
        return dict(row)

    applied = await invoke()
    replayed = await invoke()
    return applied, replayed


async def run() -> int:
    args = arguments()
    owners = canonical_owners(args.owner_user_id)
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("local packet disposition apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("local packet disposition requires brains_app session")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        sanitized_plans = [
            {
                "owner_user_id_sha256": sha256_text(str(owner)),
                "packet_id_sha256": sha256_text(str(row["packet_id"])) if row else None,
                "route": row["disposition_route"] if row else "no_work",
                "reason_code": row["reason_code"] if row else None,
                "counts": (
                    {
                        "entity_mentions": row["entity_mention_count"],
                        "observations": row["observation_count"],
                        "comparison_hints": row["comparison_hint_count"],
                        "deferrals": row["deferral_count"],
                    }
                    if row
                    else None
                ),
            }
            for owner, row in plans
        ]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "plans": sanitized_plans,
                        "database_writes": 0,
                        "qdrant_writes": 0,
                        "external_model_calls": 0,
                        "prompt_influence": 0,
                    }
                )
            )
            return 0
        if selected is None:
            outcome = "no_work"
            write_count = 0
            replay_proved = True
        else:
            owner, target = selected
            if target["disposition_route"] != "terminal_deferral":
                outcome = "manual_review_pending"
                write_count = 0
                replay_proved = True
            else:
                applied, replayed = await finalize(conn, owner, target)
                if (
                    applied["apply_outcome"] not in {"applied", "replayed"}
                    or replayed["apply_outcome"] != "replayed"
                    or applied["disposition"] != "terminal_no_stage"
                ):
                    raise RuntimeError("local packet disposition replay failed")
                outcome = "terminal_no_stage"
                write_count = int(applied["apply_outcome"] == "applied")
                replay_proved = True
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": outcome,
                    "plans": sanitized_plans,
                    "write_counts": {
                        "dispositions": write_count,
                        "stage": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "zero_write_replay_proved": replay_proved,
                    "external_model_calls": 0,
                }
            )
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(run())


def guarded_main() -> int:
    try:
        return main()
    except Exception as exc:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "disposition_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "claims": 0,
                    "qdrant": 0,
                    "prompt_influence": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
