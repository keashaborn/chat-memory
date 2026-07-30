#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from qdrant_client.http import models as qmodels

from memory_v1_v5_2_reviewed_claim_projection_clone import (
    EXPECTED_CLAIMS,
    admit_exact_jobs,
    load_plan,
    set_actor,
    verify_cross_owner,
)
from memory_v1_v5_2_reviewed_claim_projection_shadow import (
    COLLECTION_PREFIX,
    load_points,
    shadow_tests,
)
from rag_engine.memory_v1_projection import ClaimVectorIndex
from rag_engine.qdrant_compat import make_qdrant_client


REPORT_CONTRACT = "memory_v1_v5_2_reviewed_claim_projection_activation_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
CLONE_TARGET_PREFIX = "memory_claim_v1_activation_clone_"
LIVE_COLLECTION = "memory_claim_v1"


class ActivationError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "replay"), required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-collection", required=True)
    parser.add_argument("--target-collection", required=True)
    return parser.parse_args()


def private_output(value: str) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents or path.exists():
        raise ActivationError("output must be a new private review artifact")
    return path


def validate_target_baseline(
    qdrant: Any, items: list[dict[str, Any]], target_collection: str
) -> None:
    points = qdrant.retrieve(
        collection_name=target_collection,
        ids=[item["claim_id"] for item in items],
        with_payload=True,
        with_vectors=True,
    )
    by_id = {str(point.id): point for point in points}
    expected = {
        item["claim_id"] for item in items if item["prior_qdrant"] == "revision_2"
    }
    if set(by_id) != expected:
        raise ActivationError("target projection baseline differs from plan")
    for item in items:
        point = by_id.get(item["claim_id"])
        if point is None:
            continue
        payload = point.payload or {}
        if (
            payload.get("owner_user_id")
            != "1240822d-ac9a-4096-95aa-e2b24d36ef50"
            or payload.get("claim_id") != item["claim_id"]
            or payload.get("predicate") != item["predicate"]
            or payload.get("revision_number") != 2
            or not isinstance(point.vector, list)
            or len(point.vector) != 3072
        ):
            raise ActivationError("target replacement baseline is invalid")


async def lease_exact_jobs(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
    outbox_ids: dict[str, str],
) -> dict[str, str]:
    leases: dict[str, str] = {}
    async with conn.transaction(isolation="serializable"):
        await set_actor(conn, owner)
        for item in items:
            row = await conn.fetchrow(
                """
                UPDATE memory.projection_outbox
                SET status='processing'::memory.outbox_status,
                    attempts=attempts+1,last_error=NULL,
                    lease_token=gen_random_uuid(),
                    lease_expires_at=clock_timestamp()+interval '10 minutes',
                    worker_id='controlled-reviewed-claim-activation',
                    updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND outbox_id=$2
                  AND aggregate_type='claim' AND aggregate_id=$3
                  AND operation='upsert' AND status='pending' AND attempts=0
                RETURNING lease_token,attempts,payload
                """,
                owner,
                uuid.UUID(outbox_ids[item["claim_id"]]),
                uuid.UUID(item["claim_id"]),
            )
            payload = row["payload"] if row else None
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                row is None
                or row["attempts"] != 1
                or payload
                != {
                    "claim_id": item["claim_id"],
                    "revision_number": item["revision_number"],
                }
            ):
                raise ActivationError("exact projection lease failed")
            leases[item["claim_id"]] = str(row["lease_token"])
    return leases


async def finalize_exact_jobs(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
    outbox_ids: dict[str, str],
    leases: dict[str, str],
) -> None:
    async with conn.transaction(isolation="serializable"):
        await set_actor(conn, owner)
        for item in items:
            command = await conn.execute(
                """
                UPDATE memory.projection_outbox
                SET status='done'::memory.outbox_status,last_error=NULL,
                    lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,
                    updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND outbox_id=$2 AND aggregate_id=$3
                  AND status='processing' AND lease_token=$4
                """,
                owner,
                uuid.UUID(outbox_ids[item["claim_id"]]),
                uuid.UUID(item["claim_id"]),
                uuid.UUID(leases[item["claim_id"]]),
            )
            if command != "UPDATE 1":
                raise ActivationError("exact projection finalization failed")


