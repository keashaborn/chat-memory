from __future__ import annotations

import asyncio
import inspect
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence

import asyncpg
from qdrant_client.http import models as qmodels

from .memory_v1_store import InvalidActor, actor_uuid


DEFAULT_COLLECTION = "memory_claim_v1"
DEFAULT_VECTOR_SIZE = 3072
RETRIEVABLE_STATUSES = {"supported", "uncertain", "disputed"}


class ProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionJob:
    outbox_id: uuid.UUID
    claim_id: uuid.UUID
    operation: str
    payload: Dict[str, Any]
    attempts: int


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ProjectionError(f"{field} must be a JSON object")


def _policy_values(policy: Mapping[str, Any], key: str) -> list[str]:
    value = policy.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ProjectionError(f"retrieval_policy.{key} must be a string or list")
    return sorted({str(item).strip().lower() for item in values if str(item).strip()})


def _vector(values: Sequence[float], expected_size: Optional[int] = None) -> list[float]:
    out = [float(value) for value in values]
    if not out:
        raise ProjectionError("embedding vector is empty")
    if expected_size is not None and len(out) != expected_size:
        raise ProjectionError(
            f"embedding dimension {len(out)} does not match collection dimension {expected_size}"
        )
    if any(not math.isfinite(value) for value in out):
        raise ProjectionError("embedding vector contains a non-finite value")
    return out


def render_claim_for_embedding(snapshot: Mapping[str, Any]) -> str:
    text = " ".join(str(snapshot.get("canonical_text") or "").split()).strip()
    predicate = str(snapshot.get("predicate") or "").strip()
    if not text or not predicate:
        raise ProjectionError("claim snapshot is missing canonical_text or predicate")
    qualifiers = _json_object(snapshot.get("qualifiers"), "qualifiers")
    parts = [f"claim: {text}", f"predicate: {predicate}"]
    if qualifiers:
        parts.append(
            "qualifiers: "
            + json.dumps(qualifiers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    return "\n".join(parts)


def projection_payload(
    actor: uuid.UUID, snapshot: Mapping[str, Any]
) -> Dict[str, Any]:
    policy = _json_object(snapshot.get("retrieval_policy"), "retrieval_policy")
    return {
        "schema_version": "memory_claim_projection_v1",
        "owner_user_id": str(actor),
        "claim_id": str(snapshot["claim_id"]),
        "revision_number": int(snapshot["revision_number"]),
        "status": str(snapshot["status"]),
        "predicate": str(snapshot["predicate"]),
        "sensitivity": str(snapshot["sensitivity"]),
        "domains": _policy_values(policy, "domains"),
        "intents": _policy_values(policy, "intents"),
        "surface": str(policy.get("surface") or "support").strip().lower(),
        "requires_explicit": bool(policy.get("requires_explicit")),
        "updated_at": snapshot["updated_at"].isoformat()
        if isinstance(snapshot.get("updated_at"), datetime)
        else str(snapshot.get("updated_at") or ""),
    }


class ClaimVectorIndex:
    def __init__(
        self,
        client: Any,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        vector_size: int = DEFAULT_VECTOR_SIZE,
    ) -> None:
        self.client = client
        self.collection_name = str(collection_name).strip()
        self.vector_size = int(vector_size)
        if not self.collection_name:
            raise ProjectionError("collection_name is required")
        if self.vector_size <= 0:
            raise ProjectionError("vector_size must be positive")

    def ensure_collection(self) -> bool:
        collections = self.client.get_collections().collections
        names = {item.name for item in collections}
        created = self.collection_name not in names
        if created:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(
                    size=self.vector_size,
                    distance=qmodels.Distance.COSINE,
                    on_disk=False,
                ),
                on_disk_payload=True,
            )
        info = self.client.get_collection(self.collection_name)
        params = info.config.params.vectors
        if getattr(params, "size", None) != self.vector_size:
            raise ProjectionError("existing collection has the wrong vector dimension")
        if getattr(params, "distance", None) != qmodels.Distance.COSINE:
            raise ProjectionError("existing collection does not use cosine distance")

        existing_payload = set((getattr(info, "payload_schema", None) or {}).keys())
        for field_name in ("owner_user_id", "status", "sensitivity", "domains", "intents"):
            if field_name not in existing_payload:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
        return created

    def upsert_claim(
        self,
        actor_user_id: str | uuid.UUID,
        snapshot: Mapping[str, Any],
        embedding: Sequence[float],
    ) -> None:
        actor = actor_uuid(actor_user_id)
        claim_id = uuid.UUID(str(snapshot["claim_id"]))
        self.client.upsert(
            collection_name=self.collection_name,
            wait=True,
            points=[
                qmodels.PointStruct(
                    id=str(claim_id),
                    vector=_vector(embedding, self.vector_size),
                    payload=projection_payload(actor, snapshot),
                )
            ],
        )

    def delete_claim(
        self, actor_user_id: str | uuid.UUID, claim_id: str | uuid.UUID
    ) -> None:
        actor = actor_uuid(actor_user_id)
        claim_uuid = uuid.UUID(str(claim_id))
        # The owner filter prevents an incorrect worker actor from deleting another
        # owner's point, even if a claim UUID is supplied incorrectly.
        self.client.delete(
            collection_name=self.collection_name,
            wait=True,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="owner_user_id", match=qmodels.MatchValue(value=str(actor))
                        ),
                        qmodels.HasIdCondition(has_id=[str(claim_uuid)]),
                    ]
                )
            ),
        )

    def search_claims(
        self,
        actor_user_id: str | uuid.UUID,
        query_vector: Sequence[float],
        *,
        limit: int = 24,
    ) -> list[Dict[str, Any]]:
        actor = actor_uuid(actor_user_id)
        if not 1 <= int(limit) <= 100:
            raise ProjectionError("limit must be between 1 and 100")
        hits = self.client.search(
            collection_name=self.collection_name,
            query_vector=_vector(query_vector, self.vector_size),
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="owner_user_id", match=qmodels.MatchValue(value=str(actor))
                    ),
                    qmodels.FieldCondition(
                        key="status",
                        match=qmodels.MatchAny(any=sorted(RETRIEVABLE_STATUSES)),
                    ),
                ]
            ),
            limit=int(limit),
            with_payload=False,
            with_vectors=False,
        )
        return [
            {
                "claim_id": str(uuid.UUID(str(hit.id))),
                # Cosine search is theoretically [-1, 1] and can slightly exceed
                # its bounds numerically. Retrieval scoring accepts normalized
                # relevance only, so clamp without changing rank order.
                "semantic_score": max(0.0, min(1.0, float(hit.score))),
            }
            for hit in hits
        ]


