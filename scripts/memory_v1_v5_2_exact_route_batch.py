#!/usr/bin/env python3
"""Atomically route one hash-bound manifest of exact V5.2 packets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_2_exact_review_route import (
    record_review_in_transaction,
)
from scripts.memory_v1_v5_2_exact_terminal_batch import (
    finalize_disposition_rows,
    finalize_terminal_rows,
    replay_rows,
)
from scripts.memory_v1_v5_2_local_packet_router import (
    DEFAULT_BUILDER,
    artifact_paths,
    build_artifacts,
    plan_owner,
    secure_review_root,
    validate_artifacts,
)
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_2_exact_route_batch_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_exact_route_batch_apply_v1"
CONTRACT_VERSION = "memory_v1_contextual_exact_23_route_v1"
MAX_PACKETS = 64
DISPOSITION_REASONS = frozenset(
    {"deferral_only_no_stage", "deferral_only_review_unresolved"}
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--review-root", required=True)
    parser.add_argument("--review-builder", default=str(DEFAULT_BUILDER))
    parser.add_argument("--other-owner-user-id", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_manifest(path: Path) -> tuple[uuid.UUID, list[dict[str, Any]], dict[str, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("exact route manifest contract differs")
    try:
        owner = uuid.UUID(value["owner_user_id"])
        packets = value["packets"]
        expected = value["expected_counts"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("exact route manifest structure is invalid") from exc
    if (
        not isinstance(packets, list)
        or not 1 <= len(packets) <= MAX_PACKETS
        or expected.get("packets") != len(packets)
        or expected.get("route_events") != len(packets)
        or expected.get("model_calls") != 0
    ):
        raise RuntimeError("exact route manifest bounds differ")
    packet_ids: set[uuid.UUID] = set()
    for item in packets:
        try:
            packet_id = uuid.UUID(item["packet_id"])
            uuid.UUID(item["job_id"])
            uuid.UUID(item["evidence_id"])
            storage_sha256 = item["packet_storage_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("exact route manifest packet is invalid") from exc
        if (
            packet_id in packet_ids
            or not isinstance(storage_sha256, str)
            or len(storage_sha256) != 64
            or any(character not in "0123456789abcdef" for character in storage_sha256)
        ):
            raise RuntimeError("exact route manifest packet is duplicated or invalid")
        packet_ids.add(packet_id)
    return owner, packets, expected


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_zero(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            "SELECT * FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1($1::uuid)",
            packet_id,
        )
    return dict(row) if row is not None else None


async def classify(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    packets: list[dict[str, Any]],
) -> tuple[
    dict[uuid.UUID, str],
    dict[uuid.UUID, tuple[str, str]],
    list[tuple[dict[str, Any], uuid.UUID]],
]:
    terminal: dict[uuid.UUID, str] = {}
    dispositions: dict[uuid.UUID, tuple[str, str]] = {}
    review: list[tuple[dict[str, Any], uuid.UUID]] = []
    for item in packets:
        packet_id = uuid.UUID(item["packet_id"])
        target = await plan_owner(conn, owner, packet_id)
        if target is not None:
            if target["packet_storage_sha256"] != item["packet_storage_sha256"]:
                raise RuntimeError("exact packet storage hash drifted")
            if target["route"] == "terminal_no_stage":
                terminal[packet_id] = item["packet_storage_sha256"]
            elif target["route"] == "manual_review_artifact_ready":
                review.append((target, packet_id))
            else:
                raise RuntimeError("exact packet main route differs")
            continue
        zero = await plan_zero(conn, owner, packet_id)
        if zero is None or zero["packet_storage_sha256"] != item["packet_storage_sha256"]:
            raise RuntimeError("exact packet has no governed route")
        reason = str(zero["reason_code"])
        if not reason.endswith("_v5_2") or reason[:-5] not in DISPOSITION_REASONS:
            raise RuntimeError("exact zero-atom reason differs")
        dispositions[packet_id] = (item["packet_storage_sha256"], reason[:-5])
    return terminal, dispositions, review


async def foreign_visible(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    packet_ids: list[uuid.UUID],
) -> int:
    visible = 0
    for packet_id in packet_ids:
        if await plan_owner(conn, owner, packet_id) is not None:
            visible += 1
            continue
        if await plan_zero(conn, owner, packet_id) is not None:
            visible += 1
    return visible


async def run() -> int:
    args = arguments()
    repository_root = Path(__file__).resolve().parents[1]
    if not (repository_root / ".git").exists():
        raise RuntimeError("exact route batch repository root is invalid")
    os.chdir(repository_root)
    manifest_path = Path(args.manifest).resolve(strict=True)
    owner, packets, expected = load_manifest(manifest_path)
    other_owner = uuid.UUID(args.other_owner_user_id)
    if owner == other_owner:
        raise RuntimeError("isolation owner must differ")
    root = secure_review_root(args.review_root)
    builder = Path(args.review_builder).resolve(strict=True)
    if args.apply and os.getenv("MEMORY_V1_V5_2_EXACT_ROUTE_BATCH_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("exact route batch apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    created_paths: list[Path] = []
    routes_committed = False
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("exact route batch requires brains_app")
        terminal, dispositions, review = await classify(
            conn, owner=owner, packets=packets
        )
        packet_ids = [uuid.UUID(item["packet_id"]) for item in packets]
        if await foreign_visible(conn, owner=other_owner, packet_ids=packet_ids):
            raise RuntimeError("cross-owner exact packet visibility detected")
        actual = {
            "standard_terminal_routes": len(terminal),
            "exact_zero_atom_routes": len(dispositions),
            "manual_review_routes": len(review),
        }
        if any(actual[key] != expected[key] for key in actual):
            raise RuntimeError("exact route manifest outcome counts drifted")
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "owner_user_id_sha256": sha256_text(str(owner)),
                        "packet_count": len(packets),
                        "route_counts": actual,
                        "database_writes": 0,
                        "filesystem_writes": 0,
                        "stage_writes": 0,
                        "claim_writes": 0,
                        "qdrant_writes": 0,
                        "external_model_calls": 0,
                        "prompt_influence": 0,
                    }
                )
            )
            return 0

        review_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for target, packet_id in review:
            report_path, bundle_path = artifact_paths(root, packet_id)
            created = build_artifacts(
                builder=builder,
                root=root,
                report_path=report_path,
                bundle_path=bundle_path,
                owner=owner,
                packet_id=packet_id,
            )
            if created:
                created_paths.extend((report_path, bundle_path))
            review_rows.append(
                (
                    target,
                    validate_artifacts(
                        root=root,
                        report_path=report_path,
                        bundle_path=bundle_path,
                        owner=owner,
                        packet_id=packet_id,
                    ),
                )
            )
        if len(created_paths) != expected["restricted_review_artifacts"]:
            raise RuntimeError("restricted review artifact count differs")

        async with conn.transaction():
            await set_actor(conn, owner)
            terminal_rows = await finalize_terminal_rows(
                conn, owner=owner, expected=terminal
            )
            disposition_rows = await finalize_disposition_rows(
                conn, owner=owner, expected=dispositions
            )
            applied_review = [
                await record_review_in_transaction(
                    conn,
                    owner=owner,
                    target=target,
                    artifact=artifact,
                )
                for target, artifact in review_rows
            ]
            if any(row["apply_outcome"] != "applied" for row in applied_review):
                raise RuntimeError("exact review route was not fully applied")
        routes_committed = True

        async with conn.transaction():
            await set_actor(conn, owner)
            await replay_rows(
                conn,
                owner=owner,
                terminal_rows=terminal_rows,
                disposition_rows=disposition_rows,
            )
            replayed_review = [
                await record_review_in_transaction(
                    conn,
                    owner=owner,
                    target=target,
                    artifact=artifact,
                )
                for target, artifact in review_rows
            ]
        if any(row["apply_outcome"] != "replayed" for row in replayed_review):
            raise RuntimeError("exact review replay wrote again")

        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": "exact_routes_applied",
                    "owner_user_id_sha256": sha256_text(str(owner)),
                    "packet_count": len(packets),
                    "route_counts": actual,
                    "write_counts": {
                        "route_events": len(packets),
                        "restricted_review_artifacts": len(created_paths),
                        "stage": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "transactional_apply_proved": True,
                    "zero_write_replay_proved": True,
                    "cross_owner_visible": 0,
                    "external_model_calls": 0,
                }
            )
        )
        return 0
    except BaseException:
        if not routes_committed:
            for path in created_paths:
                path.unlink(missing_ok=True)
        raise
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
                    "outcome": "exact_route_batch_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "stage_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "external_model_calls": 0,
                    "prompt_influence": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
