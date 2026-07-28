#!/usr/bin/env python3
"""Finalize an exact bounded set of V5.2 no-stage packets atomically."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_2_local_packet_router import stable_ids
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    operation_ids,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_2_exact_terminal_batch_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_exact_terminal_batch_apply_v1"
MAX_EXACT_PACKETS = 64
DISPOSITION_REASONS = frozenset(
    {"deferral_only_no_stage", "deferral_only_review_unresolved"}
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Finalize an exact owner-scoped set of V5.2 terminal routes and "
            "review-unresolved no-stage dispositions in one transaction."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--terminal-item", action="append", default=[])
    parser.add_argument("--disposition-item", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def exact_terminal_items(values: list[str]) -> dict[uuid.UUID, str]:
    result: dict[uuid.UUID, str] = {}
    for value in values:
        pieces = value.split(":")
        if len(pieces) != 2:
            raise RuntimeError("terminal item must be packet_uuid:storage_sha256")
        packet_id = uuid.UUID(pieces[0])
        storage_sha256 = pieces[1]
        if (
            packet_id in result
            or len(storage_sha256) != 64
            or any(character not in "0123456789abcdef" for character in storage_sha256)
        ):
            raise RuntimeError("terminal item is invalid or duplicated")
        result[packet_id] = storage_sha256
    return result


def exact_disposition_items(
    values: list[str],
) -> dict[uuid.UUID, tuple[str, str]]:
    result: dict[uuid.UUID, tuple[str, str]] = {}
    for value in values:
        pieces = value.split(":")
        if len(pieces) != 3:
            raise RuntimeError(
                "disposition item must be packet_uuid:storage_sha256:reason"
            )
        packet_id = uuid.UUID(pieces[0])
        storage_sha256, reason = pieces[1:]
        if (
            packet_id in result
            or len(storage_sha256) != 64
            or any(character not in "0123456789abcdef" for character in storage_sha256)
            or reason not in DISPOSITION_REASONS
        ):
            raise RuntimeError("disposition item is invalid or duplicated")
        result[packet_id] = (storage_sha256, reason)
    return result


def validate_batch(
    terminal: dict[uuid.UUID, str],
    dispositions: dict[uuid.UUID, tuple[str, str]],
) -> None:
    if not terminal and not dispositions:
        raise RuntimeError("at least one exact packet is required")
    if len(terminal) + len(dispositions) > MAX_EXACT_PACKETS:
        raise RuntimeError("exact terminal batch exceeds its bound")
    if set(terminal).intersection(dispositions):
        raise RuntimeError("a packet appears in both exact route lanes")


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def finalize_terminal_rows(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    expected: dict[uuid.UUID, str],
) -> list[dict[str, Any]]:
    remaining = set(expected)
    applied: list[dict[str, Any]] = []
    while remaining:
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_2_local_packet_route_v1($1)",
            25,
        )
        selected = [
            dict(row)
            for row in rows
            if row["packet_id"] in remaining and row["route"] == "terminal_no_stage"
        ]
        if not selected:
            raise RuntimeError("an exact V5.2 terminal packet is not currently eligible")
        # The database finalizer revalidates against the current first 25
        # planned rows. Applying one row changes that window, so use only the
        # first matching row from each fresh planner snapshot.
        for target in selected[:1]:
            packet_id = uuid.UUID(str(target["packet_id"]))
            if target["packet_storage_sha256"] != expected[packet_id]:
                raise RuntimeError("exact terminal packet storage hash drifted")
            operation_id, event_id = stable_ids(
                owner=owner,
                packet_id=packet_id,
                routing_basis_sha256=target["routing_basis_sha256"],
            )
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.finalize_owner_v5_2_terminal_route_v1(
                  $1,$2,$3,$4,$5,$6
                )
                """,
                operation_id,
                event_id,
                packet_id,
                target["packet_storage_sha256"],
                target["routing_basis_sha256"],
                list(target["source_deferral_reason_codes"]),
            )
            if row is None or row["apply_outcome"] != "applied":
                raise RuntimeError("exact V5.2 terminal packet was not applied")
            applied.append(
                {
                    "packet_id": packet_id,
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "target": target,
                }
            )
            remaining.remove(packet_id)
    return applied


