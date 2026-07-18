from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from typing import Any, Mapping, Optional

import asyncpg

from .memory_v1_intent import PROJECT_INTENTS, classify_memory_intent
from .memory_v1_v5_project_shadow_loader import (
    load_v5_shadow_project_knowledge,
)


VERSION = "memory_v1_v5_project_shadow_trace_v1"
EMPTY_SET_SHA256 = hashlib.sha256(b"[]").hexdigest()


class V5ProjectShadowTraceError(RuntimeError):
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


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _enabled(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _uuid_values(raw: Any) -> set[str]:
    values: set[str] = set()
    for item in str(raw or "").split(","):
        try:
            values.add(str(uuid.UUID(item.strip())))
        except (ValueError, AttributeError):
            continue
    return values


def _allowlisted(actor: uuid.UUID) -> bool:
    if _enabled(os.getenv("MEMORY_V1_V5_PROJECT_SHADOW_ALL_AUTHENTICATED", "0")):
        return True
    return str(actor) in _uuid_values(
        os.getenv("MEMORY_V1_V5_PROJECT_SHADOW_USER_IDS", "")
    )


def _budget(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError) as exc:
        raise V5ProjectShadowTraceError(f"{name.casefold()} is invalid") from exc
    if not minimum <= value <= maximum:
        raise V5ProjectShadowTraceError(f"{name.casefold()} is out of range")
    return value


def _project_context(message: str, request_classification: str) -> dict[str, Any]:
    plan = classify_memory_intent(
        message,
        request_classification=request_classification,
    )
    intent = str(plan.get("memory_intent") or "none")
    if intent not in PROJECT_INTENTS:
        return {"eligible": False, "reason": "not_project_intent"}
    domains = list(plan.get("domains") or [])
    return {
        "eligible": True,
        "intent": intent,
        "domain": str(domains[0] if domains else "project"),
        "project_key": str(plan.get("project_key") or ""),
    }


def _record_identity(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "project_id": str(record["project_id"]),
        "component_id": str(record["component_id"]),
        "knowledge_id": str(record["knowledge_id"]),
        "revision_id": str(record["revision_id"]),
        "content_sha256": str(record["content_sha256"]),
    }


def _token_estimate(record: Mapping[str, Any]) -> int:
    text = str(record.get("canonical_text") or "")
    return max(1, (len(text) + 3) // 4)


async def build_v5_project_shadow_trace(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    query: str,
    request_classification: str,
    request_id: Optional[str],
    thread_id: str | uuid.UUID,
    max_items: int = 4,
    max_tokens: int = 600,
) -> dict[str, Any]:
    try:
        actor = uuid.UUID(str(actor_user_id))
        thread = uuid.UUID(str(thread_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ProjectShadowTraceError("actor and thread must be UUIDs") from exc
    context = _project_context(query, request_classification)
    if not context["eligible"]:
        raise V5ProjectShadowTraceError("eligible project context is required")
    records = await load_v5_shadow_project_knowledge(
        conn,
        actor,
        thread,
        limit=max_items,
    )

    candidates = [_record_identity(record) for record in records]
    selected: list[dict[str, str]] = []
    token_estimate = 0
    rejected_for_budget = 0
    for record, identity in zip(records, candidates):
        item_tokens = _token_estimate(record)
        if token_estimate + item_tokens > max_tokens:
            rejected_for_budget += 1
            continue
        selected.append(identity)
        token_estimate += item_tokens

    candidate_set_sha256 = _sha256(candidates)
    selection_set_sha256 = _sha256(selected)
    scope_value = (
        {
            "owner_user_id": str(actor),
            "thread_id": str(thread),
            "project_id": str(records[0]["project_id"]),
            "project_key": str(records[0]["project_key"]),
            "component_id": str(records[0]["component_id"]),
            "component_key": str(records[0]["component_key"]),
        }
        if records
        else {
            "owner_user_id": str(actor),
            "thread_id": str(thread),
            "scope": "no_visible_project_knowledge",
        }
    )
    request_id_sha256 = _text_sha256(request_id)
    thread_id_sha256 = _text_sha256(thread)
    query_sha256 = _text_sha256(query)
    binding = {
        "version": VERSION,
        "memory_lane": "project_knowledge",
        "owner_user_id": str(actor),
        "request_id_sha256": request_id_sha256,
        "thread_id_sha256": thread_id_sha256,
        "query_sha256": query_sha256,
        "intent": context["intent"],
        "domain": context["domain"],
        "project_scope_sha256": _sha256(scope_value),
        "candidate_set_sha256": candidate_set_sha256,
        "selection_set_sha256": selection_set_sha256,
    }
    rejected_counts = (
        {"token_budget": rejected_for_budget} if rejected_for_budget else {}
    )
    return {
        "version": VERSION,
        "memory_lane": "project_knowledge",
        "status": "ok",
        "outcome_code": "evaluated" if records else "no_visible_project_knowledge",
        "owner_user_id_sha256": _text_sha256(actor),
        "request_id_sha256": request_id_sha256,
        "thread_id_sha256": thread_id_sha256,
        "request_binding_sha256": _sha256(binding),
        "query_sha256": query_sha256,
        "persistable": bool(str(request_id or "").strip()),
        "intent": context["intent"],
        "domain": context["domain"],
        "project_scope_sha256": binding["project_scope_sha256"],
        "candidate_set_sha256": candidate_set_sha256,
        "selection_set_sha256": selection_set_sha256,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "token_estimate": token_estimate,
        "rejected_counts": rejected_counts,
        "max_items": int(max_items),
        "max_tokens": int(max_tokens),
        "database_transaction": "read_only",
        "database_writes": 0,
        "qdrant_reads": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "trace_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


async def _connect_and_build(
    dsn: str,
    actor: uuid.UUID,
    **kwargs: Any,
) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        return await build_v5_project_shadow_trace(conn, actor, **kwargs)
    finally:
        await conn.close()


def run_memory_v1_v5_project_shadow_trace(
    actor_user_id: str,
    *,
    query: str,
    request_classification: str,
    request_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    if not _enabled(os.getenv("MEMORY_V1_V5_PROJECT_SHADOW", "0")):
        return {"version": VERSION, "status": "disabled", "persistable": False}
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
            "persistable": False,
        }
    if not _allowlisted(actor):
        return {
            "version": VERSION,
            "status": "skipped",
            "reason": "actor_not_allowlisted",
            "persistable": False,
        }
    context = _project_context(query, request_classification)
    if not context["eligible"]:
        return {
            "version": VERSION,
            "status": "skipped",
            "reason": context["reason"],
            "persistable": False,
        }
    try:
        thread = uuid.UUID(str(thread_id))
        max_items = _budget("MEMORY_V1_V5_PROJECT_SHADOW_MAX_ITEMS", 4, 1, 8)
        max_tokens = _budget("MEMORY_V1_V5_PROJECT_SHADOW_MAX_TOKENS", 600, 1, 4000)
        dsn = os.getenv("POSTGRES_DSN")
        if not dsn:
            raise V5ProjectShadowTraceError("POSTGRES_DSN is required")
        return asyncio.run(
            _connect_and_build(
                dsn,
                actor,
                query=query,
                request_classification=request_classification,
                request_id=request_id,
                thread_id=thread,
                max_items=max_items,
                max_tokens=max_tokens,
            )
        )
    except Exception as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
            "persistable": False,
        }
