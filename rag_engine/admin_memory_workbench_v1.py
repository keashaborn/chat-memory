from __future__ import annotations

"""Owner-only review of immutable local Memory V1 extraction packets."""

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

import asyncpg


SCHEMA = "admin_memory_workbench_v2"
POLICY_VERSION = "memory_owner_packet_feedback_v2"
MAX_SOURCE_CHARS = 6000
MAX_CONTEXT_CHARS = 12000
MAX_ITEMS = 25

DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "context_missing",
        "duplicate_or_repeat",
        "missed_durable_information",
        "incomplete_compound_extraction",
        "incorrect_entity_or_relationship",
        "incorrect_time_or_status",
        "uncertainty_or_attribution_error",
        "wrong_memory_lane",
        "should_not_be_memory",
        "transcription_ambiguity",
        "other",
    }
)


class MemoryWorkbenchError(RuntimeError):
    pass


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _safe_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _entity_labels(packet: Mapping[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    mentions = packet.get("entity_mentions")
    if not isinstance(mentions, list):
        return labels
    for raw in mentions:
        if not isinstance(raw, Mapping):
            continue
        ref = _safe_text(raw.get("entity_ref"), 40)
        if not ref:
            continue
        entity_type = _safe_text(raw.get("entity_type"), 80)
        name = _safe_text(raw.get("name_text"), 180)
        if entity_type == "self":
            labels[ref] = "you"
        elif name:
            labels[ref] = name
        elif entity_type:
            labels[ref] = entity_type.replace("_", " ")
        else:
            labels[ref] = ref
    return labels


def _literal_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return _safe_text(value, 500)
    if isinstance(value, list):
        return ", ".join(filter(None, (_literal_value(item) for item in value)))[:500]
    if not isinstance(value, Mapping):
        return _safe_text(value, 500)
    preferred = (
        "name",
        "title",
        "role",
        "position",
        "topic_text",
        "orientation",
        "context",
        "value",
    )
    parts = []
    for key in preferred:
        if key not in value or value.get(key) in (None, "", [], {}):
            continue
        rendered = _literal_value(value.get(key))
        if rendered:
            parts.append(f"{key.replace('_', ' ')}: {rendered}")
    if not parts:
        for key in sorted(value):
            if value.get(key) in (None, "", [], {}):
                continue
            rendered = _literal_value(value.get(key))
            if rendered:
                parts.append(f"{str(key).replace('_', ' ')}: {rendered}")
            if len(parts) >= 4:
                break
    return _safe_text("; ".join(parts), 500)


def _object_label(raw: Any, entities: Mapping[str, str]) -> str:
    if not isinstance(raw, Mapping):
        return _safe_text(raw, 500)
    kind = _safe_text(raw.get("kind"), 40)
    if kind == "entity":
        ref = _safe_text(
            raw.get("entity_ref") or raw.get("object_entity_ref"),
            40,
        )
        return entities.get(ref, ref or "an entity")
    if kind == "literal":
        return _literal_value(raw.get("value"))
    return _literal_value(raw)


_PREDICATE_LABELS = {
    "identity.name_canonical": "has the canonical name",
    "identity.name_alias": "also uses the name",
    "relationship.spouse_of": "is the spouse of",
    "relationship.parent_of": "is a parent of",
    "relationship.child_of": "is a child of",
    "relationship.sibling_of": "is a sibling of",
    "relationship.friend_of": "is a friend of",
    "relationship.caregiver_for": "provides caregiving for",
    "occupation.works_as": "currently works as",
    "occupation.worked_as": "previously worked as",
    "employment.worked_for": "worked for",
    "stance.reported": "expressed a view about",
}


def _observation_summary(
    raw: Mapping[str, Any],
    entities: Mapping[str, str],
) -> str:
    subject_ref = _safe_text(raw.get("subject_entity_ref"), 40)
    subject = entities.get(subject_ref, subject_ref or "the subject")
    predicate = _safe_text(raw.get("predicate"), 160)
    relation = _PREDICATE_LABELS.get(
        predicate,
        predicate.replace(".", " ").replace("_", " "),
    )
    object_label = _object_label(raw.get("object"), entities)
    polarity = _safe_text(raw.get("polarity"), 40)
    if subject == "you" and polarity not in {"negated", "negative"}:
        owner_phrases = {
            "identity.name_canonical": f"Your canonical name is {object_label}.",
            "identity.name_alias": f"You also use the name {object_label}.",
            "relationship.spouse_of": f"You are the spouse of {object_label}.",
            "relationship.parent_of": f"You are a parent of {object_label}.",
            "relationship.child_of": f"You are a child of {object_label}.",
            "relationship.sibling_of": f"You are a sibling of {object_label}.",
            "relationship.friend_of": f"You are a friend of {object_label}.",
            "relationship.caregiver_for": (
                f"You provide caregiving for {object_label}."
            ),
            "occupation.works_as": f"You currently work as {object_label}.",
            "occupation.worked_as": f"You previously worked as {object_label}.",
            "employment.worked_for": f"You worked for {object_label}.",
            "stance.reported": f"You expressed a view about {object_label}.",
        }
        if predicate in owner_phrases:
            return _safe_text(owner_phrases[predicate], 700)
    if polarity in {"negated", "negative"}:
        return _safe_text(f"{subject} does not {relation} {object_label}", 700)
    return _safe_text(f"{subject} {relation} {object_label}", 700)


_DEFERRAL_LABELS = {
    "question_only": "The GPU treated this as a question, not a durable memory.",
    "structured_domain": (
        "The GPU routed this to structured application data instead of chat memory."
    ),
    "transient_state": "The GPU treated this as temporary rather than durable.",
    "insufficient_evidence": (
        "The GPU found too little durable evidence to create a memory."
    ),
    "context_reference_unresolved": (
        "The GPU could not resolve a contextual reference safely."
    ),
    "context_missing": "The GPU did not receive enough context to interpret this safely.",
}


def _packet_interpretations(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    entities = _entity_labels(packet)
    interpretations: list[dict[str, Any]] = []
    mentions = packet.get("entity_mentions")
    if isinstance(mentions, list):
        for raw in mentions:
            if not isinstance(raw, Mapping):
                continue
            entity_type = _safe_text(raw.get("entity_type"), 100)
            name = _safe_text(raw.get("name_text"), 180)
            if entity_type == "self" and not name:
                continue
            label = name or entity_type.replace("_", " ") or "unnamed entity"
            interpretations.append(
                {
                    "kind": "entity",
                    "code": entity_type or "entity",
                    "summary": f"Recognized {label}.",
                    "confidence": raw.get("extraction_confidence"),
                    "reason_codes": list(raw.get("reason_codes") or [])[:8],
                }
            )
    observations = packet.get("observations")
    if isinstance(observations, list):
        for raw in observations:
            if not isinstance(raw, Mapping):
                continue
            interpretations.append(
                {
                    "kind": "observation",
                    "code": _safe_text(raw.get("predicate"), 160),
                    "summary": _observation_summary(raw, entities),
                    "confidence": raw.get("extraction_confidence"),
                    "reason_codes": list(raw.get("reason_codes") or [])[:8],
                }
            )
    deferrals = packet.get("deferrals")
    if isinstance(deferrals, list):
        for raw in deferrals:
            if not isinstance(raw, Mapping):
                continue
            reason = _safe_text(raw.get("reason_code"), 160)
            interpretations.append(
                {
                    "kind": "deferral",
                    "code": reason or "deferred",
                    "summary": _DEFERRAL_LABELS.get(
                        reason,
                        f"The GPU deferred this item: {reason.replace('_', ' ')}.",
                    ),
                    "confidence": None,
                    "reason_codes": [reason] if reason else [],
                }
            )
    return interpretations[:64]


def _source_context(
    *,
    target: str,
    full_source: Any,
    source_char_start: Any,
    source_char_end: Any,
) -> dict[str, Any]:
    source = str(full_source or "")
    if not source or source == target:
        return {
            "available": False,
            "text": "",
            "truncated": False,
            "target_start": None,
            "target_end": None,
        }
    try:
        target_start = int(source_char_start)
        target_end = int(source_char_end)
    except (TypeError, ValueError):
        target_start = source.find(target)
        target_end = target_start + len(target) if target_start >= 0 else -1
    if (
        target_start < 0
        or target_end <= target_start
        or target_end > len(source)
        or source[target_start:target_end] != target
    ):
        target_start = source.find(target)
        target_end = target_start + len(target) if target_start >= 0 else -1
    if target_start < 0 or target_end <= target_start:
        return {
            "available": True,
            "text": source[:MAX_CONTEXT_CHARS],
            "truncated": len(source) > MAX_CONTEXT_CHARS,
            "target_start": None,
            "target_end": None,
        }
    if len(source) <= MAX_CONTEXT_CHARS:
        return {
            "available": True,
            "text": source,
            "truncated": False,
            "target_start": target_start,
            "target_end": target_end,
        }
    padding = max(0, (MAX_CONTEXT_CHARS - len(target)) // 2)
    window_start = max(0, target_start - padding)
    window_start = min(window_start, len(source) - MAX_CONTEXT_CHARS)
    window_end = min(len(source), window_start + MAX_CONTEXT_CHARS)
    return {
        "available": True,
        "text": source[window_start:window_end],
        "truncated": True,
        "target_start": target_start - window_start,
        "target_end": target_end - window_start,
    }


def _diagnostic_packet(
    packet: Mapping[str, Any],
    *,
    route: str,
    route_reason_code: str | None,
    manual_review_required: bool,
) -> dict[str, Any]:
    def sequence(field: str) -> list[Any]:
        value = packet.get(field)
        return list(value)[:64] if isinstance(value, list) else []

    return {
        "contract_version": _safe_text(packet.get("contract_version"), 200)
        or None,
        "predicate_registry_version": _safe_text(
            packet.get("predicate_registry_version"),
            200,
        )
        or None,
        "manual_review_required": manual_review_required,
        "route": route,
        "route_reason_code": route_reason_code,
        "entity_mentions": sequence("entity_mentions"),
        "observations": sequence("observations"),
        "deferrals": sequence("deferrals"),
        "comparison_hints": sequence("comparison_hints"),
        "packet_findings": sequence("packet_findings"),
    }


def _workbench_item(row: Mapping[str, Any]) -> dict[str, Any]:
    source = str(row.get("source_content") or "")
    packet = row.get("normalized_packet")
    if not isinstance(packet, Mapping):
        packet = {}
    interpretations = _packet_interpretations(packet)
    route = _safe_text(row.get("route"), 120) or "not_routed"
    route_reason_code = (
        _safe_text(row.get("route_reason_code"), 160) or None
    )
    manual_review_required = bool(row.get("manual_review_required"))
    return {
        "packet_id": str(row["packet_id"]),
        "packet_storage_sha256": str(row["packet_storage_sha256"]),
        "source": {
            "text": source[:MAX_SOURCE_CHARS],
            "truncated": len(source) > MAX_SOURCE_CHARS,
            "recorded_at": _iso(row.get("source_recorded_at")),
            "context": _source_context(
                target=source,
                full_source=row.get("source_context_content"),
                source_char_start=row.get("source_char_start"),
                source_char_end=row.get("source_char_end"),
            ),
        },
        "gpu": {
            "interpretations": interpretations,
            "entity_count": len(packet.get("entity_mentions") or []),
            "observation_count": len(packet.get("observations") or []),
            "deferral_count": len(packet.get("deferrals") or []),
            "manual_review_required": manual_review_required,
            "diagnostic_json": _diagnostic_packet(
                packet,
                route=route,
                route_reason_code=route_reason_code,
                manual_review_required=manual_review_required,
            ),
        },
        "routing": {
            "route": route,
            "reason_code": route_reason_code,
        },
        "review": {
            "feedback_id": (
                str(row["feedback_id"]) if row.get("feedback_id") else None
            ),
            "decision": row.get("feedback_decision"),
            "category": row.get("feedback_category"),
            "note": row.get("feedback_note"),
            "created_at": _iso(row.get("feedback_created_at")),
        },
        "created_at": _iso(row.get("packet_created_at")),
    }


async def list_admin_memory_workbench_v1(
    *,
    dsn: str,
    actor_user_id: str,
    state: str = "pending",
    limit: int = 12,
    before_created_at: datetime | None = None,
    before_packet_id: UUID | None = None,
) -> dict[str, Any]:
    actor = UUID(actor_user_id)
    if state not in {"pending", "reviewed", "all"}:
        raise MemoryWorkbenchError("invalid_state")
    if not 1 <= limit <= MAX_ITEMS:
        raise MemoryWorkbenchError("invalid_limit")
    if (before_created_at is None) != (before_packet_id is None):
        raise MemoryWorkbenchError("invalid_cursor")

    conn = await asyncpg.connect(dsn, command_timeout=20)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(actor),
            )
            summary = await conn.fetchrow(
                "SELECT * FROM memory.owner_memory_workbench_summary_v1()"
            )
            rows: Sequence[Mapping[str, Any]] = await conn.fetch(
                """
                SELECT *
                FROM memory.list_owner_memory_workbench_v2($1,$2,$3,$4)
                """,
                state,
                limit,
                before_created_at,
                before_packet_id,
            )
    finally:
        await conn.close()

    items = [_workbench_item(dict(row)) for row in rows]
    next_cursor = None
    if len(items) == limit:
        tail = items[-1]
        next_cursor = {
            "created_at": tail["created_at"],
            "packet_id": tail["packet_id"],
        }
    values = dict(summary or {})
    return {
        "ok": True,
        "schema": SCHEMA,
        "scope": "current_actor",
        "generated_at": _iso(datetime.now(timezone.utc)),
        "state": state,
        "summary": {
            "total": int(values.get("total_count") or 0),
            "pending": int(values.get("pending_count") or 0),
            "correct": int(values.get("correct_count") or 0),
            "not_correct": int(values.get("not_correct_count") or 0),
        },
        "items": items,
        "next_cursor": next_cursor,
    }


async def record_admin_memory_workbench_feedback_v2(
    *,
    dsn: str,
    actor_user_id: str,
    operation_id: UUID,
    packet_id: UUID,
    packet_storage_sha256: str,
    decision: str,
    diagnostic_category: str | None,
    diagnostic_note: str | None,
) -> dict[str, Any]:
    actor = UUID(actor_user_id)
    if decision not in {"correct", "not_correct"}:
        raise MemoryWorkbenchError("invalid_decision")
    category = (diagnostic_category or "").strip() or None
    if decision == "not_correct" and category not in DIAGNOSTIC_CATEGORIES:
        raise MemoryWorkbenchError("invalid_diagnostic_category")
    if decision == "correct" and category is not None:
        raise MemoryWorkbenchError("unexpected_diagnostic_category")
    note = (diagnostic_note or "").strip() or None
    if note is not None and len(note) > 2000:
        raise MemoryWorkbenchError("diagnostic_note_too_long")

    conn = await asyncpg.connect(dsn, command_timeout=20)
    try:
        async with conn.transaction(isolation="serializable"):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(actor),
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM memory.record_owner_memory_workbench_feedback_v2(
                  $1,$2,$3,$4,$5,$6
                )
                """,
                operation_id,
                packet_id,
                packet_storage_sha256,
                decision,
                category,
                note,
            )
    finally:
        await conn.close()
    if row is None:
        raise MemoryWorkbenchError("feedback_not_recorded")
    return {
        "ok": True,
        "schema": SCHEMA,
        "scope": "current_actor",
        "feedback": {
            "feedback_id": str(row["feedback_id"]),
            "decision": row["decision"],
            "category": row["diagnostic_category"],
            "note": row["diagnostic_note"],
            "created_at": _iso(row["created_at"]),
            "outcome": row["apply_outcome"],
        },
    }


__all__ = [
    "MemoryWorkbenchError",
    "DIAGNOSTIC_CATEGORIES",
    "SCHEMA",
    "list_admin_memory_workbench_v1",
    "record_admin_memory_workbench_feedback_v2",
]
