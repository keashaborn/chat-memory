#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import asyncpg

from rag_engine.memory_v1_projection import (
    ClaimVectorIndex,
    process_owner_projection_outbox,
)
from rag_engine.memory_v1_store import (
    apply_candidate,
    propose_candidate,
    record_evidence,
)


ACTOR_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTOR_B = uuid.UUID("22222222-2222-4222-8222-222222222222")


class FakeQdrant:
    def __init__(self) -> None:
        self.collection = None
        self.vector_size = None
        self.distance = None
        self.payload_schema: dict[str, object] = {}
        self.points: dict[str, object] = {}

    def get_collections(self):
        names = [] if self.collection is None else [SimpleNamespace(name=self.collection)]
        return SimpleNamespace(collections=names)

    def create_collection(self, *, collection_name, vectors_config, on_disk_payload):
        if not on_disk_payload:
            raise AssertionError("projection payload must be on disk")
        self.collection = collection_name
        self.vector_size = vectors_config.size
        self.distance = vectors_config.distance

    def get_collection(self, collection_name):
        if collection_name != self.collection:
            raise AssertionError("unknown collection")
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=self.vector_size, distance=self.distance)
                )
            ),
            payload_schema=self.payload_schema,
        )

    def create_payload_index(self, *, collection_name, field_name, field_schema, wait):
        if collection_name != self.collection or not wait:
            raise AssertionError("invalid payload index request")
        self.payload_schema[field_name] = field_schema

    def upsert(self, *, collection_name, wait, points):
        if collection_name != self.collection or not wait:
            raise AssertionError("invalid upsert request")
        for point in points:
            self.points[str(point.id)] = point

    def delete(self, *, collection_name, wait, points_selector):
        if collection_name != self.collection or not wait:
            raise AssertionError("invalid delete request")
        owner = None
        ids = []
        for condition in points_selector.filter.must:
            if getattr(condition, "key", None) == "owner_user_id":
                owner = condition.match.value
            if hasattr(condition, "has_id"):
                ids = [str(value) for value in condition.has_id]
        for point_id in ids:
            point = self.points.get(point_id)
            if point and point.payload.get("owner_user_id") == owner:
                self.points.pop(point_id, None)

    def search(
        self,
        *,
        collection_name,
        query_vector,
        query_filter,
        limit,
        with_payload,
        with_vectors,
    ):
        if collection_name != self.collection or with_payload or with_vectors:
            raise AssertionError("invalid search request")
        owner = None
        statuses = set()
        for condition in query_filter.must:
            if condition.key == "owner_user_id":
                owner = condition.match.value
            if condition.key == "status":
                statuses = set(condition.match.any)
        hits = []
        for point in self.points.values():
            if point.payload["owner_user_id"] != owner:
                continue
            if point.payload["status"] not in statuses:
                continue
            score = sum(float(a) * float(b) for a, b in zip(query_vector, point.vector))
            hits.append(SimpleNamespace(id=point.id, score=score))
        return sorted(hits, key=lambda hit: -hit.score)[:limit]


def proposal(value: str) -> dict:
    return {
        "subject": {
            "entity_key": "self",
            "entity_type": "person",
            "canonical_name": "The user",
        },
        "predicate": "name.canonical",
        "object_literal": {"type": "str", "v": value},
        "canonical_text": f"The canonical pet name is {value}.",
        "qualifiers": {"entity_role": "pet"},
        "status": "supported",
        "confidence": 0.9,
        "importance": 0.7,
        "salience": 0.8,
        "sensitivity": "low",
        "retrieval_policy": {
            "domains": ["name_correction"],
            "intents": ["personal_recall"],
            "surface": "normalization",
        },
        "evidence_stance": "supports",
        "support_score": 0.9,
        "opposition_score": 0.0,
        "assessment_method": "projection_integration",
        "assessment_method_version": "v1",
    }


async def create_claim(conn, actor: uuid.UUID, value: str) -> str:
    evidence_id = await record_evidence(
        conn,
        actor,
        kind="user_statement",
        source_system="memory_v1_projection_integration",
        external_id=f"{actor}:{value}",
        content=f"The canonical pet name is {value}.",
        directness=1.0,
        sensitivity="low",
    )
    candidate = await propose_candidate(
        conn,
        actor,
        evidence_id=evidence_id,
        proposal=proposal(value),
        extractor="reviewed_seed_v1",
        extractor_version="v1",
        auto_approve=True,
    )
    result = await apply_candidate(
        conn,
        actor,
        candidate_id=candidate["candidate_id"],
        expected_proposal_hash=candidate["proposal_hash"],
        actor_type="admin",
        actor_ref="projection_integration",
    )
    return result["claim_id"]


def embed(text: str) -> list[float]:
    lowered = text.lower()
    return [
        1.0 if "neko" in lowered else 0.0,
        1.0 if "luna" in lowered else 0.0,
        0.5,
    ]


async def set_actor(conn, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(actor))