async def verify_postgres_current(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> None:
    async with conn.transaction(readonly=True, isolation="serializable"):
        await set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT aggregate_id,status::text,attempts,payload
            FROM memory.projection_outbox
            WHERE owner_user_id=$1 AND aggregate_id=ANY($2::uuid[])
            ORDER BY aggregate_id
            """,
            owner,
            [uuid.UUID(item["claim_id"]) for item in items],
        )
    by_id = {str(row["aggregate_id"]): row for row in rows}
    if set(by_id) != {item["claim_id"] for item in items}:
        raise ActivationError("current outbox target set differs")
    for item in items:
        row = by_id[item["claim_id"]]
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (
            row["status"] != "done"
            or row["attempts"] != 1
            or payload
            != {
                "claim_id": item["claim_id"],
                "revision_number": item["revision_number"],
            }
        ):
            raise ActivationError("current outbox state differs")


def copy_shadow_points(
    qdrant: Any,
    *,
    source_collection: str,
    target_collection: str,
    items: list[dict[str, Any]],
) -> None:
    points = qdrant.retrieve(
        collection_name=source_collection,
        ids=[item["claim_id"] for item in items],
        with_payload=True,
        with_vectors=True,
    )
    by_id = {str(point.id): point for point in points}
    if set(by_id) != {item["claim_id"] for item in items}:
        raise ActivationError("source shadow point set differs")
    qdrant.upsert(
        collection_name=target_collection,
        wait=True,
        points=[
            qmodels.PointStruct(
                id=item["claim_id"],
                vector=[float(value) for value in by_id[item["claim_id"]].vector],
                payload=by_id[item["claim_id"]].payload or {},
            )
            for item in items
        ],
    )


async def run() -> int:
    args = arguments()
    plan_path = Path(args.plan).resolve()
    output = private_output(args.output)
    if (
        not plan_path.is_file()
        or stat.S_IMODE(plan_path.stat().st_mode) & 0o002
        or not args.source_collection.startswith(COLLECTION_PREFIX)
        or args.source_collection == args.target_collection
        or (
            args.target_collection != LIVE_COLLECTION
            and not args.target_collection.startswith(CLONE_TARGET_PREFIX)
        )
    ):
        raise ActivationError("activation collection boundary is invalid")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_V5_2_REVIEWED_CLAIM_ACTIVATION"
    ) != "authorized":
        raise ActivationError("activation apply gate is closed")
    if args.mode == "replay" and os.environ.get(
        "MEMORY_V1_V5_2_REVIEWED_CLAIM_ACTIVATION_REPLAY"
    ) != "authorized":
        raise ActivationError("activation replay gate is closed")
    if args.target_collection == LIVE_COLLECTION and os.environ.get(
        "MEMORY_V1_V5_2_REVIEWED_CLAIM_LIVE_ACTIVATION"
    ) != "authorized":
        raise ActivationError("live activation gate is closed")

    plan = load_plan(plan_path)
    items = plan["items"]
    owner = uuid.UUID(plan["owner_user_id"])
    other_owner = uuid.UUID(plan["other_owner_user_id"])
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise ActivationError("runtime configuration is missing")
    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    collections = {item.name for item in qdrant.get_collections().collections}
    if (
        args.source_collection not in collections
        or args.target_collection not in collections
    ):
        raise ActivationError("activation collection is absent")
    source_index = ClaimVectorIndex(
        qdrant, collection_name=args.source_collection, vector_size=3072
    )
    target_index = ClaimVectorIndex(
        qdrant, collection_name=args.target_collection, vector_size=3072
    )
    source_vectors, source_state = load_points(
        qdrant,
        collection=args.source_collection,
        owner=owner,
        items=items,
    )
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ActivationError("DSN must authenticate as brains_app")
        if args.mode == "apply":
            validate_target_baseline(qdrant, items, args.target_collection)
            outbox_ids = await admit_exact_jobs(conn, owner, items)
            leases = await lease_exact_jobs(conn, owner, items, outbox_ids)
            copy_shadow_points(
                qdrant,
                source_collection=args.source_collection,
                target_collection=args.target_collection,
                items=items,
            )
            await finalize_exact_jobs(conn, owner, items, outbox_ids, leases)
            qdrant_writes = EXPECTED_CLAIMS
            postgres_rows_mutated = EXPECTED_CLAIMS * 3
        else:
            qdrant_writes = 0
            postgres_rows_mutated = 0
        await verify_postgres_current(conn, owner, items)
        target_vectors, target_state = load_points(
            qdrant,
            collection=args.target_collection,
            owner=owner,
            items=items,
        )
        if target_state != source_state:
            raise ActivationError("live and shadow projection states differ")
        isolation = await verify_cross_owner(conn, owner, other_owner, items)
        shadows = await shadow_tests(
            conn, target_index, owner, other_owner, items, target_vectors
        )
        if source_vectors.keys() != target_vectors.keys():
            raise ActivationError("source and target vector sets differ")
        for claim_id in source_vectors:
            if source_vectors[claim_id] != target_vectors[claim_id]:
                raise ActivationError("source and target vector values differ")
        report = {
            "contract_version": REPORT_CONTRACT,
            "mode": args.mode,
            "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "owner_user_id": str(owner),
            "source_collection": args.source_collection,
            "target_collection": args.target_collection,
            "claim_count": EXPECTED_CLAIMS,
            "embedding_requests": 0,
            "external_model_calls": 0,
            "qdrant_writes": qdrant_writes,
            "postgres_rows_mutated": postgres_rows_mutated,
            "point_state": target_state,
            "cross_owner": isolation,
            "shadow_tests": shadows,
            "retrieval_configuration_changes": 0,
            "prompt_configuration_changes": 0,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        output.chmod(0o600)
    finally:
        await conn.close()
        qdrant.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
