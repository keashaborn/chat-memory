from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Sequence

import asyncpg

from .memory_v1_store import InvalidActor, actor_uuid


VERSION = "memory_v1_preference_project_shadow_v1"
MAX_PREFERENCE_SCAN = 200
MAX_PROJECT_SCAN = 500
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
PREFERENCE_CONTENT_INTENTS = {"preference_recall", "recommendation"}
PROJECT_MEMORY_INTENTS = {
    "project_recall",
    "project_status",
    "project_planning",
    "project_decision",
}
CURRENT_PROJECT_STATES = {"working", "proposed", "ratified"}
PROJECT_MIN_SCORE = 4
GENERIC_PROJECT_KEY_TOKENS = {
    "app",
    "backend",
    "frontend",
    "memory",
    "project",
    "status",
    "system",
    "website",
}
HISTORICAL_QUERY_TERMS = (
    "history",
    "historical",
    "legacy",
    "old system",
    "used to",
    "before",
    "how was",
    "how did",
    "source date",
)
AUTHORITY_RANK = {
    "external_reference": 1,
    "user_reported": 2,
    "system_observed": 3,
    "user_ratified": 4,
    "approved_spec": 5,
}
DOCUMENT_STATE_RANK = {
    "historical": 1,
    "proposed": 2,
    "working": 3,
    "ratified": 4,
}
PROJECT_INTENT_KINDS = {
    "project_planning": {"architecture", "constraint", "decision", "requirement", "roadmap"},
    "project_decision": {"architecture", "constraint", "decision", "requirement"},
    "project_status": {"architecture", "constraint", "decision", "status"},
    "project_recall": {"architecture", "constraint", "decision", "requirement", "roadmap", "status"},
}
STATUS_DIRECTION_TERMS = {"direction", "future", "goal", "roadmap"}
EXPECTED_RLS_TABLES = {
    "user_preference",
    "preference_revision",
    "preference_revision_evidence",
    "project_space",
    "project_knowledge_head",
    "project_knowledge_revision",
    "project_knowledge_revision_evidence",
    "project_knowledge_relation",
    "evidence",
}
STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "i",
    "in",
    "include",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "project",
    "should",
    "that",
    "the",
    "their",
    "this",
    "through",
    "to",
    "user",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}
TOKEN_EQUIVALENTS = {
    "behaviour": "behavior",
    "behavioural": "behavior",
    "behavioral": "behavior",
    "cards": "card",
    "memories": "memory",
    "old": "legacy",
    "preferences": "preference",
    "responses": "response",
    "sites": "website",
}


class SpecializedRetrievalError(RuntimeError):
    pass


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SpecializedRetrievalError(f"{field} must be a JSON object") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise SpecializedRetrievalError(f"{field} must be a JSON object")


def _number(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0.0)


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip()


def _tokens(value: Any) -> set[str]:
    text = _normalized_text(value).replace("a/b", " ab ")
    raw = re.findall(r"[a-z0-9]+", text)
    result: set[str] = set()
    for token in raw:
        token = TOKEN_EQUIVALENTS.get(token, token)
        if token and token not in STOP_WORDS and len(token) > 1:
            result.add(token)
    return result


def _token_estimate(value: Any) -> int:
    if isinstance(value, Mapping):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value or "")
    return max(1, math.ceil(len(text) / 3.5) + 12)


