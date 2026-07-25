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

from memory_v1_projection_v5_contract_test import sha256
from rag_engine.memory_v1_projection import ClaimVectorIndex, projection_payload
from rag_engine.qdrant_compat import make_qdrant_client


CONTRACT = "memory_v1_v5_claim_projection_payload_repair_result_v1"
PLAN_CONTRACT = "memory_v1_v5_compiler_v8_four_claim_projection_plan_v1"
ADMISSION_CONTRACT = "memory_v1_v5_deferred_projection_admission_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class ProjectionPayloadRepairError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--admission-result", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection", default="memory_claim_v1")
    parser.add_argument("--vector-size", type=int, default=3072)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if output:
        if REVIEW_ROOT not in path.parents or path.exists():
            raise ProjectionPayloadRepairError("invalid private output path")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProjectionPayloadRepairError("input must be a private mode-0600 file")
    return path


def load_sources(
    plan_path: Path, admission_path: Path
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    plan = json.loads(plan_path.read_text())
    admission = json.loads(admission_path.read_text())
    items = plan.get("items")
    outcomes = admission.get("outcomes")
    if (
        plan.get("contract_version") != PLAN_CONTRACT
        or plan.get("owner_user_id") != OWNER
        or not isinstance(items, list)
        or len(items) != 4
        or admission.get("contract_version") != ADMISSION_CONTRACT
        or admission.get("mode") != "apply"
        or admission.get("owner_user_id") != OWNER
        or admission.get("rows_written") != 4
        or admission.get("qdrant_writes") != 0
        or not isinstance(outcomes, list)
        or len(outcomes) != 4
        or admission.get("result_sha256")
        != sha256(
            {
                key: value
                for key, value in admission.items()
                if key != "result_sha256"
            }
        )
    ):
        raise ProjectionPayloadRepairError("repair sources are outside the boundary")
    by_claim = {str(item.get("claim_id")): item for item in items}
    admitted = {str(item.get("claim_id")): item for item in outcomes}
    if len(by_claim) != 4 or by_claim.keys() != admitted.keys():
        raise ProjectionPayloadRepairError("plan and admission claim sets differ")
    normalized: dict[str, dict[str, Any]] = {}
    for claim_id, item in by_claim.items():
        admitted_item = admitted[claim_id]
        try:
            normalized_id = str(uuid.UUID(claim_id))
            outbox_id = str(uuid.UUID(admitted_item["outbox_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionPayloadRepairError("invalid repair identifier") from exc
        if (
            normalized_id != claim_id
            or item.get("revision_number") != 2
            or admitted_item.get("status") != "pending"
            or admitted_item.get("predicate") != item.get("predicate")
        ):
            raise ProjectionPayloadRepairError("repair item contract differs")
        normalized[claim_id] = {**item, "outbox_id": outbox_id}
    return plan, normalized


async def load_snapshot(
    conn: asyncpg.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """SELECT claim.claim_id,claim.canonical_text,claim.predicate,
                  claim.qualifiers,claim.status::text,claim.sensitivity::text,
                  claim.retrieval_policy,claim.updated_at,
                  COALESCE(max(revision.revision_number),0) AS revision_number,
                  outbox.status::text AS outbox_status,outbox.attempts,outbox.payload
           FROM memory.claim AS claim
           LEFT JOIN memory.claim_revision AS revision
             ON revision.owner_user_id=claim.owner_user_id
            AND revision.claim_id=claim.claim_id
           JOIN memory.projection_outbox AS outbox
             ON outbox.owner_user_id=claim.owner_user_id
            AND outbox.aggregate_type='claim'
            AND outbox.aggregate_id=claim.claim_id
            AND outbox.operation='upsert'
            AND outbox.outbox_id=$3
           WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
           GROUP BY claim.claim_id,outbox.outbox_id""",
        uuid.UUID(OWNER),
        uuid.UUID(item["claim_id"]),
        uuid.UUID(item["outbox_id"]),
    )
    payload = row["payload"] if row else None
    if isinstance(payload, str):
        payload = json.loads(payload)
    if (
        not row
        or row["status"] != "supported"
        or row["revision_number"] != 2
        or row["predicate"] != item["predicate"]
        or hashlib.sha256(row["canonical_text"].encode()).hexdigest()
        != item["canonical_text_sha256"]
        or row["outbox_status"] != "done"
        or row["attempts"] != 1
        or payload != {"claim_id": item["claim_id"], "revision_number": 2}
    ):
        raise ProjectionPayloadRepairError("repair snapshot differs")
    return dict(row)


async def run() -> int:
    args = arguments()
    plan_path = private_path(args.plan)
    admission_path = private_path(args.admission_result)
    output = private_path(args.output, output=True)
    plan, items_by_claim = load_sources(plan_path, admission_path)
    if os.environ.get("MEMORY_V1_PROJECTION_PAYLOAD_REPAIR") != "authorized":
        raise ProjectionPayloadRepairError("payload repair gate is closed")
    if args.vector_size != 3072:
        raise ProjectionPayloadRepairError("vector size differs")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise ProjectionPayloadRepairError("runtime configuration is missing")

    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    index = ClaimVectorIndex(
        qdrant,
        collection_name=args.collection,
        vector_size=args.vector_size,
    )
    points = qdrant.retrieve(
        collection_name=args.collection,
        ids=sorted(items_by_claim),
        with_payload=True,
        with_vectors=True,
    )
    point_by_id = {str(point.id): point for point in points}
    if point_by_id.keys() != items_by_claim.keys():
        raise ProjectionPayloadRepairError("repair target point set differs")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    repaired: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ProjectionPayloadRepairError("reader must use brains_app")
        async with conn.transaction(readonly=True, isolation="serializable"):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                OWNER,
            )
            for claim_id in sorted(items_by_claim):
                item = items_by_claim[claim_id]
                snapshot = await load_snapshot(conn, item)
                point = point_by_id[claim_id]
                vector = point.vector
                if (
                    not isinstance(vector, list)
                    or len(vector) != args.vector_size
                    or any(not math.isfinite(float(value)) for value in vector)
                    or (point.payload or {}).get("owner_user_id") != OWNER
                ):
                    raise ProjectionPayloadRepairError(
                        "repair target vector or owner differs"
                    )
                index.upsert_claim(OWNER, snapshot, vector)
                expected_payload = projection_payload(uuid.UUID(OWNER), snapshot)
                repaired.append(
                    {
                        "claim_id": claim_id,
                        "predicate": item["predicate"],
                        "surface": expected_payload["surface"],
                        "requires_explicit": expected_payload[
                            "requires_explicit"
                        ],
                    }
                )
    finally:
        await conn.close()

    verified = qdrant.retrieve(
        collection_name=args.collection,
        ids=sorted(items_by_claim),
        with_payload=True,
        with_vectors=True,
    )
    qdrant.close()
    verified_by_id = {str(point.id): point for point in verified}
    for item in repaired:
        point = verified_by_id.get(item["claim_id"])
        payload = point.payload if point else {}
        if (
            payload.get("surface") != item["surface"]
            or payload.get("requires_explicit") != item["requires_explicit"]
            or not isinstance(point.vector, list)
            or len(point.vector) != args.vector_size
        ):
            raise ProjectionPayloadRepairError("repaired payload verification failed")

    result = {
        "contract_version": CONTRACT,
        "owner_user_id": OWNER,
        "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "admission_result_file_sha256": hashlib.sha256(
            admission_path.read_bytes()
        ).hexdigest(),
        "admission_result_sha256": json.loads(admission_path.read_text())[
            "result_sha256"
        ],
        "collection": args.collection,
        "embedding_requests": 0,
        "automatic_http_retries": 0,
        "qdrant_writes": len(repaired),
        "repaired": repaired,
        "database_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"embedding_requests=0")
    print(f"qdrant_writes={len(repaired)}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
