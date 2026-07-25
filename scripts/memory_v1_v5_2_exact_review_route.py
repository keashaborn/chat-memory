#!/usr/bin/env python3
"""Transactionally route an exact bounded set of reviewed V5.2 packets."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_2_local_packet_router import (
    DEFAULT_BUILDER,
    DEFAULT_REVIEW_ROOT,
    artifact_paths,
    build_artifacts,
    plan_owner,
    secure_review_root,
    stable_ids,
    validate_artifacts,
)
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_2_exact_review_route_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_exact_review_route_apply_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review and route exactly two owner-scoped V5.2 packets in one "
            "database transaction. Never stages, promotes, retrieves, writes "
            "Qdrant, or influences prompts."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", action="append", required=True)
    parser.add_argument("--review-builder", default=str(DEFAULT_BUILDER))
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def exact_packet_ids(values: list[str]) -> list[uuid.UUID]:
    try:
        packet_ids = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("exact packet allowlist contains an invalid UUID") from exc
    if len(packet_ids) != 2:
        raise RuntimeError("exact review routing requires two unique packet IDs")
    return packet_ids


def sanitized_plan(packet_id: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "packet_id_sha256": sha256_text(str(packet_id)),
        "route": row["route"] if row else "no_work",
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


async def record_review_in_transaction(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    operation_id, event_id = stable_ids(
        owner=owner,
        packet_id=packet_id,
        routing_basis_sha256=target["routing_basis_sha256"],
        report_sha256=artifact["report_sha256"],
        bundle_sha256=artifact["bundle_sha256"],
    )
    row = await conn.fetchrow(
        """
        SELECT * FROM memory.record_owner_v5_2_review_route_v1(
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
        )
        """,
        operation_id,
        event_id,
        packet_id,
        target["packet_storage_sha256"],
        target["routing_basis_sha256"],
        artifact["review_id"],
        artifact["request_id"],
        artifact["report_sha256"],
        artifact["bundle_sha256"],
        artifact["repository_commit"],
        artifact["counts"]["auto_link_eligible"],
        artifact["counts"]["manual_review_required"],
        artifact["counts"]["deferred"],
        artifact["counts"]["rejected"],
        artifact["blocking_code_count"],
    )
    if row is None:
        raise RuntimeError("exact V5.2 review route returned no row")
    return dict(row)


async def run() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    packet_ids = exact_packet_ids(args.packet_id)
    if args.apply and os.getenv("MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("exact V5.2 review route capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    root = secure_review_root(args.review_root)
    builder = Path(args.review_builder).resolve(strict=True)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    created_paths: list[Path] = []
    routes_committed = False
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("exact V5.2 review route requires brains_app session")
        targets = [
            await plan_owner(conn, owner, packet_id)
            for packet_id in packet_ids
        ]
        plans = [
            sanitized_plan(packet_id, target)
            for packet_id, target in zip(packet_ids, targets, strict=True)
        ]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "owner_user_id_sha256": sha256_text(str(owner)),
                        "plans": plans,
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
        if any(
            target is None or target["route"] != "manual_review_artifact_ready"
            for target in targets
        ):
            raise RuntimeError("an exact packet is not review-route eligible")

        artifacts: list[dict[str, Any]] = []
        for packet_id in packet_ids:
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
            artifacts.append(
                validate_artifacts(
                    root=root,
                    report_path=report_path,
                    bundle_path=bundle_path,
                    owner=owner,
                    packet_id=packet_id,
                )
            )

        applied: list[dict[str, Any]] = []
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            for target, artifact in zip(targets, artifacts, strict=True):
                assert target is not None
                applied.append(
                    await record_review_in_transaction(
                        conn,
                        owner=owner,
                        target=target,
                        artifact=artifact,
                    )
                )
            if any(row["apply_outcome"] != "applied" for row in applied):
                raise RuntimeError("exact V5.2 route transaction was not fully applied")
        routes_committed = True

        replayed: list[dict[str, Any]] = []
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            for target, artifact in zip(targets, artifacts, strict=True):
                assert target is not None
                replayed.append(
                    await record_review_in_transaction(
                        conn,
                        owner=owner,
                        target=target,
                        artifact=artifact,
                    )
                )
        if any(row["apply_outcome"] != "replayed" for row in replayed):
            raise RuntimeError("exact V5.2 route replay wrote again")

        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": "manual_review_artifacts_ready",
                    "owner_user_id_sha256": sha256_text(str(owner)),
                    "plans": plans,
                    "review_resolution_counts": [
                        artifact["counts"] for artifact in artifacts
                    ],
                    "write_counts": {
                        "route_events": len(applied),
                        "restricted_review_artifacts": len(created_paths),
                        "stage": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "transactional_apply_proved": True,
                    "zero_write_replay_proved": True,
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
                    "outcome": "exact_review_route_error",
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
