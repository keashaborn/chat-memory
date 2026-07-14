from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

import asyncpg

from .memory_v1_intent import PROJECT_KEY, classify_memory_intent
from .memory_v1_preference_project_retrieval import (
    VERSION as SELECTOR_VERSION,
    evaluate_specialized_memory,
    load_specialized_snapshot,
)
from .memory_v1_store import actor_uuid


VERSION = "memory_v1_preference_project_runtime_shadow_v2"
MAX_PREFERENCES = 3
MAX_PROJECT_RECORDS = 3
MAX_TOKENS = 500
MAX_SENSITIVITY = "high"


def _allowlisted(actor: uuid.UUID) -> bool:
    raw = os.getenv("MEMORY_V1_SHADOW_USER_IDS", "")
    values: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            values.add(str(uuid.UUID(value)))
        except ValueError:
            continue
    return str(actor) in values


def _query_hash(actor: uuid.UUID, query: str) -> str:
    material = actor.bytes + b"\0" + query.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _optional_uuid(value: Optional[str]) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _trace_metadata(
    intent_plan: Dict[str, Any],
    result: Dict[str, Any],
    *,
    decision_status: str = "evaluated",
    evaluation_elapsed_ms: float = 0.0,
    skip_reason: Optional[str] = None,
) -> Dict[str, Any]:
    controls = [
        {
            "preference_id": item["preference_id"],
            "preference_key": item["preference_key"],
            "action": item["action"],
            "matched_entities": list(item.get("matched_entities") or []),
            "would_suppress": bool(item.get("would_suppress")),
        }
        for item in result.get("policy_controls") or []
    ]
    preferences = [
        {
            "preference_id": item["preference_id"],
            "revision_id": item["revision_id"],
            "preference_key": item["preference_key"],
        }
        for item in result.get("selected_preferences") or []
    ]
    projects = [
        {
            "project_id": item["project_id"],
            "knowledge_id": item["knowledge_id"],
            "revision_id": item["revision_id"],
            "knowledge_key": item["knowledge_key"],
            "lexical_score": int(item["lexical_score"]),
        }
        for item in result.get("selected_project_records") or []
    ]
    metadata = {
        "version": VERSION,
        "selector_version": result["version"],
        "intent_adapter_version": intent_plan["version"],
        "decision_status": str(decision_status),
        "evaluation_elapsed_ms": round(max(0.0, float(evaluation_elapsed_ms)), 3),
        "request_classification": intent_plan["request_classification"],
        "reason_codes": list(intent_plan.get("reason_codes") or []),
        "domains": list(result.get("domains") or []),
        "project_key": result.get("project_key"),
        "candidate_entities": list(intent_plan.get("candidate_entities") or []),
        "direct_relevance": bool(intent_plan.get("direct_relevance")),
        "policy_controls": controls,
        "selected_preferences": preferences,
        "selected_project_records": projects,
        "token_estimate": int(result.get("token_estimate") or 0),
        "rejected_counts": dict(result.get("rejected_counts") or {}),
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }
    if skip_reason:
        metadata["skip_reason"] = str(skip_reason)
    return metadata


def _empty_result(intent_plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": SELECTOR_VERSION,
        "memory_intent": str(intent_plan.get("memory_intent") or "none"),
        "domains": list(intent_plan.get("domains") or []),
        "project_key": intent_plan.get("project_key"),
        "policy_controls": [],
        "selected_preferences": [],
        "selected_project_records": [],
        "selected_content_count": 0,
        "token_estimate": 0,
        "rejected_counts": {"not_specialized": 1},
    }