async def requeue(conn, actor: uuid.UUID, claim_id: str, revision: int) -> None:
    async with conn.transaction():
        await set_actor(conn, actor)
        await conn.execute(
            """
            UPDATE memory.projection_outbox
            SET payload=jsonb_build_object(
                  'claim_id', $3::text, 'revision_number', $4::integer
                ),
                status='pending'::memory.outbox_status,
                attempts=0,
                available_at=clock_timestamp(),
                last_error=NULL
            WHERE owner_user_id=$1 AND aggregate_id=$2
            """,
            actor,
            uuid.UUID(claim_id),
            claim_id,
            revision,
        )


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn)
    outer = conn.transaction()
    await outer.start()
    try:
        await conn.execute("SET LOCAL ROLE brains_app")
        claim_a = await create_claim(conn, ACTOR_A, "Neko")
        claim_b = await create_claim(conn, ACTOR_B, "Luna")

        fake = FakeQdrant()
        index = ClaimVectorIndex(fake, collection_name="memory_claim_v1_test", vector_size=3)
        if not index.ensure_collection():
            raise AssertionError("test collection was not created")
        if index.ensure_collection():
            raise AssertionError("collection creation was not idempotent")

        projected_a = await process_owner_projection_outbox(
            conn, ACTOR_A, index=index, embedder=embed
        )
        if projected_a != {
            "claimed": 1,
            "upserted": 1,
            "deleted": 0,
            "errors": 0,
            "stale": 0,
        }:
            raise AssertionError(f"unexpected actor A projection result: {projected_a}")
        if set(fake.points) != {claim_a}:
            raise AssertionError("processing actor A projected another owner's claim")

        projected_b = await process_owner_projection_outbox(
            conn, ACTOR_B, index=index, embedder=embed
        )
        if projected_b["upserted"] != 1 or set(fake.points) != {claim_a, claim_b}:
            raise AssertionError("actor B projection failed")

        for point in fake.points.values():
            if "canonical_text" in point.payload or "text" in point.payload:
                raise AssertionError("personal prose leaked into Qdrant payload")

        hits_a = index.search_claims(ACTOR_A, [1.0, 0.0, 0.5])
        hits_b = index.search_claims(ACTOR_B, [1.0, 0.0, 0.5])
        if [hit["claim_id"] for hit in hits_a] != [claim_a]:
            raise AssertionError("actor A search was not owner isolated")
        if [hit["claim_id"] for hit in hits_b] != [claim_b]:
            raise AssertionError("actor B search was not owner isolated")
        if not all(0.0 <= hit["semantic_score"] <= 1.0 for hit in hits_a + hits_b):
            raise AssertionError("semantic scores were not normalized")

        await requeue(conn, ACTOR_A, claim_a, 2)

        def fail_embed(_text: str):
            raise RuntimeError("deterministic embedding failure")

        failed = await process_owner_projection_outbox(
            conn, ACTOR_A, index=index, embedder=fail_embed
        )
        if failed["errors"] != 1:
            raise AssertionError("projection failure was not recorded")
        async with conn.transaction():
            await set_actor(conn, ACTOR_A)
            status = await conn.fetchrow(
                """
                SELECT status::text, last_error
                FROM memory.projection_outbox
                WHERE owner_user_id=$1 AND aggregate_id=$2
                """,
                ACTOR_A,
                uuid.UUID(claim_a),
            )
        if status["status"] != "error" or "deterministic embedding failure" not in status["last_error"]:
            raise AssertionError("projection error state is incomplete")

        await requeue(conn, ACTOR_A, claim_a, 3)

        async def stale_embed(text: str):
            await requeue(conn, ACTOR_A, claim_a, 4)
            return embed(text)

        stale = await process_owner_projection_outbox(
            conn, ACTOR_A, index=index, embedder=stale_embed
        )
        if stale["stale"] != 1:
            raise AssertionError("concurrent outbox refresh was overwritten")
        recovered = await process_owner_projection_outbox(
            conn, ACTOR_A, index=index, embedder=embed
        )
        if recovered["upserted"] != 1:
            raise AssertionError("refreshed outbox job was not projected")

        async with conn.transaction():
            await set_actor(conn, ACTOR_A)
            await conn.execute(
                """
                UPDATE memory.claim
                SET status='superseded'::memory.claim_status
                WHERE owner_user_id=$1 AND claim_id=$2
                """,
                ACTOR_A,
                uuid.UUID(claim_a),
            )
        await requeue(conn, ACTOR_A, claim_a, 5)
        deleted = await process_owner_projection_outbox(
            conn, ACTOR_A, index=index, embedder=embed
        )
        if deleted["deleted"] != 1 or claim_a in fake.points:
            raise AssertionError("non-retrievable claim was not deleted from Qdrant")
        if claim_b not in fake.points:
            raise AssertionError("actor A deletion removed actor B's point")

        print("memory_v1_projection_integration: PASS")
        return 0
    finally:
        await outer.rollback()
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
