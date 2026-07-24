#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import asyncpg

from rag_engine.memory_v1_v5_shadow_loader import load_v5_shadow_claims
from rag_engine.memory_v1_v5_shadow_trace import run_memory_v1_v5_shadow_trace
from rag_engine.qdrant_compat import make_qdrant_client


CONTRACT = "memory_v1_v5_ordinary_stance_shadow_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM_ID = "8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"
COLLECTION = "memory_claim_v1"
QUERY = "What have I said about worrying about the future?"


class OrdinaryShadowVerifyError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def output_path(value: str) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents or path.exists():
        raise OrdinaryShadowVerifyError(
            "output must be a new private review artifact"
        )
    return path


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def text_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


async def other_owner_target_count(dsn: str) -> int:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        await conn.execute(
            "SELECT set_config('app.user_id',$1,false)", OTHER_OWNER
        )
        async with conn.transaction(readonly=True, isolation="serializable"):
            rows = await load_v5_shadow_claims(
                conn, OTHER_OWNER, [CLAIM_ID]
            )
        return len(rows)
    finally:
        await conn.close()


def sanitized_trace(trace: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "status",
        "outcome_code",
        "intent",
        "domain",
        "candidate_set_sha256",
        "selection_set_sha256",
        "candidate_count",
        "visible_candidate_count",
        "selected_count",
        "token_estimate",
        "rejected_counts",
        "database_writes",
        "qdrant_writes",
        "trace_writes",
        "prompt_injection",
        "answer_model_exposure",
        "retrieval_activation",
    )
    return {field: trace.get(field) for field in fields}


def main() -> int:
    output = output_path(arguments().output)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise OrdinaryShadowVerifyError(
            "POSTGRES_DSN and QDRANT_URL are required"
        )

    os.environ["MEMORY_V1_V5_SHADOW"] = "1"
    os.environ["MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED"] = "1"
    os.environ["MEMORY_V1_V5_SHADOW_MAX_SENSITIVITY"] = "medium"
    os.environ["MEMORY_V1_V5_SHADOW_EXPLICIT_MAX_SENSITIVITY"] = "high"

    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    points = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[CLAIM_ID],
        with_payload=True,
        with_vectors=True,
    )
    if len(points) != 1 or points[0].vector is None:
        raise OrdinaryShadowVerifyError("target projection is missing")
    point = points[0]
    vector = [float(item) for item in point.vector]
    point_before = stable_hash(
        {
            "id": str(point.id),
            "payload": dict(point.payload or {}),
            "vector": vector,
        }
    )

    owner_trace = run_memory_v1_v5_shadow_trace(
        OWNER,
        query=QUERY,
        request_classification="SPECIFIC_RECALL",
        query_vector=vector,
    )
    other_trace = run_memory_v1_v5_shadow_trace(
        OTHER_OWNER,
        query=QUERY,
        request_classification="SPECIFIC_RECALL",
        query_vector=vector,
    )
    other_target_count = asyncio.run(other_owner_target_count(dsn))

    points_after = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[CLAIM_ID],
        with_payload=True,
        with_vectors=True,
    )
    qdrant.close()
    point_after = stable_hash(
        {
            "id": str(points_after[0].id),
            "payload": dict(points_after[0].payload or {}),
            "vector": [float(item) for item in points_after[0].vector],
        }
    )
    expected_selection = stable_hash([CLAIM_ID])
    gates = {
        "owner_status_ok": owner_trace.get("status") == "ok",
        "ordinary_domain_stance_recall": (
            owner_trace.get("domain") == "stance_recall"
        ),
        "ordinary_intent_personal_recall": (
            owner_trace.get("intent") == "personal_recall"
        ),
        "exact_target_selected": (
            owner_trace.get("selection_set_sha256") == expected_selection
        ),
        "selected_one": owner_trace.get("selected_count") == 1,
        "owner_database_zero_write": (
            owner_trace.get("database_writes") == 0
        ),
        "owner_qdrant_zero_write": owner_trace.get("qdrant_writes") == 0,
        "owner_prompt_injection_false": (
            owner_trace.get("prompt_injection") is False
        ),
        "owner_answer_exposure_false": (
            owner_trace.get("answer_model_exposure") is False
        ),
        "owner_retrieval_activation_false": (
            owner_trace.get("retrieval_activation") is False
        ),
        "other_owner_target_not_selected": (
            other_trace.get("selection_set_sha256") != expected_selection
        ),
        "other_owner_target_not_visible_in_postgres": (
            other_target_count == 0
        ),
        "qdrant_unchanged": point_after == point_before,
    }
    failed_gates = sorted(
        name for name, passed in gates.items() if not passed
    )
    result = {
        "contract_version": CONTRACT,
        "status": "pass" if not failed_gates else "fail",
        "owner_user_id_sha256": text_hash(OWNER),
        "other_owner_user_id_sha256": text_hash(OTHER_OWNER),
        "target_claim_id_sha256": text_hash(CLAIM_ID),
        "query_sha256": text_hash(QUERY),
        "owner_trace": sanitized_trace(owner_trace),
        "other_trace": sanitized_trace(other_trace),
        "other_owner_target_row_count": other_target_count,
        "qdrant_point_sha256_before": point_before,
        "qdrant_point_sha256_after": point_after,
        "gates": gates,
        "failed_gates": failed_gates,
        "external_model_calls": 0,
        "production_writes": 0,
    }
    result["result_sha256"] = stable_hash(result)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(
        json.dumps(
            {
                "status": result["status"],
                "owner_intent": owner_trace.get("intent"),
                "owner_domain": owner_trace.get("domain"),
                "owner_selected_count": owner_trace.get("selected_count"),
                "other_target_rows": other_target_count,
                "failed_gates": failed_gates,
                "result": str(output),
                "result_sha256": result["result_sha256"],
            },
            sort_keys=True,
        )
    )
    if failed_gates:
        raise OrdinaryShadowVerifyError(
            "ordinary stance shadow gates failed:"
            + ",".join(failed_gates)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