def _parse_time(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SpecializedRetrievalError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sensitivity_allowed(value: Any, maximum: str) -> bool:
    sensitivity = str(value or "").strip().lower()
    if sensitivity not in SENSITIVITY_RANK:
        return False
    return SENSITIVITY_RANK[sensitivity] <= SENSITIVITY_RANK[maximum]


def _evidence_is_active(record: Mapping[str, Any]) -> bool:
    linked = int(record.get("evidence_link_count") or 0)
    active = int(record.get("active_evidence_count") or 0)
    return linked > 0 and active == linked


def _preference_chain_is_current(record: Mapping[str, Any]) -> bool:
    return (
        str(record.get("current_revision_id") or "")
        == str(record.get("revision_id") or "")
        and int(record.get("head_revision_number") or 0)
        == int(record.get("revision_number") or -1)
        and str(record.get("head_content_sha256") or "")
        == str(record.get("content_sha256") or "")
        and str(record.get("head_accepted_review_id") or "")
        == str(record.get("accepted_review_id") or "")
    )


def _project_match(
    query_tokens: set[str], record: Mapping[str, Any]
) -> tuple[int, int, int, set[str]]:
    key_tokens = _tokens(str(record.get("knowledge_key") or "").replace("_", " ").replace(".", " "))
    text_tokens = _tokens(record.get("canonical_text"))
    matched_key_tokens = query_tokens.intersection(key_tokens)
    text_overlap = len(query_tokens.intersection(text_tokens))
    key_overlap = len(matched_key_tokens)
    score = 3 * key_overlap + text_overlap
    return score, key_overlap, text_overlap, matched_key_tokens


def _history_requested(query: str) -> bool:
    normalized = _normalized_text(query)
    return any(term in normalized for term in HISTORICAL_QUERY_TERMS)


def _project_kind_allowed(
    memory_intent: str, query_tokens: set[str], knowledge_kind: str
) -> bool:
    allowed = set(PROJECT_INTENT_KINDS.get(memory_intent, set()))
    if memory_intent == "project_status" and query_tokens.intersection(
        STATUS_DIRECTION_TERMS
    ):
        allowed.add("roadmap")
    return knowledge_kind in allowed


def _preference_result(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "preference_id": str(record["preference_id"]),
        "revision_id": str(record["revision_id"]),
        "preference_key": str(record["preference_key"]),
        "preference_class": str(record["preference_class"]),
        "preference_domain": str(record["preference_domain"]),
        "value": _json_object(record.get("value"), "preference.value"),
        "polarity": str(record["polarity"]),
        "stability": str(record["stability"]),
        "surface_policy": str(record["surface_policy"]),
        "sensitivity": str(record["sensitivity"]),
        "content_sha256": str(record["content_sha256"]),
        "evidence_refs": sorted(str(value) for value in record.get("evidence_ids") or []),
    }


def _project_result(record: Mapping[str, Any], score: int) -> Dict[str, Any]:
    return {
        "project_id": str(record["project_id"]),
        "project_key": str(record["project_key"]),
        "knowledge_id": str(record["knowledge_id"]),
        "revision_id": str(record["revision_id"]),
        "knowledge_key": str(record["knowledge_key"]),
        "knowledge_kind": str(record["knowledge_kind"]),
        "canonical_text": str(record["canonical_text"]),
        "document_state": str(record["document_state"]),
        "authority_level": str(record["authority_level"]),
        "sensitivity": str(record["sensitivity"]),
        "content_sha256": str(record["content_sha256"]),
        "lexical_score": int(score),
        "evidence_refs": sorted(str(value) for value in record.get("evidence_ids") or []),
    }


def evaluate_specialized_memory(
    *,
    preferences: Sequence[Mapping[str, Any]],
    project_records: Sequence[Mapping[str, Any]],
    query: str,
    memory_intent: str,
    domains: Sequence[str] = (),
    project_key: str | None = None,
    candidate_entities: Sequence[str] = (),
    direct_relevance: bool = False,
    max_sensitivity: str = "high",
    max_preferences: int = 3,
    max_project_records: int = 3,
    max_tokens: int = 500,
    as_of: str | datetime | None = None,
) -> Dict[str, Any]:
    query = " ".join(str(query or "").split()).strip()
    memory_intent = str(memory_intent or "").strip().lower()
    if not query:
        raise SpecializedRetrievalError("query is required")
    if not memory_intent:
        raise SpecializedRetrievalError("memory_intent is required")
    if max_sensitivity not in SENSITIVITY_RANK:
        raise SpecializedRetrievalError("invalid max_sensitivity")
    if not 0 <= int(max_preferences) <= 20:
        raise SpecializedRetrievalError("max_preferences must be between 0 and 20")
    if not 0 <= int(max_project_records) <= 20:
        raise SpecializedRetrievalError("max_project_records must be between 0 and 20")
    if not 0 <= int(max_tokens) <= 4000:
        raise SpecializedRetrievalError("max_tokens must be between 0 and 4000")

    normalized_domains = {
        _normalized_text(value) for value in domains if _normalized_text(value)
    }
    normalized_entities = {
        _normalized_text(value)
        for value in candidate_entities
        if _normalized_text(value)
    }
    if len(normalized_domains) > 8 or len(normalized_entities) > 16:
        raise SpecializedRetrievalError("domain or entity input exceeds limit")
    evaluation_time = _parse_time(as_of, "as_of") or datetime.now(timezone.utc)

    rejected: Counter[str] = Counter()
    controls: list[Dict[str, Any]] = []
    preference_candidates: list[Dict[str, Any]] = []

    for record in preferences:
        reasons: list[str] = []
        if str(record.get("status") or "") != "active":
            reasons.append("status")
        if not _preference_chain_is_current(record):
            reasons.append("stale_revision")
        if not _evidence_is_active(record):
            reasons.append("inactive_evidence")
        if not _sensitivity_allowed(record.get("sensitivity"), max_sensitivity):
            reasons.append("sensitivity")
        preference_class = str(record.get("preference_class") or "")
        surface = str(record.get("surface_policy") or "")
        scope = _json_object(record.get("scope"), "preference.scope")

        if reasons:
            rejected.update(reasons)
            continue

        if preference_class == "response":
            if surface == "never_surface_as_content":
                scoped_people = {
                    _normalized_text(value)
                    for value in scope.get("people", [])
                    if _normalized_text(value)
                }
                matched = sorted(scoped_people.intersection(normalized_entities))
                if not matched:
                    rejected["control_scope"] += 1
                    continue
                controls.append(
                    {
                        "preference_id": str(record["preference_id"]),
                        "preference_key": str(record["preference_key"]),
                        "action": "require_direct_relevance",
                        "matched_entities": matched,
                        "would_suppress": not bool(direct_relevance),
                        "surface_policy": surface,
                        "content_tokens": 0,
                    }
                )
                continue
            rejected["response_control_not_compiled"] += 1
            continue

        if preference_class != "life":
            rejected["preference_class"] += 1
            continue
        if memory_intent not in PREFERENCE_CONTENT_INTENTS:
            rejected["preference_intent"] += 1
            continue
        preference_domain = _normalized_text(record.get("preference_domain"))
        if preference_domain not in normalized_domains:
            rejected["preference_domain"] += 1
            continue
        context = _normalized_text(scope.get("context"))
        if (
            memory_intent == "recommendation"
            and context
            and context != f"{preference_domain}_recommendation"
        ):
            rejected["preference_scope"] += 1
            continue
        if surface not in {"mention_when_relevant", "explicit_recall_only"}:
            rejected["preference_surface"] += 1
            continue
        if surface == "explicit_recall_only" and memory_intent != "preference_recall":
            rejected["requires_explicit_recall"] += 1
            continue
        item = _preference_result(record)
        item["token_estimate"] = _token_estimate(item["value"])
        preference_candidates.append(item)

    preference_candidates.sort(key=lambda item: item["preference_key"])
    selected_preferences: list[Dict[str, Any]] = []
    tokens_used = 0
    for item in preference_candidates:
        if len(selected_preferences) >= int(max_preferences):
            rejected["preference_budget"] += 1
            continue
        if tokens_used + item["token_estimate"] > int(max_tokens):
            rejected["token_budget"] += 1
            continue
        selected_preferences.append(item)
        tokens_used += item["token_estimate"]

    scored_projects: list[tuple[int, int, int, Mapping[str, Any]]] = []
    if memory_intent in PROJECT_MEMORY_INTENTS and project_key:
        query_tokens = _tokens(query)
        historical_requested = _history_requested(query)
        for record in project_records:
            reasons: list[str] = []
            if str(record.get("project_key") or "") != str(project_key):
                reasons.append("project_key")
            if not _evidence_is_active(record):
                reasons.append("inactive_evidence")
            if not _sensitivity_allowed(record.get("sensitivity"), max_sensitivity):
                reasons.append("sensitivity")
            if int(record.get("relation_count") or 0) > 0:
                reasons.append("relations_require_later_contract")
            state = str(record.get("document_state") or "")
            authority = str(record.get("authority_level") or "")
            knowledge_kind = str(record.get("knowledge_kind") or "")
            if state == "superseded":
                reasons.append("superseded")
            if state not in DOCUMENT_STATE_RANK:
                reasons.append("document_state")
            if state == "historical" and not historical_requested:
                reasons.append("historical_not_requested")
            if memory_intent in {"project_planning", "project_decision"} and state not in CURRENT_PROJECT_STATES:
                reasons.append("historical_not_current")
            if authority not in AUTHORITY_RANK:
                reasons.append("authority")
            if not _project_kind_allowed(memory_intent, query_tokens, knowledge_kind):
                reasons.append("project_kind")
            if not direct_relevance:
                reasons.append("not_directly_relevant")
            effective_from = _parse_time(record.get("effective_from"), "effective_from")
            effective_to = _parse_time(record.get("effective_to"), "effective_to")
            if effective_from and evaluation_time < effective_from:
                reasons.append("not_yet_effective")
            if effective_to and evaluation_time >= effective_to:
                reasons.append("no_longer_effective")
            score, key_overlap, _, matched_key_tokens = _project_match(
                query_tokens, record
            )
            if key_overlap == 0:
                reasons.append("project_key_anchor")
            elif key_overlap == 1 and matched_key_tokens.issubset(
                GENERIC_PROJECT_KEY_TOKENS
            ):
                reasons.append("project_generic_key_anchor")
            if score < PROJECT_MIN_SCORE:
                reasons.append("project_min_score")
            if reasons:
                rejected.update(reasons)
                continue
            scored_projects.append(
                (
                    score,
                    DOCUMENT_STATE_RANK[state],
                    AUTHORITY_RANK[authority],
                    record,
                )
            )
    elif project_records:
        rejected["project_intent"] += len(project_records)

    selected_projects: list[Dict[str, Any]] = []
    if scored_projects:
        scored_projects.sort(
            key=lambda value: (
                -value[0],
                -value[1],
                -value[2],
                str(value[3].get("knowledge_key") or ""),
            )
        )
        relative_floor = max(2, math.ceil(scored_projects[0][0] * 0.65))
        for score, _, _, record in scored_projects:
            if score < relative_floor:
                rejected["project_relative_score"] += 1
                continue
            if len(selected_projects) >= int(max_project_records):
                rejected["project_budget"] += 1
                continue
            item = _project_result(record, score)
            item["token_estimate"] = _token_estimate(item["canonical_text"])
            if tokens_used + item["token_estimate"] > int(max_tokens):
                rejected["token_budget"] += 1
                continue
            selected_projects.append(item)
            tokens_used += item["token_estimate"]

    controls.sort(key=lambda item: item["preference_key"])
    return {
        "version": VERSION,
        "status": "shadow_evaluated",
        "memory_intent": memory_intent,
        "domains": sorted(normalized_domains),
        "project_key": project_key,
        "policy_controls": controls,
        "selected_preferences": selected_preferences,
        "selected_project_records": selected_projects,
        "selected_content_count": len(selected_preferences) + len(selected_projects),
        "token_estimate": tokens_used,
        "rejected_counts": dict(sorted(rejected.items())),
        "prompt_injection": False,
        "retrieval_activation": False,
        "database_writes": 0,
    }


async def load_specialized_snapshot(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    project_key: str,
) -> Dict[str, Any]:
    try:
        actor = actor_uuid(actor_user_id)
    except InvalidActor as exc:
        raise SpecializedRetrievalError(str(exc)) from exc
    project_key = str(project_key or "").strip()
    if not project_key:
        raise SpecializedRetrievalError("project_key is required")

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))
        effective_role = str(await conn.fetchval("SELECT current_user"))
        transaction_read_only = str(
            await conn.fetchval("SELECT current_setting('transaction_read_only')")
        )
        if effective_role != "brains_app":
            raise SpecializedRetrievalError("effective role must be brains_app")
        if transaction_read_only != "on":
            raise SpecializedRetrievalError("retrieval transaction must be read-only")

        rls_rows = await conn.fetch(
            """
            SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory' AND c.relname=ANY($1::text[])
            """,
            sorted(EXPECTED_RLS_TABLES),
        )
        rls = {
            str(row["relname"]): bool(row["relrowsecurity"])
            and bool(row["relforcerowsecurity"])
            for row in rls_rows
        }
        if set(rls) != EXPECTED_RLS_TABLES or not all(rls.values()):
            raise SpecializedRetrievalError("specialized retrieval RLS controls changed")

        preference_rows = await conn.fetch(
            """
            SELECT h.preference_id,h.status::text,h.preference_class,
                   h.preference_domain,h.preference_key,h.value,h.polarity,h.scope,
                   h.stability,h.surface_policy,h.sensitivity::text,
                   h.current_revision_id,h.revision_number AS head_revision_number,
                   h.content_sha256 AS head_content_sha256,
                   h.accepted_review_id AS head_accepted_review_id,
                   r.revision_id,r.revision_number,r.content_sha256,
                   r.accepted_review_id,
                   ARRAY(
                     SELECT link.evidence_id
                     FROM memory.preference_revision_evidence link
                     WHERE link.owner_user_id=h.owner_user_id
                       AND link.revision_id=r.revision_id
                     ORDER BY link.evidence_id
                   ) AS evidence_ids,
                   (SELECT count(*)
                    FROM memory.preference_revision_evidence link
                    WHERE link.owner_user_id=h.owner_user_id
                      AND link.revision_id=r.revision_id) AS evidence_link_count,
                   (SELECT count(*)
                    FROM memory.preference_revision_evidence link
                    JOIN memory.evidence e
                      ON e.owner_user_id=link.owner_user_id
                     AND e.evidence_id=link.evidence_id
                    WHERE link.owner_user_id=h.owner_user_id
                      AND link.revision_id=r.revision_id
                      AND e.status='active') AS active_evidence_count
            FROM memory.user_preference h
            JOIN memory.preference_revision r
              ON r.owner_user_id=h.owner_user_id
             AND r.preference_id=h.preference_id
             AND r.revision_id=h.current_revision_id
            WHERE h.owner_user_id=$1
            ORDER BY h.preference_key
            LIMIT $2
            """,
            actor,
            MAX_PREFERENCE_SCAN + 1,
        )
        project_rows = await conn.fetch(
            """
            SELECT s.project_id,s.project_key,h.knowledge_id,h.knowledge_key,
                   h.knowledge_kind,r.revision_id,r.revision_number,
                   r.canonical_text,r.content_sha256,r.document_state,
                   r.authority_level,r.authority_source,r.effective_from,
                   r.effective_to,r.sensitivity::text,
                   (SELECT count(*)
                    FROM memory.project_knowledge_relation relation
                    WHERE relation.owner_user_id=h.owner_user_id
                      AND relation.project_id=h.project_id
                      AND (relation.from_knowledge_id=h.knowledge_id
                           OR relation.to_knowledge_id=h.knowledge_id)) AS relation_count,
                   ARRAY(
                     SELECT link.evidence_id
                     FROM memory.project_knowledge_revision_evidence link
                     WHERE link.owner_user_id=h.owner_user_id
                       AND link.project_id=h.project_id
                       AND link.revision_id=r.revision_id
                     ORDER BY link.evidence_id
                   ) AS evidence_ids,
                   (SELECT count(*)
                    FROM memory.project_knowledge_revision_evidence link
                    WHERE link.owner_user_id=h.owner_user_id
                      AND link.project_id=h.project_id
                      AND link.revision_id=r.revision_id) AS evidence_link_count,
                   (SELECT count(*)
                    FROM memory.project_knowledge_revision_evidence link
                    JOIN memory.evidence e
                      ON e.owner_user_id=link.owner_user_id
                     AND e.evidence_id=link.evidence_id
                    WHERE link.owner_user_id=h.owner_user_id
                      AND link.project_id=h.project_id
                      AND link.revision_id=r.revision_id
                      AND e.status='active') AS active_evidence_count
            FROM memory.project_space s
            JOIN memory.project_knowledge_head h
              ON h.owner_user_id=s.owner_user_id AND h.project_id=s.project_id
            JOIN LATERAL (
              SELECT r0.*
              FROM memory.project_knowledge_revision r0
              WHERE r0.owner_user_id=h.owner_user_id
                AND r0.project_id=h.project_id
                AND r0.knowledge_id=h.knowledge_id
              ORDER BY r0.revision_number DESC
              LIMIT 1
            ) r ON true
            WHERE s.owner_user_id=$1 AND s.project_key=$2
            ORDER BY h.knowledge_key
            LIMIT $3
            """,
            actor,
            project_key,
            MAX_PROJECT_SCAN + 1,
        )
        if len(preference_rows) > MAX_PREFERENCE_SCAN:
            raise SpecializedRetrievalError(
                "preference scan limit exceeded; indexed candidate generation required"
            )
        if len(project_rows) > MAX_PROJECT_SCAN:
            raise SpecializedRetrievalError(
                "project scan limit exceeded; indexed candidate generation required"
            )

        return {
            "owner_user_id": str(actor),
            "project_key": project_key,
            "preferences": [dict(row) for row in preference_rows],
            "project_records": [dict(row) for row in project_rows],
            "controls": {
                "effective_role_brains_app": True,
                "transaction_read_only": True,
                "forced_rls": True,
            },
            "database_writes": 0,
        }
