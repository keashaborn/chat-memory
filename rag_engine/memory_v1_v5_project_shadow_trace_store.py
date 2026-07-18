from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from typing import Any, Mapping, Optional

import asyncpg


VERSION = "memory_v1_v5_project_shadow_trace_store_v1"
TRACE_VERSION = "memory_v1_v5_project_shadow_trace_v1"
EMPTY_TEXT_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z0-9_:-]{1,80}$")
SHORT_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
FORBIDDEN_KEYS = {
    "answer",
    "answer_text",
    "canonical_text",
    "claim",
    "claims",
    "evidence",
    "message",
    "prompt",
    "query",
    "system_prompt",
    "text",
}


class V5ProjectShadowTraceStoreError(RuntimeError):
    pass


def _enabled(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _forbidden_key(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_KEYS:
                return normalized
            found = _forbidden_key(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _forbidden_key(child)
            if found:
                return found
    return None


def _hash(value: Any, field: str) -> str:
    parsed = str(value or "").strip().casefold()
    if not SHA256_RE.fullmatch(parsed):
        raise V5ProjectShadowTraceStoreError(f"{field} is not a SHA-256")
    return parsed


def _code(value: Any, field: str) -> str:
    parsed = str(value or "").strip().casefold()
    pattern = SHORT_CODE_RE if field in {"intent", "domain"} else CODE_RE
    if not pattern.fullmatch(parsed):
        raise V5ProjectShadowTraceStoreError(f"{field} is invalid")
    return parsed


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise V5ProjectShadowTraceStoreError(f"{field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise V5ProjectShadowTraceStoreError(f"{field} is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise V5ProjectShadowTraceStoreError(f"{field} is out of range")
    return parsed


def _payload(actor_user_id: str, trace: Mapping[str, Any]) -> dict[str, Any]:
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ProjectShadowTraceStoreError("actor_user_id must be a UUID") from exc
    if not isinstance(trace, Mapping) or trace.get("version") != TRACE_VERSION:
        raise V5ProjectShadowTraceStoreError("trace version is invalid")
    forbidden = _forbidden_key(trace)
    if forbidden:
        raise V5ProjectShadowTraceStoreError(f"trace contains forbidden field {forbidden}")
    if trace.get("memory_lane") != "project_knowledge":
        raise V5ProjectShadowTraceStoreError("memory_lane is invalid")
    if trace.get("persistable") is not True:
        raise V5ProjectShadowTraceStoreError("trace is not persistable")
    request_hash = _hash(trace.get("request_id_sha256"), "request_id_sha256")
    if request_hash == EMPTY_TEXT_SHA256:
        raise V5ProjectShadowTraceStoreError("request_id_sha256 is empty")
    candidate_count = _integer(trace.get("candidate_count"), "candidate_count", 0, 8)
    selected_count = _integer(trace.get("selected_count"), "selected_count", 0, 8)
    max_items = _integer(trace.get("max_items"), "max_items", 1, 8)
    max_tokens = _integer(trace.get("max_tokens"), "max_tokens", 1, 4000)
    token_estimate = _integer(trace.get("token_estimate"), "token_estimate", 0, 4000)
    if not selected_count <= candidate_count <= max_items or token_estimate > max_tokens:
        raise V5ProjectShadowTraceStoreError("trace counts exceed budgets")
    rejected_raw = trace.get("rejected_counts")
    if not isinstance(rejected_raw, Mapping) or len(rejected_raw) > 8:
        raise V5ProjectShadowTraceStoreError("rejected_counts is invalid")
    rejected_counts = {
        _code(key, "rejection_code"): _integer(count, "rejection_count", 0, 8)
        for key, count in rejected_raw.items()
    }
    if sum(rejected_counts.values()) != candidate_count - selected_count:
        raise V5ProjectShadowTraceStoreError("rejected counts do not reconcile")
    for field in (
        "database_writes",
        "qdrant_reads",
        "qdrant_writes",
        "external_model_calls",
        "trace_writes",
    ):
        if trace.get(field) != 0:
            raise V5ProjectShadowTraceStoreError(f"{field} must be zero")
    for field in ("prompt_injection", "answer_model_exposure", "retrieval_activation"):
        if trace.get(field) is not False:
            raise V5ProjectShadowTraceStoreError(f"{field} must be false")
    return {
        "actor": actor,
        "trace_version": TRACE_VERSION,
        "request_id_sha256": request_hash,
        "thread_id_sha256": _hash(trace.get("thread_id_sha256"), "thread_id_sha256"),
        "request_binding_sha256": _hash(
            trace.get("request_binding_sha256"), "request_binding_sha256"
        ),
        "query_sha256": _hash(trace.get("query_sha256"), "query_sha256"),
        "status": _code(trace.get("status"), "status"),
        "outcome_code": _code(trace.get("outcome_code"), "outcome_code"),
        "intent": _code(trace.get("intent"), "intent"),
        "domain": _code(trace.get("domain"), "domain"),
        "project_scope_sha256": _hash(
            trace.get("project_scope_sha256"), "project_scope_sha256"
        ),
        "candidate_set_sha256": _hash(
            trace.get("candidate_set_sha256"), "candidate_set_sha256"
        ),
        "selection_set_sha256": _hash(
            trace.get("selection_set_sha256"), "selection_set_sha256"
        ),
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "token_estimate": token_estimate,
        "rejected_counts": dict(sorted(rejected_counts.items())),
        "max_items": max_items,
        "max_tokens": max_tokens,
    }


async def _record(dsn: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(payload["actor"]),
            )
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.record_v5_project_shadow_trace_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                  $13,$14,$15,$16::jsonb,$17,$18
                )
                """,
                payload["trace_version"],
                payload["request_id_sha256"],
                payload["thread_id_sha256"],
                payload["request_binding_sha256"],
                payload["query_sha256"],
                payload["status"],
                payload["outcome_code"],
                payload["intent"],
                payload["domain"],
                payload["project_scope_sha256"],
                payload["candidate_set_sha256"],
                payload["selection_set_sha256"],
                payload["candidate_count"],
                payload["selected_count"],
                payload["token_estimate"],
                json.dumps(payload["rejected_counts"], separators=(",", ":"), sort_keys=True),
                payload["max_items"],
                payload["max_tokens"],
            )
            if row is None:
                raise V5ProjectShadowTraceStoreError("trace writer returned no row")
            return dict(row)
    finally:
        await conn.close()


def persist_memory_v1_v5_project_shadow_trace(
    actor_user_id: str,
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    if not _enabled(
        os.getenv("MEMORY_V1_V5_PROJECT_SHADOW_TRACE_PERSISTENCE", "0")
    ):
        return {"version": VERSION, "status": "disabled", "rows_written": 0}
    try:
        payload = _payload(actor_user_id, trace)
        dsn = os.getenv("POSTGRES_DSN")
        if not dsn:
            raise V5ProjectShadowTraceStoreError("POSTGRES_DSN is required")
        row = asyncio.run(_record(dsn, payload))
        outcome = str(row.get("outcome") or "").strip().casefold()
        if outcome not in {"applied", "replayed"}:
            raise V5ProjectShadowTraceStoreError("trace writer outcome is invalid")
        rows_written = _integer(row.get("rows_written"), "rows_written", 0, 1)
        event_id = uuid.UUID(str(row.get("trace_event_id")))
        return {
            "version": VERSION,
            "status": "ok",
            "outcome": outcome,
            "trace_event_id_sha256": _sha256(event_id),
            "trace_manifest_sha256": _hash(
                row.get("trace_manifest_sha256"), "trace_manifest_sha256"
            ),
            "rows_written": rows_written,
        }
    except Exception as exc:
        return {
            "version": VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
            "rows_written": 0,
        }
