#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from openai import OpenAI

from memory_v1_v5_2_reviewed_claim_projection_clone import (
    EXPECTED_CLAIMS,
    claim_snapshot,
    load_plan,
    set_actor,
)
from rag_engine.memory_v1_projection import (
    ClaimVectorIndex,
    render_claim_for_embedding,
)
from rag_engine.memory_v1_v5_shadow_loader import load_v5_shadow_claims
from rag_engine.memory_v1_v5_shadow_trace import build_v5_shadow_trace
from rag_engine.qdrant_compat import make_qdrant_client


REPORT_CONTRACT = "memory_v1_v5_2_reviewed_claim_projection_shadow_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
COLLECTION_PREFIX = "memory_claim_v1_shadow_reviewed_"
QUERY_BY_PREDICATE = {
    "age.reported": "What age have I reported?",
    "health.user_reported_observation": "What health information have I reported?",
    "identity.name": "What names have I told you?",
    "pet.breed": "What breeds have I mentioned for my pets?",
    "pet.coat_color": "What coat colors have I mentioned for my pets?",
    "pet.eye_color": "What eye colors have I mentioned for my pets?",
    "pet.hearing_status": "What hearing information have I mentioned for my pets?",
    "pet.sex": "What sex have I reported for my pets?",
    "pet.weight_reported": "What weights have I reported for my pets?",
    "relationship.sibling_of": "What have I told you about my siblings?",
}


class ShadowProjectionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "replay"), required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection", required=True)
    return parser.parse_args()


def private_output(value: str) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents or path.exists():
        raise ShadowProjectionError("output must be a new private review artifact")
    return path