async def _persist_trace(
    conn: asyncpg.Connection,
    actor: uuid.UUID,
    *,
    query: str,
    request_id: Optional[str],
    thread_id: Optional[str],
    intent_plan: Dict[str, Any],
    result: Dict[str, Any],
    decision_status: str,
    evaluation_elapsed_ms: float,
    skip_reason: Optional[str] = None,
) -> uuid.UUID:
    trace_id = uuid.uuid4()
    metadata = _trace_metadata(
        intent_plan,
        result,
        decision_status=decision_status,
        evaluation_elapsed_ms=evaluation_elapsed_ms,
        skip_reason=skip_reason,
    )
    domain = ",".join(result.get("domains") or []) or "none"
    token_budget = 0 if decision_status == "skipped" else MAX_TOKENS
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))
        role = str(await conn.fetchval("SELECT current_user"))
        if role != "brains_app":
            raise RuntimeError("effective role must be brains_app")
        rls = await conn.fetchrow(
            """
            SELECT c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory' AND c.relname='retrieval_trace'
            """
        )
        if not rls or not bool(rls["relrowsecurity"]) or not bool(rls["relforcerowsecurity"]):
            raise RuntimeError("retrieval_trace RLS controls changed")
        await conn.execute(
            """
            INSERT INTO memory.retrieval_trace(
              trace_id,owner_user_id,request_id,answer_id,thread_id,
              query_hash,query_preview,intent,domain,
              token_budget,selected_count,metadata
            ) VALUES($1,$2,$3,NULL,$4,$5,NULL,$6,$7,$8,$9,$10::jsonb)
            """,
            trace_id,
            actor,
            str(request_id or "")[:200] or None,
            _optional_uuid(thread_id),
            _query_hash(actor, query),
            str(result["memory_intent"]),
            domain,
            token_budget,
            int(result.get("selected_content_count") or 0),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    return trace_id


async def _evaluate_and_trace(
    dsn: str,
    actor: uuid.UUID,
    *,
    query: str,
    request_id: Optional[str],
    thread_id: Optional[str],
    intent_plan: Dict[str, Any],
    started_at: float,
) -> Dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        snapshot = await load_specialized_snapshot(conn, actor, project_key=PROJECT_KEY)
        result = evaluate_specialized_memory(
            preferences=snapshot["preferences"],
            project_records=(
                snapshot["project_records"] if intent_plan.get("project_key") else []
            ),
            query=query,
            memory_intent=str(intent_plan["memory_intent"]),
            domains=list(intent_plan["domains"]),
            project_key=intent_plan.get("project_key"),
            candidate_entities=list(intent_plan["candidate_entities"]),
            direct_relevance=bool(intent_plan["direct_relevance"]),
            max_sensitivity=MAX_SENSITIVITY,
            max_preferences=MAX_PREFERENCES,
            max_project_records=MAX_PROJECT_RECORDS,
            max_tokens=MAX_TOKENS,
        )
        evaluation_elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        trace_id = await _persist_trace(
            conn,
            actor,
            query=query,
            request_id=request_id,
            thread_id=thread_id,
            intent_plan=intent_plan,
            result=result,
            decision_status="evaluated",
            evaluation_elapsed_ms=evaluation_elapsed_ms,
        )
        return {
            "trace_id": str(trace_id),
            "result": result,
            "evaluation_elapsed_ms": round(evaluation_elapsed_ms, 3),
        }
    finally:
        await conn.close()


async def _trace_skipped(
    dsn: str,
    actor: uuid.UUID,
    *,
    query: str,
    request_id: Optional[str],
    thread_id: Optional[str],
    intent_plan: Dict[str, Any],
    started_at: float,
) -> Dict[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        evaluation_elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        trace_id = await _persist_trace(
            conn,
            actor,
            query=query,
            request_id=request_id,
            thread_id=thread_id,
            intent_plan=intent_plan,
            result=_empty_result(intent_plan),
            decision_status="skipped",
            evaluation_elapsed_ms=evaluation_elapsed_ms,
            skip_reason="no_specialized_memory_need",
        )
        return {
            "trace_id": str(trace_id),
            "evaluation_elapsed_ms": round(evaluation_elapsed_ms, 3),
        }
    finally:
        await conn.close()


def run_preference_project_shadow(
    actor_user_id: str,
    *,
    query: str,
    request_classification: str,
    request_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    if os.getenv("MEMORY_V1_SPECIALIZED_SHADOW", "0").strip() != "1":
        return {"version": VERSION, "status": "disabled"}

    try:
        actor = actor_uuid(actor_user_id)
    except Exception as exc:
        return {"version": VERSION, "status": "error", "error_type": type(exc).__name__}
    if not _allowlisted(actor):
        return {
            "version": VERSION,
            "status": "skipped",
            "reason": "actor_not_allowlisted",
        }

    started_at = time.perf_counter()
    intent_plan = classify_memory_intent(
        query,
        request_classification=request_classification,
    )
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": "missing_configuration",
        }

    if not intent_plan["routes"]["specialized"]:
        try:
            traced = asyncio.run(
                _trace_skipped(
                    dsn,
                    actor,
                    query=query,
                    request_id=request_id,
                    thread_id=thread_id,
                    intent_plan=intent_plan,
                    started_at=started_at,
                )
            )
            return {
                "version": VERSION,
                "status": "skipped",
                "reason": "no_specialized_memory_need",
                "memory_intent": intent_plan["memory_intent"],
                "trace_id": traced["trace_id"],
                "evaluation_elapsed_ms": traced["evaluation_elapsed_ms"],
                "prompt_injection": False,
                "answer_model_exposure": False,
                "retrieval_activation": False,
                "diagnostic_trace_writes": 1,
            }
        except Exception as exc:
            return {
                "version": VERSION,
                "status": "error",
                "error_type": type(exc).__name__,
            }

    try:
        evaluated = asyncio.run(
            _evaluate_and_trace(
                dsn,
                actor,
                query=query,
                request_id=request_id,
                thread_id=thread_id,
                intent_plan=intent_plan,
                started_at=started_at,
            )
        )
        result = evaluated["result"]
        return {
            "version": VERSION,
            "status": "ok",
            "trace_id": evaluated["trace_id"],
            "memory_intent": result["memory_intent"],
            "domains": result["domains"],
            "project_key": result["project_key"],
            "policy_control_count": len(result["policy_controls"]),
            "would_suppress_count": sum(
                1 for item in result["policy_controls"] if item["would_suppress"]
            ),
            "selected_preference_keys": [
                item["preference_key"] for item in result["selected_preferences"]
            ],
            "selected_project_keys": [
                item["knowledge_key"] for item in result["selected_project_records"]
            ],
            "selected_content_count": result["selected_content_count"],
            "token_estimate": result["token_estimate"],
            "evaluation_elapsed_ms": evaluated["evaluation_elapsed_ms"],
            "prompt_injection": False,
            "answer_model_exposure": False,
            "retrieval_activation": False,
            "diagnostic_trace_writes": 1,
        }
    except Exception as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
        }
