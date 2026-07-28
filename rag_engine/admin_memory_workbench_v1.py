from __future__ import annotations

"""Owner-only review of immutable local Memory V1 extraction packets."""

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

import asyncpg


SCHEMA = "admin_memory_workbench_v1"
POLICY_VERSION = "memory_owner_packet_feedback_v1"
MAX_SOURCE_CHARS = 6000
MAX_ITEMS = 25


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


def _workbench_item(row: Mapping[str, Any]) -> dict[str, Any]:
    source = str(row.get("source_content") or "")
    packet = row.get("normalized_packet")
    if not isinstance(packet, Mapping):
        packet = {}
    interpretations = _packet_interpretations(packet)
    return {
        "packet_id": str(row["packet_id"]),
        "packet_storage_sha256": str(row["packet_storage_sha256"]),
        "source": {
            "text": source[:MAX_SOURCE_CHARS],
            "truncated": len(source) > MAX_SOURCE_CHARS,
            "recorded_at": _iso(row.get("source_recorded_at")),
        },
        "gpu": {
            "interpretations": interpretations,
            "entity_count": len(packet.get("entity_mentions") or []),
            "observation_count": len(packet.get("observations") or []),
            "deferral_count": len(packet.get("deferrals") or []),
            "manual_review_required": bool(row.get("manual_review_required")),
        },
        "routing": {
            "route": _safe_text(row.get("route"), 120) or "not_routed",
            "reason_code": _safe_text(row.get("route_reason_code"), 160) or None,
        },
        "review": {
            "feedback_id": (
                str(row["feedback_id"]) if row.get("feedback_id") else None
            ),
            "decision": row.get("feedback_decision"),
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
                FROM memory.list_owner_memory_workbench_v1($1,$2,$3,$4)
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


async def record_admin_memory_workbench_feedback_v1(
    *,
    dsn: str,
    actor_user_id: str,
    operation_id: UUID,
    packet_id: UUID,
    packet_storage_sha256: str,
    decision: str,
    diagnostic_note: str | None,
) -> dict[str, Any]:
    actor = UUID(actor_user_id)
    if decision not in {"correct", "not_correct"}:
        raise MemoryWorkbenchError("invalid_decision")
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
                FROM memory.record_owner_memory_workbench_feedback_v1(
                  $1,$2,$3,$4,$5
                )
                """,
                operation_id,
                packet_id,
                packet_storage_sha256,
                decision,
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
            "note": row["diagnostic_note"],
            "created_at": _iso(row["created_at"]),
            "outcome": row["apply_outcome"],
        },
    }


__all__ = [
    "MemoryWorkbenchError",
    "SCHEMA",
    "list_admin_memory_workbench_v1",
    "record_admin_memory_workbench_feedback_v1",
]