async def verify_cross_owner_read(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    other_owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> dict[str, int]:
    claim_ids = [uuid.UUID(item["claim_id"]) for item in items]
    async with conn.transaction(readonly=True, isolation="serializable"):
        await set_actor(conn, other_owner)
        visible_claims = await conn.fetchval(
            "SELECT count(*) FROM memory.claim WHERE owner_user_id=$1 AND claim_id=ANY($2::uuid[])",
            owner,
            claim_ids,
        )
        visible_outbox = await conn.fetchval(
            """
            SELECT count(*) FROM memory.projection_outbox
            WHERE owner_user_id=$1 AND aggregate_id=ANY($2::uuid[])
            """,
            owner,
            claim_ids,
        )
    if visible_claims != 0 or visible_outbox != 0:
        raise ShadowProjectionError("cross-owner Postgres read exposed target")
    return {
        "cross_owner_claim_count": visible_claims,
        "cross_owner_outbox_count": visible_outbox,
    }


async def validate_snapshots(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    async with conn.transaction(readonly=True, isolation="serializable"):
        await set_actor(conn, owner)
        for item in items:
            snapshots[item["claim_id"]] = await claim_snapshot(
                conn, owner, item
            )
    return snapshots


def load_points(
    qdrant: Any,
    *,
    collection: str,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], list[dict[str, Any]]]:
    points = qdrant.retrieve(
        collection_name=collection,
        ids=[item["claim_id"] for item in items],
        with_payload=True,
        with_vectors=True,
    )
    by_id = {str(point.id): point for point in points}
    if set(by_id) != {item["claim_id"] for item in items}:
        raise ShadowProjectionError("shadow point set differs from plan")
    vectors: dict[str, list[float]] = {}
    state: list[dict[str, Any]] = []
    for item in items:
        point = by_id[item["claim_id"]]
        payload = point.payload or {}
        vector = point.vector
        if (
            payload.get("owner_user_id") != str(owner)
            or payload.get("claim_id") != item["claim_id"]
            or payload.get("predicate") != item["predicate"]
            or payload.get("revision_number") != item["revision_number"]
            or payload.get("status") != "supported"
            or payload.get("schema_version") != "memory_claim_projection_v1"
            or not isinstance(vector, list)
            or len(vector) != 3072
            or any(not math.isfinite(float(value)) for value in vector)
        ):
            raise ShadowProjectionError("shadow point contract differs")
        vectors[item["claim_id"]] = [float(value) for value in vector]
        state.append(
            {
                "claim_id": item["claim_id"],
                "predicate": item["predicate"],
                "revision_number": item["revision_number"],
                "payload_sha256": hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "vector_sha256": hashlib.sha256(
                    json.dumps(vector, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
    return vectors, sorted(state, key=lambda item: item["claim_id"])


async def shadow_tests(
    conn: asyncpg.Connection,
    index: ClaimVectorIndex,
    owner: uuid.UUID,
    other_owner: uuid.UUID,
    items: list[dict[str, Any]],
    vectors: dict[str, list[float]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        vector = vectors[item["claim_id"]]
        owner_hits = index.search_claims(owner, vector, limit=24)
        if item["claim_id"] not in {hit["claim_id"] for hit in owner_hits}:
            raise ShadowProjectionError("owner shadow search missed target")
        other_hits = index.search_claims(other_owner, vector, limit=24)
        if item["claim_id"] in {hit["claim_id"] for hit in other_hits}:
            raise ShadowProjectionError("cross-owner Qdrant search exposed target")
        other_records = await load_v5_shadow_claims(
            conn, other_owner, [item["claim_id"]]
        )
        if other_records:
            raise ShadowProjectionError("cross-owner Postgres load exposed target")
        trace = await build_v5_shadow_trace(
            conn,
            index,
            owner,
            query=QUERY_BY_PREDICATE[item["predicate"]],
            query_vector=vector,
            context={
                "eligible": True,
                "reason": "isolated_reviewed_claim_projection_shadow",
                "intent": "specific_recall",
                "domain": "personal",
                "allowed_predicate_prefixes": [item["predicate"]],
                "explicit_recall": True,
            },
            request_id=None,
            thread_id=None,
            candidate_limit=24,
            max_claims=4,
            max_tokens=500,
            max_sensitivity="restricted",
        )
        if (
            trace.get("status") != "ok"
            or int(trace.get("selected_count") or 0) < 1
            or trace.get("prompt_injection")
            or trace.get("answer_model_exposure")
            or trace.get("retrieval_activation")
            or trace.get("database_writes") != 0
            or trace.get("qdrant_writes") != 0
        ):
            raise ShadowProjectionError("shadow trace was not read-only")
        results.append(
            {
                "claim_id": item["claim_id"],
                "predicate": item["predicate"],
                "owner_candidate_count": len(owner_hits),
                "other_owner_candidate_count": len(other_hits),
                "other_owner_database_record_count": len(other_records),
                "selected_count": trace["selected_count"],
                "candidate_set_sha256": trace["candidate_set_sha256"],
                "selection_set_sha256": trace["selection_set_sha256"],
                "prompt_influence": False,
            }
        )
    return results


async def run() -> int:
    args = arguments()
    plan_path = Path(args.plan).resolve()
    output = private_output(args.output)
    if (
        not plan_path.is_file()
        or stat.S_IMODE(plan_path.stat().st_mode) & 0o002
        or not args.collection.startswith(COLLECTION_PREFIX)
    ):
        raise ShadowProjectionError("plan or isolated collection boundary is invalid")
    plan = load_plan(plan_path)
    items = plan["items"]
    owner = uuid.UUID(plan["owner_user_id"])
    other_owner = uuid.UUID(plan["other_owner_user_id"])
    projection = plan["projection"]
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise ShadowProjectionError("runtime configuration is missing")
    gate = (
        "MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_PROJECTION"
        if args.mode == "apply"
        else "MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_REPLAY"
    )
    if os.environ.get(gate) != "authorized":
        raise ShadowProjectionError("shadow projection gate is closed")

    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    existing_names = {
        item.name for item in qdrant.get_collections().collections
    }
    if args.mode == "apply" and args.collection in existing_names:
        raise ShadowProjectionError("shadow collection already exists")
    if args.mode == "replay" and args.collection not in existing_names:
        raise ShadowProjectionError("shadow replay collection is absent")
    index = ClaimVectorIndex(
        qdrant, collection_name=args.collection, vector_size=3072
    )
    if args.mode == "apply":
        index.ensure_collection()
    collection_info = qdrant.get_collection(args.collection)
    if getattr(collection_info.config.params.vectors, "size", None) != 3072:
        raise ShadowProjectionError("shadow vector size differs")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    calls = 0
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ShadowProjectionError("DSN must authenticate as brains_app")
        snapshots = await validate_snapshots(conn, owner, items)
        isolation = await verify_cross_owner_read(
            conn, owner, other_owner, items
        )
        if args.mode == "apply":
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise ShadowProjectionError("OPENAI_API_KEY is required")
            provider = OpenAI(
                api_key=api_key,
                base_url=os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1",
                max_retries=0,
                timeout=30.0,
            )
            try:
                for item in items:
                    if calls >= projection["maximum_embedding_requests"]:
                        raise ShadowProjectionError("embedding request budget exhausted")
                    text = render_claim_for_embedding(
                        snapshots[item["claim_id"]]
                    )
                    calls += 1
                    response = provider.embeddings.create(
                        model=projection["embedding_model"], input=text
                    )
                    vector = [
                        float(value) for value in response.data[0].embedding
                    ]
                    if len(vector) != 3072 or any(
                        not math.isfinite(value) for value in vector
                    ):
                        raise ShadowProjectionError(
                            "provider returned an invalid embedding vector"
                        )
                    index.upsert_claim(
                        owner, snapshots[item["claim_id"]], vector
                    )
            except Exception:
                qdrant.delete_collection(args.collection)
                raise
            if calls != EXPECTED_CLAIMS:
                raise ShadowProjectionError("exact embedding budget was not consumed")
        vectors, point_state = load_points(
            qdrant,
            collection=args.collection,
            owner=owner,
            items=items,
        )
        shadows = await shadow_tests(
            conn, index, owner, other_owner, items, vectors
        )
        report = {
            "contract_version": REPORT_CONTRACT,
            "mode": args.mode,
            "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "owner_user_id": str(owner),
            "collection": args.collection,
            "claim_count": EXPECTED_CLAIMS,
            "embedding_requests": calls,
            "automatic_http_retries": 0,
            "qdrant_writes": EXPECTED_CLAIMS if args.mode == "apply" else 0,
            "production_collection_writes": 0,
            "database_writes": 0,
            "answer_generation": False,
            "prompt_influence": False,
            "point_state": point_state,
            "cross_owner": isolation,
            "shadow_tests": shadows,
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