async def _set_actor(conn: asyncpg.Connection, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(actor))


async def claim_projection_jobs(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    limit: int = 25,
    max_attempts: int = 8,
) -> list[ProjectionJob]:
    try:
        actor = actor_uuid(actor_user_id)
    except InvalidActor as exc:
        raise ProjectionError(str(exc)) from exc
    if not 1 <= int(limit) <= 100:
        raise ProjectionError("limit must be between 1 and 100")
    if not 1 <= int(max_attempts) <= 50:
        raise ProjectionError("max_attempts must be between 1 and 50")
    async with conn.transaction():
        await _set_actor(conn, actor)
        rows = await conn.fetch(
            """
            WITH picked AS (
              SELECT outbox_id
              FROM memory.projection_outbox
              WHERE owner_user_id=$1
                AND aggregate_type='claim'
                AND status IN ('pending', 'error')
                AND attempts < $3
                AND available_at <= clock_timestamp()
              ORDER BY available_at, created_at, outbox_id
              LIMIT $2
              FOR UPDATE SKIP LOCKED
            )
            UPDATE memory.projection_outbox AS outbox
            SET status='processing'::memory.outbox_status,
                attempts=outbox.attempts + 1,
                last_error=NULL,
                updated_at=clock_timestamp()
            FROM picked
            WHERE outbox.owner_user_id=$1
              AND outbox.outbox_id=picked.outbox_id
            RETURNING outbox.outbox_id, outbox.aggregate_id,
                      outbox.operation, outbox.payload, outbox.attempts
            """,
            actor,
            int(limit),
            int(max_attempts),
        )
    return [
        ProjectionJob(
            outbox_id=uuid.UUID(str(row["outbox_id"])),
            claim_id=uuid.UUID(str(row["aggregate_id"])),
            operation=str(row["operation"]),
            payload=_json_object(row["payload"], "outbox payload"),
            attempts=int(row["attempts"]),
        )
        for row in rows
    ]


