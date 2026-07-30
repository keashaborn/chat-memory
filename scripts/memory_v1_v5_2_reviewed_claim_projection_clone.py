#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from qdrant_client.http import models as qmodels

from rag_engine.memory_v1_projection import (
    ClaimVectorIndex,
    process_owner_projection_outbox,
)
from rag_engine.qdrant_compat import make_qdrant_client


PLAN_CONTRACT = "memory_v1_v5_2_reviewed_claim_projection_plan_v1"
REPORT_CONTRACT = "memory_v1_v5_2_reviewed_claim_projection_clone_result_v1"
EXPECTED_CLAIMS = 19
ALLOWED_PREDICATES = {
    "age.reported",
    "health.user_reported_observation",
    "identity.name",
    "pet.breed",
    "pet.coat_color",
    "pet.eye_color",
    "pet.hearing_status",
    "pet.sex",
    "pet.weight_reported",
    "relationship.sibling_of",
}
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"


class CloneProjectionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection", required=True)
    return parser.parse_args()


def load_plan(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    items = value.get("items")
    projection = value.get("projection")
    plan_hash = value.get("plan_sha256")
    calculated_hash = hashlib.sha256(
        json.dumps(
            {key: item for key, item in value.items() if key != "plan_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if (
        value.get("contract_version") != PLAN_CONTRACT
        or plan_hash != calculated_hash
        or value.get("owner_user_id") != OWNER
        or value.get("other_owner_user_id") != OTHER_OWNER
        or value.get("required_ancestor_commit")
        != os.environ.get("MEMORY_V1_REQUIRED_ANCESTOR", "").strip()
        or not isinstance(items, list)
        or len(items) != EXPECTED_CLAIMS
        or len({item.get("claim_id") for item in items}) != EXPECTED_CLAIMS
        or not isinstance(projection, dict)
        or projection.get("maximum_embedding_requests") != EXPECTED_CLAIMS
        or projection.get("automatic_http_retries") != 0
        or projection.get("vector_size") != 3072
        or projection.get("embedding_model") != "text-embedding-3-large"
        or projection.get("answer_generation") is not False
        or projection.get("prompt_influence") is not False
        or projection.get("collection") != "memory_claim_v1"
    ):
        raise CloneProjectionError("projection plan is outside the exact boundary")
    normalized: list[dict[str, Any]] = []
    for item in items:
        try:
            claim_id = str(uuid.UUID(str(item["claim_id"])))
            revision = int(item["revision_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CloneProjectionError("plan contains an invalid claim identity") from exc
        if (
            revision != 2
            or item.get("predicate") not in ALLOWED_PREDICATES
            or item.get("prior_outbox") != "absent"
            or item.get("prior_qdrant") != "absent"
            or not isinstance(item.get("canonical_text_sha256"), str)
            or len(item["canonical_text_sha256"]) != 64
        ):
            raise CloneProjectionError("plan item is outside the exact claim boundary")
        normalized.append({**item, "claim_id": claim_id, "revision_number": revision})
    value["items"] = sorted(normalized, key=lambda item: item["claim_id"])
    return value


def stable_vector(text: str, size: int = 3072) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    vector = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(size)]
    if not any(value != 0.0 for value in vector):
        raise CloneProjectionError("deterministic vector unexpectedly collapsed")
    return vector


async def set_actor(conn: asyncpg.Connection, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))


async def claim_snapshot(
    conn: asyncpg.Connection, owner: uuid.UUID, item: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT claim.claim_id,claim.canonical_text,claim.predicate,
               claim.qualifiers,claim.status::text,claim.sensitivity::text,
               claim.retrieval_policy,claim.updated_at,
               COALESCE(max(revision.revision_number),0) AS revision_number
        FROM memory.claim AS claim
        LEFT JOIN memory.claim_revision AS revision
          ON revision.owner_user_id=claim.owner_user_id
         AND revision.claim_id=claim.claim_id
        WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
        GROUP BY claim.claim_id
        """,
        owner,
        uuid.UUID(item["claim_id"]),
    )
    if (
        row is None
        or row["status"] != "supported"
        or row["predicate"] != item["predicate"]
        or row["revision_number"] != item["revision_number"]
        or hashlib.sha256(row["canonical_text"].encode()).hexdigest()
        != item["canonical_text_sha256"]
    ):
        raise CloneProjectionError("supported claim snapshot differs from plan")
    return dict(row)


async def admit_exact_jobs(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> dict[str, str]:
    outbox_ids: dict[str, str] = {}
    async with conn.transaction(isolation="serializable"):
        await set_actor(conn, owner)
        for item in items:
            await claim_snapshot(conn, owner, item)
            claim_id = uuid.UUID(item["claim_id"])
            existing = await conn.fetchrow(
                """
                SELECT outbox_id,status::text,attempts,payload
                FROM memory.projection_outbox
                WHERE owner_user_id=$1 AND aggregate_type='claim'
                  AND aggregate_id=$2 AND operation='upsert'
                """,
                owner,
                claim_id,
            )
            payload = {
                "claim_id": item["claim_id"],
                "revision_number": item["revision_number"],
            }
            if item["prior_outbox"] == "absent":
                if existing is not None:
                    raise CloneProjectionError("new claim already has an outbox row")
                row = await conn.fetchrow(
                    """
                    INSERT INTO memory.projection_outbox(
                      owner_user_id,aggregate_type,aggregate_id,operation,payload
                    ) VALUES($1,'claim',$2,'upsert',$3::jsonb)
                    RETURNING outbox_id,status::text,attempts
                    """,
                    owner,
                    claim_id,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                )
            else:
                current_payload = existing["payload"] if existing else None
                if isinstance(current_payload, str):
                    current_payload = json.loads(current_payload)
                if (
                    existing is None
                    or existing["status"] != "done"
                    or existing["attempts"] != 1
                    or current_payload
                    != {"claim_id": item["claim_id"], "revision_number": 2}
                    or item["revision_number"] != 3
                ):
                    raise CloneProjectionError("replacement claim prior outbox state differs")
                row = await conn.fetchrow(
                    """
                    UPDATE memory.projection_outbox
                    SET payload=$3::jsonb,status='pending'::memory.outbox_status,
                        attempts=0,available_at=clock_timestamp(),last_error=NULL,
                        lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,
                        updated_at=clock_timestamp()
                    WHERE owner_user_id=$1 AND outbox_id=$2
                    RETURNING outbox_id,status::text,attempts
                    """,
                    owner,
                    existing["outbox_id"],
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                )
            if row is None or row["status"] != "pending" or row["attempts"] != 0:
                raise CloneProjectionError("exact outbox admission failed")
            outbox_ids[item["claim_id"]] = str(row["outbox_id"])
        eligible = await conn.fetchval(
            """
            SELECT count(*) FROM memory.projection_outbox
            WHERE owner_user_id=$1 AND aggregate_type='claim' AND attempts < 8
              AND (
                (status IN ('pending','error') AND available_at<=clock_timestamp())
                OR (status='processing' AND lease_expires_at<=clock_timestamp())
              )
            """,
            owner,
        )
        if eligible != len(items):
            raise CloneProjectionError("owner has projection work outside the exact plan")
    return outbox_ids


async def verify_cross_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    other_owner: uuid.UUID,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    claim_ids = [uuid.UUID(item["claim_id"]) for item in items]
    tx = conn.transaction(readonly=True, isolation="serializable")
    await tx.start()
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
    await tx.rollback()

    insert_rejected = False
    tx = conn.transaction(isolation="serializable")
    await tx.start()
    await set_actor(conn, other_owner)
    try:
        await conn.execute(
            """
            INSERT INTO memory.projection_outbox(
              owner_user_id,aggregate_type,aggregate_id,operation,payload
            ) VALUES($1,'claim',$2,'upsert',$3::jsonb)
            """,
            owner,
            uuid.uuid4(),
            json.dumps({"claim_id": str(uuid.uuid4()), "revision_number": 1}),
        )
    except (
        asyncpg.InsufficientPrivilegeError,
        asyncpg.CheckViolationError,
    ):
        insert_rejected = True
    finally:
        await tx.rollback()
    result = {
        "cross_owner_claim_count": visible_claims,
        "cross_owner_outbox_count": visible_outbox,
        "cross_owner_insert_rejected": insert_rejected,
    }
    if result != {
        "cross_owner_claim_count": 0,
        "cross_owner_outbox_count": 0,
        "cross_owner_insert_rejected": True,
    }:
        raise CloneProjectionError("cross-owner projection isolation failed")
    return result


async def run() -> int:
    args = arguments()
    plan_path = Path(args.plan).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise CloneProjectionError("output already exists")
    plan = load_plan(plan_path)
    owner = uuid.UUID(plan["owner_user_id"])
    other_owner = uuid.UUID(plan["other_owner_user_id"])
    items = plan["items"]
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise CloneProjectionError("clone DSN and Qdrant URL are required")

    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    index = ClaimVectorIndex(
        qdrant, collection_name=args.collection, vector_size=3072
    )
    index.ensure_collection()
    production_points = qdrant.retrieve(
        collection_name=plan["projection"]["collection"],
        ids=[item["claim_id"] for item in items],
        with_payload=True,
        with_vectors=True,
    )
    prior_by_id = {str(point.id): point for point in production_points}
    expected_existing = {
        item["claim_id"] for item in items if item["prior_qdrant"] == "revision_2"
    }
    if set(prior_by_id) != expected_existing:
        raise CloneProjectionError("production Qdrant target baseline differs from plan")
    for claim_id, point in prior_by_id.items():
        payload = point.payload or {}
        if payload.get("revision_number") != 2 or not isinstance(point.vector, list):
            raise CloneProjectionError("replacement Qdrant baseline is invalid")
        qdrant.upsert(
            collection_name=args.collection,
            wait=True,
            points=[
                qmodels.PointStruct(
                    id=claim_id,
                    vector=[float(value) for value in point.vector],
                    payload=payload,
                )
            ],
        )

    conn = await asyncpg.connect(dsn, command_timeout=60)
    calls = 0

    def embed(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        if calls > len(items):
            raise CloneProjectionError("deterministic embedding budget exceeded")
        return stable_vector(text)

    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise CloneProjectionError("clone DSN must authenticate as brains_app")
        outbox_ids = await admit_exact_jobs(conn, owner, items)
        result = await process_owner_projection_outbox(
            conn,
            owner,
            index=index,
            embedder=embed,
            worker_id="reviewed-claim-projection-clone",
            lease_seconds=600,
            limit=EXPECTED_CLAIMS,
            max_attempts=8,
        )
        expected_result = {
            "claimed": EXPECTED_CLAIMS,
            "upserted": EXPECTED_CLAIMS,
            "deleted": 0,
            "errors": 0,
            "stale": 0,
        }
        if result != expected_result or calls != EXPECTED_CLAIMS:
            raise CloneProjectionError("exact projection result differs")

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
            raise CloneProjectionError("projected outbox target set differs")
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
                or outbox_ids[item["claim_id"]] is None
            ):
                raise CloneProjectionError("projected outbox state differs")

        points = qdrant.retrieve(
            collection_name=args.collection,
            ids=[item["claim_id"] for item in items],
            with_payload=True,
            with_vectors=True,
        )
        point_by_id = {str(point.id): point for point in points}
        if set(point_by_id) != {item["claim_id"] for item in items}:
            raise CloneProjectionError("temporary projection point set differs")
        for item in items:
            point = point_by_id[item["claim_id"]]
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
                raise CloneProjectionError("temporary projection payload differs")
            owner_hits = index.search_claims(owner, vector, limit=24)
            if item["claim_id"] not in {hit["claim_id"] for hit in owner_hits}:
                raise CloneProjectionError("owner-only vector search missed target")
            other_hits = index.search_claims(other_owner, vector, limit=24)
            if item["claim_id"] in {hit["claim_id"] for hit in other_hits}:
                raise CloneProjectionError("cross-owner vector search exposed target")

        isolation = await verify_cross_owner(conn, owner, other_owner, items)
        before_replay = json.dumps(
            sorted(
                [
                    {
                        "id": str(point.id),
                        "payload": point.payload,
                        "vector_sha256": hashlib.sha256(
                            json.dumps(point.vector, separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
                    for point in points
                ],
                key=lambda value: value["id"],
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        replay = await process_owner_projection_outbox(
            conn,
            owner,
            index=index,
            embedder=lambda _text: (_ for _ in ()).throw(
                CloneProjectionError("replay attempted an embedding call")
            ),
            worker_id="reviewed-claim-projection-clone-replay",
            lease_seconds=600,
            limit=EXPECTED_CLAIMS,
            max_attempts=8,
        )
        replay_points = qdrant.retrieve(
            collection_name=args.collection,
            ids=[item["claim_id"] for item in items],
            with_payload=True,
            with_vectors=True,
        )
        after_replay = json.dumps(
            sorted(
                [
                    {
                        "id": str(point.id),
                        "payload": point.payload,
                        "vector_sha256": hashlib.sha256(
                            json.dumps(point.vector, separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
                    for point in replay_points
                ],
                key=lambda value: value["id"],
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        if replay != {
            "claimed": 0,
            "upserted": 0,
            "deleted": 0,
            "errors": 0,
            "stale": 0,
        } or before_replay != after_replay:
            raise CloneProjectionError("projection replay was not zero-write")

        report = {
            "contract_version": REPORT_CONTRACT,
            "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "owner_user_id": str(owner),
            "claim_count": len(items),
            "new_projection_count": EXPECTED_CLAIMS,
            "replacement_projection_count": 0,
            "embedding_requests": calls,
            "external_model_calls": 0,
            "temporary_qdrant_point_writes": EXPECTED_CLAIMS,
            "production_qdrant_writes": 0,
            "projection_result": result,
            "replay_result": replay,
            "cross_owner": isolation,
            "prompt_influence": False,
            "answer_generation": False,
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
