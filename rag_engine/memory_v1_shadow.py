from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, Optional

import asyncpg

from .memory_v1_projection import ClaimVectorIndex
from .memory_v1_retrieval import build_memory_packet
from .memory_v1_store import actor_uuid
from .openai_client import embed_text
from .qdrant_compat import make_qdrant_client


SKIPPED_TURN_INTENTS = {"TECH", "MEMORY_ARCHITECTURE", "FM_CONCEPTUAL"}
LOSS_TERMS = ("died", "dying", "death", "dead", "passed away", "loss", "lost")


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_shadow_context(message: str, turn_intent: str) -> Dict[str, Any]:
    text = " ".join(str(message or "").casefold().split())
    intent_key = str(turn_intent or "GENERAL").strip().upper() or "GENERAL"
    if not text:
        return {"eligible": False, "reason": "empty_query"}
    if intent_key in SKIPPED_TURN_INTENTS:
        return {
            "eligible": False,
            "reason": f"turn_intent:{intent_key.lower()}",
        }

    name_terms = (
        "neko",
        "nemo",
        "correct name",
        "correct spelling",
        "spell the name",
        "name correction",
        "name should be",
    )
    family_terms = (
        "mother",
        "mom",
        "mum",
        "father",
        "dad",
        "parent",
        "deedee",
    )
    pet_terms = ("pet", "dog", "cat", "helsing")
    caregiving_terms = (
        "caregiving",
        "caregiver",
        "caretaking",
        "caretaker",
        "psychotic break",
        "care burden",
        "caring for my wife",
        "caring for my spouse",
        "monika",
    )

    domain: Optional[str] = None
    if _contains(text, name_terms):
        domain = "name_correction"
    elif _contains(text, family_terms) and _contains(text, LOSS_TERMS):
        domain = "family_death"
    elif _contains(text, pet_terms) and _contains(text, LOSS_TERMS):
        domain = "pet_loss"
    elif _contains(text, caregiving_terms):
        domain = "life_context"

    if domain is None:
        return {"eligible": False, "reason": "unclassified_domain"}

    if intent_key in {"SPECIFIC_RECALL", "PROFILE_SUMMARY"}:
        retrieval_intent = "personal_recall"
    elif domain == "life_context":
        retrieval_intent = "relevant_support"
    else:
        retrieval_intent = "personal_recall"

    return {
        "eligible": True,
        "reason": "classified",
        "domain": domain,
        "intent": retrieval_intent,
        "explicit_recall": intent_key == "SPECIFIC_RECALL",
    }


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
        vector = embed_text(
            query, model=os.getenv("EMBED_MODEL", "text-embedding-3-large")
        )
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
