from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_intent import PROJECT_INTENTS, classify_memory_intent
from .memory_v1_projection import ClaimVectorIndex
from .memory_v1_v5_shadow_candidate import discover_v5_shadow_candidates
from .memory_v1_v5_shadow_loader import load_v5_shadow_claims
from .memory_v1_v5_shadow_retrieval import evaluate_v5_shadow_claims
from .qdrant_compat import make_qdrant_client


VERSION = "memory_v1_v5_shadow_trace_v1"
SUPPRESSED_REQUEST_CLASSES = {"TECH", "MEMORY_ARCHITECTURE", "FM_CONCEPTUAL"}
SPECIALIZED_INTENTS = {"preference_recall", "recommendation", *PROJECT_INTENTS}
PROFILE_RECALL_CLASSES = {"SPECIFIC_RECALL", "PROFILE_SUMMARY"}
FIRST_PERSON_RE = re.compile(r"\b(?:i|me|my|mine)\b", re.IGNORECASE)
DOMAIN_PREDICATES = {
    "name_correction": ("identity.name", "identity.name_canonical"),
    "pet_loss": (
        "identity.name",
        "identity.name_canonical",
        "life_event.died",
        "relationship.has_pet",
    ),
    "family_death": (
        "identity.name",
        "identity.name_canonical",
        "life_event.died",
        "relationship.parent_of",
    ),
    "life_context": ("health.", "relationship.", "residence."),
    "health_behavior": ("health.",),
    "profile": (
        "age.reported",
        "credential.reported",
        "identity.name",
        "identity.name_canonical",
        "occupation.works_as",
        "residence.lives_at",
    ),
}


class V5ShadowTraceError(RuntimeError):
    pass


def _stable_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value)).hexdigest()


def _uuid_values(raw: Any) -> set[str]:
    values: set[str] = set()
    for item in str(raw or "").split(","):
        try:
            values.add(str(uuid.UUID(item.strip())))
        except (ValueError, AttributeError):
            continue
    return values


