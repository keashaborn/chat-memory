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

from memory_v1_projection_v5_contract_test import sha256
from rag_engine.memory_v1_projection import ClaimVectorIndex
from rag_engine.memory_v1_v5_shadow_loader import load_v5_shadow_claims
from rag_engine.memory_v1_v5_shadow_trace import build_v5_shadow_trace
from rag_engine.qdrant_compat import make_qdrant_client


CONTRACT = "memory_v1_v5_reconciled_stance_shadow_verify_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM_ID = "8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"
PREDICATE = "stance.reported"
COLLECTION = "memory_claim_v1"


class ShadowVerifyError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def output_path(value: str) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents or path.exists():
        raise ShadowVerifyError("output must be a new private review artifact")
    return path


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def run() -> int:
    args = arguments()
    output = output_path(args.output)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise ShadowVerifyError("POSTGRES_DSN and QDRANT_URL are required")
    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    index = ClaimVectorIndex(qdrant, collection_name=COLLECTION, vector_size=3072)
    points = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[CLAIM_ID],
        with_payload=True,
        with_vectors=True,
    )
    if len(points) != 1 or points[0].vector is None:
        raise ShadowVerifyError("target claim projection is missing")
    payload = dict(points[0].payload or {})
    if (
        payload.get("owner_user_id") != OWNER
        or payload.get("claim_id") != CLAIM_ID
        or payload.get("predicate") != PREDICATE
        or payload.get("status") != "supported"
        or payload.get("revision_number") != 2
    ):
        raise ShadowVerifyError("target claim projection payload differs")
    vector = [float(value) for value in points[0].vector]
    point_hash_before = stable_hash(
        {"id": str(points[0].id), "payload": payload, "vector": vector}
    )
    owner_hits = index.search_claims(OWNER, vector, limit=24)
    other_hits = index.search_claims(OTHER_OWNER, vector, limit=24)
    if CLAIM_ID not in {item["claim_id"] for item in owner_hits}:
        raise ShadowVerifyError("owner candidate search missed target claim")
    if CLAIM_ID in {item["claim_id"] for item in other_hits}:
        raise ShadowVerifyError("cross-owner candidate search exposed target claim")

    connection = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ShadowVerifyError("POSTGRES_DSN must authenticate as brains_app")
        await connection.execute("SELECT set_config('app.user_id',$1,false)", OWNER)
        trace = await build_v5_shadow_trace(
            connection,
            index,
            OWNER,
            query="What have I said about worrying about the future?",
            query_vector=vector,
            context={
                "eligible": True,
                "reason": "owner_only_reconciled_stance_shadow_verification",
                "intent": "specific_recall",
                "domain": "personal",
                "allowed_predicate_prefixes": [PREDICATE],
                "explicit_recall": True,
            },
            request_id=None,
            thread_id=None,
            candidate_limit=24,
            max_claims=4,
            max_tokens=500,
            max_sensitivity="restricted",
        )
        await connection.execute(
            "SELECT set_config('app.user_id',$1,false)", OTHER_OWNER
        )
        transaction = connection.transaction(readonly=True, isolation="serializable")
        await transaction.start()
        other_records = await load_v5_shadow_claims(
            connection, OTHER_OWNER, [CLAIM_ID]
        )
        await transaction.rollback()
    finally:
        await connection.close()

    expected_selection_sha256 = sha256([CLAIM_ID])
    if (
        trace.get("status") != "ok"
        or trace.get("selected_count", 0) < 1
        or trace.get("selection_set_sha256") != expected_selection_sha256
        or trace.get("prompt_injection")
        or trace.get("answer_model_exposure")
        or trace.get("retrieval_activation")
        or trace.get("database_writes") != 0
        or trace.get("qdrant_writes") != 0
        or other_records
    ):
        raise ShadowVerifyError("shadow trace did not remain isolated and read-only")
    points_after = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[CLAIM_ID],
        with_payload=True,
        with_vectors=True,
    )
    qdrant.close()
    point_hash_after = stable_hash(
        {
            "id": str(points_after[0].id),
            "payload": dict(points_after[0].payload or {}),
            "vector": [float(value) for value in points_after[0].vector],
        }
    )
    if point_hash_after != point_hash_before:
        raise ShadowVerifyError("target Qdrant point changed during read-only trace")

    result = {
        "contract_version": CONTRACT,
        "owner_user_id": OWNER,
        "claim_id": CLAIM_ID,
        "predicate": PREDICATE,
        "owner_candidate_count": len(owner_hits),
        "other_owner_candidate_count": len(other_hits),
        "other_owner_record_count": len(other_records),
        "selected_count": trace["selected_count"],
        "target_selected": True,
        "candidate_set_sha256": trace["candidate_set_sha256"],
        "selection_set_sha256": trace["selection_set_sha256"],
        "qdrant_point_sha256": point_hash_after,
        "qdrant_unchanged": True,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "retrieval_activation": False,
        "prompt_influence_activated": False,
        "answer_model_exposure": False,
    }
    result["result_sha256"] = sha256(result)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"selected_count={result['selected_count']}")
    print(f"other_owner_candidate_count={result['other_owner_candidate_count']}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