async def _claim_snapshot(
    conn: asyncpg.Connection, actor: uuid.UUID, claim_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    async with conn.transaction():
        await _set_actor(conn, actor)
        row = await conn.fetchrow(
            """
            SELECT claim.claim_id, claim.canonical_text, claim.predicate,
                   claim.qualifiers, claim.status::text, claim.sensitivity::text,
                   claim.retrieval_policy, claim.updated_at,
                   COALESCE(max(revision.revision_number), 0) AS revision_number
            FROM memory.claim AS claim
            LEFT JOIN memory.claim_revision AS revision
              ON revision.owner_user_id=claim.owner_user_id
             AND revision.claim_id=claim.claim_id
            WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
            GROUP BY claim.claim_id
            """,
            actor,
            claim_id,
        )
    return dict(row) if row else None


async def _finish_job(
    conn: asyncpg.Connection,
    actor: uuid.UUID,
    job: ProjectionJob,
    *,
    error: Optional[str] = None,
) -> bool:
    async with conn.transaction():
        await _set_actor(conn, actor)
        if error is None:
            result = await conn.execute(
                """
                UPDATE memory.projection_outbox
                SET status='done'::memory.outbox_status,
                    last_error=NULL,
                    updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND outbox_id=$2
                  AND status='processing'
                  AND payload=$3::jsonb
                """,
                actor,
                job.outbox_id,
                json.dumps(job.payload, sort_keys=True),
            )
        else:
            delay_seconds = min(300, 2 ** min(job.attempts, 8))
            result = await conn.execute(
                """
                UPDATE memory.projection_outbox
                SET status='error'::memory.outbox_status,
                    last_error=left($4, 2000),
                    available_at=clock_timestamp() + make_interval(secs => $5),
                    updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND outbox_id=$2
                  AND status='processing'
                  AND payload=$3::jsonb
                """,
                actor,
                job.outbox_id,
                json.dumps(job.payload, sort_keys=True),
                error,
                delay_seconds,
            )
    return result == "UPDATE 1"


async def _embedding(
    embedder: Callable[[str], Sequence[float] | Awaitable[Sequence[float]]], text: str
) -> Sequence[float]:
    if inspect.iscoroutinefunction(embedder):
        return await embedder(text)
    result = await asyncio.to_thread(embedder, text)
    if inspect.isawaitable(result):
        return await result
    return result


async def process_owner_projection_outbox(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    index: ClaimVectorIndex,
    embedder: Callable[[str], Sequence[float] | Awaitable[Sequence[float]]],
    limit: int = 25,
    max_attempts: int = 8,
) -> Dict[str, Any]:
    actor = actor_uuid(actor_user_id)
    jobs = await claim_projection_jobs(
        conn, actor, limit=limit, max_attempts=max_attempts
    )
    result = {"claimed": len(jobs), "upserted": 0, "deleted": 0, "errors": 0, "stale": 0}
    for job in jobs:
        try:
            snapshot = await _claim_snapshot(conn, actor, job.claim_id)
            should_delete = (
                job.operation == "delete"
                or snapshot is None
                or str(snapshot["status"]) not in RETRIEVABLE_STATUSES
            )
            if should_delete:
                await asyncio.to_thread(index.delete_claim, actor, job.claim_id)
                action = "deleted"
            else:
                text = render_claim_for_embedding(snapshot)
                vector = await _embedding(embedder, text)
                await asyncio.to_thread(index.upsert_claim, actor, snapshot, vector)
                action = "upserted"
            if await _finish_job(conn, actor, job):
                result[action] += 1
            else:
                result["stale"] += 1
        except Exception as exc:  # Worker records and retries individual failures.
            if await _finish_job(conn, actor, job, error=f"{type(exc).__name__}: {exc}"):
                result["errors"] += 1
            else:
                result["stale"] += 1
    return result
