from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Sequence

import asyncpg

from .memory_v1_store import InvalidActor, actor_uuid


SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
RETRIEVABLE_STATUSES = {"supported", "uncertain", "disputed"}


class RetrievalValidationError(RuntimeError):
    pass


def _json_object(value: Any, field: str = "JSON value") -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise RetrievalValidationError(f"{field} must be a JSON object")


def _normalized_entity(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip()


def structured_entity_values(
    object_literal: Any, qualifiers: Any
) -> set[str]:
    """Extract governed entity labels from structured fields, never prose."""
    allowed_keys = {
        "known_name",
        "canonical_name",
        "canonical_value",
        "name",
        "subject",
    }
    values: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key) in allowed_keys and isinstance(child, str):
                    normalized = _normalized_entity(child)
                    if normalized:
                        values.add(normalized)
                if isinstance(child, (Mapping, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(_json_object(object_literal, "object_literal"))
    walk(_json_object(qualifiers, "qualifiers"))
    return values


def _score(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RetrievalValidationError(f"{field} must be numeric") from exc
    if not 0.0 <= parsed <= 1.0:
        raise RetrievalValidationError(f"{field} must be between 0 and 1")
    return parsed


def _candidate_map(candidate_hits: Sequence[Mapping[str, Any]]) -> Dict[uuid.UUID, float]:
    out: Dict[uuid.UUID, float] = {}
    for index, hit in enumerate(candidate_hits):
        if not isinstance(hit, Mapping):
            raise RetrievalValidationError(f"candidate_hits[{index}] must be an object")
        try:
            claim_id = uuid.UUID(str(hit.get("claim_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise RetrievalValidationError(
                f"candidate_hits[{index}].claim_id must be a UUID"
            ) from exc
        semantic_score = _score(hit.get("semantic_score"), "semantic_score")
        out[claim_id] = max(out.get(claim_id, 0.0), semantic_score)
    return out


def _query_hash(actor: uuid.UUID, query: str) -> str:
    material = actor.bytes + b"\0" + query.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _as_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0.0)


def _token_estimate(text: str) -> int:
    # Conservative deterministic estimate until the rendering tokenizer is fixed.
    return max(1, math.ceil(len(text) / 3.5) + 18)


def _policy_values(policy: Dict[str, Any], key: str) -> set[str]:
    value = policy.get(key)
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    raise RetrievalValidationError(f"retrieval_policy.{key} must be a string or list")


def _use_instruction(status: str, surface: str) -> str:
    if status in {"uncertain", "disputed"}:
        return "state_uncertainty_and_material_counterevidence"
    if surface == "normalization":
        return "normalize_memory_without_unprompted_discussion"
    if surface == "support":
        return "supporting_context_only"
    return "answer_directly_if_relevant"


async def _set_actor(conn: asyncpg.Connection, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(actor))


async def build_memory_packet(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    query: str,
    intent: str,
    domain: str,
    candidate_hits: Sequence[Mapping[str, Any]],
    max_claims: int = 4,
    max_tokens: int = 500,
    max_sensitivity: str = "medium",
    explicit_recall: bool = False,
    entity_hints: Sequence[str] = (),
    request_id: str | None = None,
    answer_id: str | uuid.UUID | None = None,
    thread_id: str | uuid.UUID | None = None,
    runtime_activation: bool = False,
    expose_to_answer_model: bool = False,
) -> Dict[str, Any]:
    try:
        actor = actor_uuid(actor_user_id)
    except InvalidActor as exc:
        raise RetrievalValidationError(str(exc)) from exc
    query = " ".join(str(query or "").split()).strip()
    intent = str(intent or "").strip().lower()
    domain = str(domain or "").strip().lower()
    if not query:
        raise RetrievalValidationError("query is required")
    if not intent:
        raise RetrievalValidationError("intent is required")
    if not domain:
        raise RetrievalValidationError("domain is required")
    if not 0 <= int(max_claims) <= 20:
        raise RetrievalValidationError("max_claims must be between 0 and 20")
    if not 0 <= int(max_tokens) <= 4000:
        raise RetrievalValidationError("max_tokens must be between 0 and 4000")
    max_sensitivity = str(max_sensitivity or "medium").strip().lower()
    if max_sensitivity not in SENSITIVITY_RANK:
        raise RetrievalValidationError("invalid max_sensitivity")
    normalized_entity_hints: set[str] = set()
    for value in entity_hints:
        normalized = _normalized_entity(value)
        if normalized:
            normalized_entity_hints.add(normalized)
    if len(normalized_entity_hints) > 8:
        raise RetrievalValidationError("entity_hints must contain at most 8 values")

    candidates = _candidate_map(candidate_hits)
    candidate_ids = list(candidates)
    answer_uuid = uuid.UUID(str(answer_id)) if answer_id else None
    thread_uuid = uuid.UUID(str(thread_id)) if thread_id else None
    now = datetime.now(timezone.utc)

    async with conn.transaction():
        await _set_actor(conn, actor)
        rows = []
        if candidate_ids:
            rows = await conn.fetch(
                """
                SELECT claim_id, canonical_text, status::text, confidence,
                       importance, salience, sensitivity::text,
                       valid_from, valid_to, retrieval_policy,
                       subject_entity_id, predicate, object_entity_id, object_literal,
                       qualifiers,
                       ARRAY(
                         SELECT link.evidence_id
                         FROM memory.claim_evidence AS link
                         JOIN memory.evidence AS evidence
                           ON evidence.owner_user_id=link.owner_user_id
                          AND evidence.evidence_id=link.evidence_id
                         WHERE link.owner_user_id=memory.claim.owner_user_id
                           AND link.claim_id=memory.claim.claim_id
                           AND evidence.status='active'
                         ORDER BY link.evidence_id
                       ) AS active_evidence_ids
                FROM memory.claim
                WHERE owner_user_id=$1
                  AND claim_id = ANY($2::uuid[])
                """,
                actor,
                candidate_ids,
            )

        visible_ids = {uuid.UUID(str(row["claim_id"])) for row in rows}
        rejected_counts: Counter[str] = Counter()
        if len(visible_ids) < len(candidate_ids):
            rejected_counts["not_visible"] += len(candidate_ids) - len(visible_ids)

        evaluated: list[Dict[str, Any]] = []
        for row in rows:
            claim_id = uuid.UUID(str(row["claim_id"]))
            status = str(row["status"])
            sensitivity = str(row["sensitivity"])
            policy = _json_object(row["retrieval_policy"], "retrieval_policy")
            reasons: list[str] = []
            active_evidence_refs = [
                str(evidence_id) for evidence_id in row["active_evidence_ids"]
            ]

            if status not in RETRIEVABLE_STATUSES:
                reasons.append(f"status:{status}")
            if not active_evidence_refs:
                reasons.append("no_active_evidence")

            if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[max_sensitivity]:
                reasons.append("sensitivity")

            valid_from = row["valid_from"]
            valid_to = row["valid_to"]
            if valid_from and now < valid_from:
                reasons.append("not_yet_valid")
            if valid_to and now > valid_to:
                reasons.append("expired")

            policy_domains = _policy_values(policy, "domains")
            policy_intents = _policy_values(policy, "intents")
            surface = str(policy.get("surface") or "support").strip().lower()
            if surface not in {"direct", "support", "normalization", "never"}:
                reasons.append("invalid_surface_policy")
            elif surface == "never":
                reasons.append("surface_never")
            if policy_domains and domain not in policy_domains:
                reasons.append("domain")
            if policy_intents and intent not in policy_intents:
                reasons.append("intent")
            if bool(policy.get("requires_explicit")) and not explicit_recall:
                reasons.append("requires_explicit")

            if normalized_entity_hints:
                claim_entities = structured_entity_values(
                    row["object_literal"], row["qualifiers"]
                )
                if not normalized_entity_hints.intersection(claim_entities):
                    reasons.append("entity")

            semantic = candidates[claim_id]
            confidence = _as_float(row["confidence"])
            importance = _as_float(row["importance"])
            salience = _as_float(row["salience"])
            policy_score = 1.0 if not reasons else 0.0
            final_score = (
                semantic * 0.60
                + confidence * 0.16
                + importance * 0.14
                + salience * 0.10
            )
            text = str(row["canonical_text"])
            evaluated.append(
                {
                    "claim_id": claim_id,
                    "text": text,
                    "status": status,
                    "confidence": confidence,
                    "importance": importance,
                    "salience": salience,
                    "sensitivity": sensitivity,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "semantic_score": semantic,
                    "policy_score": policy_score,
                    "final_score": final_score,
                    "reason_codes": reasons,
                    "surface": surface,
                    "qualifiers": _json_object(row["qualifiers"], "qualifiers"),
                    "evidence_refs": active_evidence_refs,
                    "token_estimate": _token_estimate(text),
                }
            )

        eligible = [item for item in evaluated if not item["reason_codes"]]
        eligible.sort(key=lambda item: (-item["final_score"], str(item["claim_id"])))

        selected_ids: set[uuid.UUID] = set()
        selected: list[Dict[str, Any]] = []
        tokens_used = 0
        for item in eligible:
            if len(selected) >= int(max_claims):
                item["reason_codes"].append("claim_budget")
                rejected_counts["claim_budget"] += 1
                continue
            if tokens_used + item["token_estimate"] > int(max_tokens):
                item["reason_codes"].append("token_budget")
                rejected_counts["token_budget"] += 1
                continue
            selected_ids.add(item["claim_id"])
            tokens_used += item["token_estimate"]
            selected.append(item)

        for item in evaluated:
            for reason in item["reason_codes"]:
                if reason not in {"claim_budget", "token_budget"}:
                    rejected_counts[reason] += 1

        retrieval_activation = bool(runtime_activation and selected)
        prompt_injection = retrieval_activation
        answer_model_exposure = bool(
            retrieval_activation and expose_to_answer_model
        )

        trace_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO memory.retrieval_trace(
              trace_id, owner_user_id, request_id, answer_id, thread_id,
              query_hash, query_preview, intent, domain,
              token_budget, selected_count, metadata
            ) VALUES(
              $1, $2, $3, $4, $5,
              $6, NULL, $7, $8,
              $9, $10, $11::jsonb
            )
            """,
            trace_id,
            actor,
            request_id,
            answer_uuid,
            thread_uuid,
            _query_hash(actor, query),
            intent,
            domain,
            int(max_tokens),
            len(selected),
            json.dumps(
                {
                    "version": "memory_packet_v1",
                    "candidate_count": len(candidate_ids),
                    "visible_candidate_count": len(visible_ids),
                    "max_claims": int(max_claims),
                    "max_sensitivity": max_sensitivity,
                    "explicit_recall": bool(explicit_recall),
                    "entity_hints": sorted(normalized_entity_hints),
                    "prompt_injection": prompt_injection,
                    "answer_model_exposure": answer_model_exposure,
                    "retrieval_activation": retrieval_activation,
                },
                sort_keys=True,
            ),
        )

        selected_rank = {
            item["claim_id"]: index for index, item in enumerate(selected, start=1)
        }
        for item in evaluated:
            await conn.execute(
                """
                INSERT INTO memory.retrieval_trace_item(
                  owner_user_id, trace_id, claim_id, selected, rank,
                  semantic_score, policy_score, final_score,
                  reason_codes, prompt_tokens
                ) VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                actor,
                trace_id,
                item["claim_id"],
                item["claim_id"] in selected_ids,
                selected_rank.get(item["claim_id"]),
                item["semantic_score"],
                item["policy_score"],
                item["final_score"],
                item["reason_codes"],
                item["token_estimate"] if item["claim_id"] in selected_ids else None,
            )

        packet_claims = [
            {
                "claim_id": str(item["claim_id"]),
                "text": item["text"],
                "status": item["status"],
                "confidence": item["confidence"],
                "importance": item["importance"],
                "salience": item["salience"],
                "valid_from": item["valid_from"].isoformat()
                if item["valid_from"]
                else None,
                "valid_to": item["valid_to"].isoformat() if item["valid_to"] else None,
                "use_instruction": _use_instruction(
                    item["status"], item["surface"]
                ),
                "qualifiers": item["qualifiers"],
                "evidence_refs": item["evidence_refs"],
                "score": round(item["final_score"], 6),
            }
            for item in selected
        ]

        return {
            "version": "memory_packet_v1",
            "trace_id": str(trace_id),
            "intent": intent,
            "domain": domain,
            "claims": packet_claims,
            "selected_count": len(packet_claims),
            "candidate_count": len(candidate_ids),
            "visible_candidate_count": len(visible_ids),
            "rejected_counts": dict(sorted(rejected_counts.items())),
            "token_estimate": tokens_used,
            "prompt_injection": prompt_injection,
            "answer_model_exposure": answer_model_exposure,
            "retrieval_activation": retrieval_activation,
        }