def _enabled(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _allowlisted(actor: uuid.UUID) -> bool:
    if _enabled(os.getenv("MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED", "0")):
        return True
    return str(actor) in _uuid_values(os.getenv("MEMORY_V1_V5_SHADOW_USER_IDS", ""))


def classify_v5_shadow_context(
    message: str,
    request_classification: str,
) -> dict[str, Any]:
    classification = str(request_classification or "GENERAL").strip().upper()
    if classification in SUPPRESSED_REQUEST_CLASSES:
        return {
            "eligible": False,
            "reason": f"turn_intent:{classification.casefold()}",
        }
    plan = classify_memory_intent(
        message,
        request_classification=classification,
    )
    memory_intent = str(plan.get("memory_intent") or "none")
    if memory_intent in SPECIALIZED_INTENTS:
        return {"eligible": False, "reason": "specialized_memory_route"}
    claim = dict(plan.get("claim_context") or {})
    if claim.get("eligible"):
        domain = str(claim.get("domain") or "")
        predicates = DOMAIN_PREDICATES.get(domain)
        if not predicates:
            return {"eligible": False, "reason": "unmapped_claim_domain"}
        return {
            **claim,
            "allowed_predicate_prefixes": list(predicates),
        }
    if (
        classification in PROFILE_RECALL_CLASSES
        and FIRST_PERSON_RE.search(str(message or ""))
    ):
        return {
            "eligible": True,
            "reason": "generic_profile_recall",
            "domain": "profile",
            "intent": "personal_recall",
            "explicit_recall": True,
            "entity_hints": [],
            "allowed_predicate_prefixes": list(DOMAIN_PREDICATES["profile"]),
        }
    return {
        "eligible": False,
        "reason": str(claim.get("reason") or "no_governed_claim_route"),
    }


def _maximum_sensitivity(actor: uuid.UUID, context: Mapping[str, Any]) -> str:
    default = os.getenv("MEMORY_V1_V5_SHADOW_MAX_SENSITIVITY", "medium")
    if not context.get("explicit_recall"):
        return default
    if str(actor) not in _uuid_values(
        os.getenv("MEMORY_V1_V5_SHADOW_EXPLICIT_HIGH_USER_IDS", "")
    ):
        return default
    return os.getenv("MEMORY_V1_V5_SHADOW_EXPLICIT_MAX_SENSITIVITY", "high")


async def build_v5_shadow_trace(
    conn: asyncpg.Connection,
    index: Any,
    actor_user_id: str | uuid.UUID,
    *,
    query: str,
    query_vector: Sequence[float],
    context: Mapping[str, Any],
    request_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    candidate_limit: int = 24,
    max_claims: int = 4,
    max_tokens: int = 500,
    max_sensitivity: str = "medium",
) -> dict[str, Any]:
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ShadowTraceError("actor_user_id must be a UUID") from exc
    if not context.get("eligible"):
        raise V5ShadowTraceError("eligible V5 shadow context is required")
    candidate = discover_v5_shadow_candidates(
        index,
        actor,
        query_vector,
        limit=candidate_limit,
    )
    candidate_hits = [
        {
            "claim_id": hit["claim_id"],
            "semantic_score": hit["semantic_score"],
        }
        for hit in candidate["candidate_hits"]
    ]
    records: list[dict[str, Any]] = []
    if candidate_hits:
        async with conn.transaction(readonly=True):
            records = await load_v5_shadow_claims(
                conn,
                actor,
                [hit["claim_id"] for hit in candidate_hits],
            )
    result = evaluate_v5_shadow_claims(
        actor,
        query=query,
        intent=str(context["intent"]),
        domain=str(context["domain"]),
        allowed_predicate_prefixes=context["allowed_predicate_prefixes"],
        candidate_hits=candidate_hits,
        records=records,
        max_claims=max_claims,
        max_tokens=max_tokens,
        max_sensitivity=max_sensitivity,
        explicit_recall=bool(context.get("explicit_recall")),
    )
    if any(
        result[key]
        for key in ("prompt_injection", "answer_model_exposure", "retrieval_activation")
    ):
        raise V5ShadowTraceError("V5 shadow selector unexpectedly activated retrieval")
    selection_sha256 = _sha256(
        sorted(str(claim["claim_id"]) for claim in result["claims"])
    )
    query_sha256 = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
    binding = {
        "version": VERSION,
        "owner_user_id": str(actor),
        "request_id_sha256": hashlib.sha256(
            str(request_id or "").encode("utf-8")
        ).hexdigest(),
        "thread_id_sha256": hashlib.sha256(
            str(thread_id or "").encode("utf-8")
        ).hexdigest(),
        "query_sha256": query_sha256,
        "intent": str(context["intent"]),
        "domain": str(context["domain"]),
        "candidate_set_sha256": candidate["candidate_set_sha256"],
        "selection_set_sha256": selection_sha256,
    }
    return {
        "version": VERSION,
        "status": "ok",
        "owner_user_id_sha256": hashlib.sha256(
            str(actor).encode("utf-8")
        ).hexdigest(),
        "request_binding_sha256": _sha256(binding),
        "query_sha256": query_sha256,
        "intent": binding["intent"],
        "domain": binding["domain"],
        "candidate_set_sha256": candidate["candidate_set_sha256"],
        "selection_set_sha256": selection_sha256,
        "candidate_count": result["candidate_count"],
        "visible_candidate_count": result["visible_candidate_count"],
        "selected_count": result["selected_count"],
        "token_estimate": result["token_estimate"],
        "rejected_counts": result["rejected_counts"],
        "database_transaction": "read_only",
        "database_writes": 0,
        "qdrant_writes": 0,
        "trace_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


async def _connect_and_build(
    dsn: str,
    index: Any,
    actor: uuid.UUID,
    **kwargs: Any,
) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        return await build_v5_shadow_trace(conn, index, actor, **kwargs)
    finally:
        await conn.close()


def run_memory_v1_v5_shadow_trace(
    actor_user_id: str,
    *,
    query: str,
    request_classification: str,
    request_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    query_vector: Optional[Sequence[float]] = None,
    embedding_provider: Optional[Callable[[], Sequence[float]]] = None,
) -> dict[str, Any]:
    if not _enabled(os.getenv("MEMORY_V1_V5_SHADOW", "0")):
        return {"version": VERSION, "status": "disabled"}
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
        }
    if not _allowlisted(actor):
        return {
            "version": VERSION,
            "status": "skipped",
            "reason": "actor_not_allowlisted",
        }
    context = classify_v5_shadow_context(query, request_classification)
    if not context["eligible"]:
        return {
            "version": VERSION,
            "status": "skipped",
            "reason": context["reason"],
        }
    dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL")
    if not dsn or not qdrant_url:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": "missing_configuration",
        }
    client = None
    try:
        if query_vector is not None and embedding_provider is not None:
            raise V5ShadowTraceError(
                "provide query_vector or embedding_provider, not both"
            )
        if query_vector is not None:
            vector = [float(value) for value in query_vector]
        elif embedding_provider is not None:
            vector = [float(value) for value in embedding_provider()]
        else:
            raise V5ShadowTraceError("query vector provider is required")
        client = make_qdrant_client(url=qdrant_url, timeout=15.0)
        index = ClaimVectorIndex(
            client,
            collection_name=os.getenv("MEMORY_V1_COLLECTION", "memory_claim_v1"),
        )
        return asyncio.run(
            _connect_and_build(
                dsn,
                index,
                actor,
                query=query,
                query_vector=vector,
                context=context,
                request_id=request_id,
                thread_id=thread_id,
                candidate_limit=int(
                    os.getenv("MEMORY_V1_V5_SHADOW_CANDIDATE_LIMIT", "24") or 24
                ),
                max_claims=int(
                    os.getenv("MEMORY_V1_V5_SHADOW_MAX_CLAIMS", "4") or 4
                ),
                max_tokens=int(
                    os.getenv("MEMORY_V1_V5_SHADOW_MAX_TOKENS", "500") or 500
                ),
                max_sensitivity=_maximum_sensitivity(actor, context),
            )
        )
    except Exception as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
        }
    finally:
        if client is not None:
            client.close()