async def finalize_disposition_rows(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    expected: dict[uuid.UUID, tuple[str, str]],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for packet_id in sorted(expected, key=str):
        storage_sha256, reason = expected[packet_id]
        operation_id, disposition_id = operation_ids(
            owner, packet_id, storage_sha256, reason
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.finalize_owner_v5_local_deferral_v1(
              $1,$2,$3,$4,$5
            )
            """,
            operation_id,
            disposition_id,
            packet_id,
            storage_sha256,
            reason,
        )
        if row is None or row["apply_outcome"] != "applied":
            raise RuntimeError("exact no-stage disposition was not applied")
        applied.append(
            {
                "packet_id": packet_id,
                "operation_id": operation_id,
                "disposition_id": disposition_id,
                "storage_sha256": storage_sha256,
                "reason": reason,
            }
        )
    return applied


async def replay_rows(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    terminal_rows: list[dict[str, Any]],
    disposition_rows: list[dict[str, Any]],
) -> None:
    for item in terminal_rows:
        target = item["target"]
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.finalize_owner_v5_2_terminal_route_v1(
              $1,$2,$3,$4,$5,$6
            )
            """,
            item["operation_id"],
            item["event_id"],
            item["packet_id"],
            target["packet_storage_sha256"],
            target["routing_basis_sha256"],
            list(target["source_deferral_reason_codes"]),
        )
        if row is None or row["apply_outcome"] != "replayed":
            raise RuntimeError("exact V5.2 terminal replay wrote again")
    for item in disposition_rows:
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.finalize_owner_v5_local_deferral_v1(
              $1,$2,$3,$4,$5
            )
            """,
            item["operation_id"],
            item["disposition_id"],
            item["packet_id"],
            item["storage_sha256"],
            item["reason"],
        )
        if row is None or row["apply_outcome"] != "replayed":
            raise RuntimeError("exact no-stage disposition replay wrote again")


async def execute(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    terminal: dict[uuid.UUID, str],
    dispositions: dict[uuid.UUID, tuple[str, str]],
    commit: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transaction = conn.transaction()
    await transaction.start()
    try:
        await set_actor(conn, owner)
        terminal_rows = await finalize_terminal_rows(
            conn, owner=owner, expected=terminal
        )
        disposition_rows = await finalize_disposition_rows(
            conn, owner=owner, expected=dispositions
        )
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()
        return terminal_rows, disposition_rows
    except BaseException:
        await transaction.rollback()
        raise


async def run() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    terminal = exact_terminal_items(args.terminal_item)
    dispositions = exact_disposition_items(args.disposition_item)
    validate_batch(terminal, dispositions)
    if args.apply and os.getenv("MEMORY_V1_V5_2_EXACT_TERMINAL_BATCH_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("exact terminal batch apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("exact terminal batch requires brains_app session")
        terminal_rows, disposition_rows = await execute(
            conn,
            owner=owner,
            terminal=terminal,
            dispositions=dispositions,
            commit=args.apply,
        )
        if args.apply:
            async with conn.transaction():
                await set_actor(conn, owner)
                await replay_rows(
                    conn,
                    owner=owner,
                    terminal_rows=terminal_rows,
                    disposition_rows=disposition_rows,
                )
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": args.apply,
                    "owner_user_id_sha256": sha256_text(str(owner)),
                    "packet_count": len(terminal) + len(dispositions),
                    "terminal_route_count": len(terminal_rows),
                    "disposition_count": len(disposition_rows),
                    "disposition_reason_counts": {
                        reason: sum(
                            1 for _storage, value in dispositions.values() if value == reason
                        )
                        for reason in sorted(DISPOSITION_REASONS)
                    },
                    "database_writes": (
                        len(terminal_rows) + len(disposition_rows)
                        if args.apply
                        else 0
                    ),
                    "stage_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "external_model_calls": 0,
                    "prompt_influence": 0,
                    "transactional_apply_proved": args.apply,
                    "zero_write_replay_proved": True,
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
                    "outcome": "exact_terminal_batch_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "stage_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "external_model_calls": 0,
                    "prompt_influence": 0,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
