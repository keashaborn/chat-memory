from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Callable, Dict, Optional, Sequence

import asyncpg

from .memory_v1_projection import ClaimVectorIndex
from .memory_v1_intent import classify_memory_intent
from .memory_v1_retrieval import build_memory_packet
from .memory_v1_store import actor_uuid
from .openai_client import embed_text
from .qdrant_compat import make_qdrant_client


def classify_shadow_context(message: str, turn_intent: str) -> Dict[str, Any]:
    plan = classify_memory_intent(
        message,
        request_classification=turn_intent,
    )
    return dict(plan["claim_context"])


def _allowlisted(actor: uuid.UUID) -> bool:
    raw = os.getenv("MEMORY_V1_SHADOW_USER_IDS", "")
    values = {item.strip() for item in raw.split(",") if item.strip()}
    if not values:
        return False
    canonical = set()
    for value in values:
        try:
            canonical.add(str(uuid.UUID(value)))
        except ValueError:
            continue
    return str(actor) in canonical


async def _packet(
    dsn: str,
    actor: uuid.UUID,
    *,
    query: str,
    context: Dict[str, Any],
    candidate_hits: list[Dict[str, Any]],
    request_id: Optional[str],
    thread_id: Optional[str],
) -> Dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        return await build_memory_packet(
            conn,
            actor,
            query=query,
            intent=context["intent"],
            domain=context["domain"],
            candidate_hits=candidate_hits,
            max_claims=int(os.getenv("MEMORY_V1_SHADOW_MAX_CLAIMS", "4") or 4),
            max_tokens=int(os.getenv("MEMORY_V1_SHADOW_MAX_TOKENS", "500") or 500),
            max_sensitivity=os.getenv("MEMORY_V1_SHADOW_MAX_SENSITIVITY", "medium"),
            explicit_recall=bool(context["explicit_recall"]),
            entity_hints=context["entity_hints"],
            request_id=request_id,
            thread_id=thread_id,
        )
    finally:
        await conn.close()


def run_memory_v1_shadow(
    actor_user_id: str,
    *,
    query: str,
    turn_intent: str,
    request_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    query_vector: Optional[Sequence[float]] = None,
    embedding_provider: Optional[Callable[[], Sequence[float]]] = None,
) -> Dict[str, Any]:
    if os.getenv("MEMORY_V1_SHADOW", "0").strip() != "1":
        return {"version": "memory_v1_route_shadow_v1", "status": "disabled"}

    actor = actor_uuid(actor_user_id)
    if not _allowlisted(actor):
        return {
            "version": "memory_v1_route_shadow_v1",
            "status": "skipped",
            "reason": "actor_not_allowlisted",
        }

    context = classify_shadow_context(query, turn_intent)
    if not context["eligible"]:
        return {
            "version": "memory_v1_route_shadow_v1",
            "status": "skipped",
            "reason": context["reason"],
        }

    dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL")
    if not dsn or not qdrant_url:
        return {
            "version": "memory_v1_route_shadow_v1",
            "status": "error",
            "error_type": "missing_configuration",
        }

    qdrant = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        if query_vector is not None and embedding_provider is not None:
            raise ValueError("provide query_vector or embedding_provider, not both")
        if query_vector is not None:
            vector = [float(value) for value in query_vector]
        elif embedding_provider is not None:
            vector = [float(value) for value in embedding_provider()]
        else:
            vector = embed_text(
                query, model=os.getenv("EMBED_MODEL", "text-embedding-3-large")
            )
        if not vector:
            raise ValueError("query vector must not be empty")
        index = ClaimVectorIndex(
            qdrant,
            collection_name=os.getenv("MEMORY_V1_COLLECTION", "memory_claim_v1"),
        )
        hits = index.search_claims(
            actor,
            vector,
            limit=int(os.getenv("MEMORY_V1_SHADOW_CANDIDATE_LIMIT", "24") or 24),
        )
        packet = asyncio.run(
            _packet(
                dsn,
                actor,
                query=query,
                context=context,
                candidate_hits=hits,
                request_id=request_id,
                thread_id=thread_id,
            )
        )
        return {
            "version": "memory_v1_route_shadow_v1",
            "status": "ok",
            "trace_id": packet["trace_id"],
            "domain": context["domain"],
            "intent": context["intent"],
            "entity_hints": context["entity_hints"],
            "candidate_count": packet["candidate_count"],
            "selected_count": packet["selected_count"],
            "token_estimate": packet["token_estimate"],
            "rejected_counts": packet["rejected_counts"],
        }
    except Exception as exc:
        return {
            "version": "memory_v1_route_shadow_v1",
            "status": "error",
            "error_type": type(exc).__name__,
        }
    finally:
        qdrant.close()
