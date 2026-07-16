from __future__ import annotations

import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

VERSION = "memory_v1_v5_shadow_retrieval_v1"
RETRIEVABLE_STATUSES = {"supported", "uncertain", "disputed"}
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
CONTENT_SURFACES = {
    "direct_or_relevant",
    "relevant_recommendation_or_explicit_recall",
    "exact_project_scope_only",
}
NON_CONTENT_SURFACES = {"never", "zero_token_control_only"}
EVIDENCE_STANCES = {"supports", "opposes", "qualifies", "context"}
MIN_SEMANTIC_SCORE = 0.20
RELATIVE_SEMANTIC_RATIO = 0.40


class V5ShadowRetrievalError(RuntimeError):
    pass


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ShadowRetrievalError(f"{field} must be a UUID") from exc


def _score(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise V5ShadowRetrievalError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise V5ShadowRetrievalError(f"{field} must be between 0 and 1")
    return parsed


def _text(value: Any, field: str, *, limit: int = 4000) -> str:
    parsed = " ".join(str(value or "").split()).strip()
    if not parsed or len(parsed) > limit:
        raise V5ShadowRetrievalError(f"{field} is required and limited to {limit}")
    return parsed


def _time(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise V5ShadowRetrievalError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise V5ShadowRetrievalError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _string_set(value: Any, field: str, *, limit: int = 100) -> set[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise V5ShadowRetrievalError(f"{field} must be a bounded array")
    parsed = {str(item).strip() for item in value if str(item).strip()}
    if len(parsed) != len(value):
        raise V5ShadowRetrievalError(f"{field} contains blank or duplicate values")
    return parsed


def _candidate_map(
    candidate_hits: Sequence[Mapping[str, Any]],
) -> dict[uuid.UUID, float]:
    candidates: dict[uuid.UUID, float] = {}
    for index, hit in enumerate(candidate_hits):
        if not isinstance(hit, Mapping):
            raise V5ShadowRetrievalError(f"candidate_hits[{index}] must be an object")
        claim_id = _uuid(hit.get("claim_id"), f"candidate_hits[{index}].claim_id")
        semantic = _score(
            hit.get("semantic_score"),
            f"candidate_hits[{index}].semantic_score",
        )
        candidates[claim_id] = max(candidates.get(claim_id, 0.0), semantic)
    return candidates


def _evidence_by_stance(value: Any, field: str) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or set(value) != EVIDENCE_STANCES:
        raise V5ShadowRetrievalError(
            f"{field} must contain exactly {sorted(EVIDENCE_STANCES)}"
        )
    return {
        stance: sorted(_string_set(value[stance], f"{field}.{stance}"))
        for stance in sorted(EVIDENCE_STANCES)
    }


def _semantic_floor(values: Sequence[float]) -> float:
    if not values:
        return MIN_SEMANTIC_SCORE
    return max(MIN_SEMANTIC_SCORE, max(values) * RELATIVE_SEMANTIC_RATIO)


def _token_estimate(text: str) -> int:
    return max(1, math.ceil(len(text) / 3.5) + 18)


def _predicate_allowed(predicate: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        predicate == prefix.removesuffix(".") or predicate.startswith(prefix)
        for prefix in prefixes
    )


def _surface_allowed(
    surface: str,
    *,
    intent: str,
    explicit_recall: bool,
    project_key: str | None,
    record_project_key: str | None,
) -> bool:
    if surface in NON_CONTENT_SURFACES:
        return False
    if surface == "direct_or_relevant":
        return True
    if surface == "relevant_recommendation_or_explicit_recall":
        return explicit_recall or intent in {
            "recommendation",
            "personal_recommendation",
            "life_preference_recall",
        }
    if surface == "exact_project_scope_only":
        return bool(project_key and record_project_key == project_key)
    return False


def _use_instruction(status: str, surface: str) -> str:
    if status in {"uncertain", "disputed"}:
        return "state_uncertainty_and_material_counterevidence"
    if surface == "relevant_recommendation_or_explicit_recall":
        return "use_only_for_relevant_recommendation_or_explicit_recall"
    if surface == "exact_project_scope_only":
        return "use_only_inside_exact_project_scope"
    return "answer_directly_only_when_relevant"


def evaluate_v5_shadow_claims(
    actor_user_id: str | uuid.UUID,
    *,
    query: str,
    intent: str,
    domain: str,
    allowed_predicate_prefixes: Sequence[str],
    candidate_hits: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    max_claims: int = 4,
    max_tokens: int = 500,
    max_sensitivity: str = "medium",
    explicit_recall: bool = False,
    project_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    actor = _uuid(actor_user_id, "actor_user_id")
    normalized_query = _text(query, "query", limit=16000)
    normalized_intent = _text(intent, "intent", limit=120).casefold()
    normalized_domain = _text(domain, "domain", limit=120).casefold()
    del normalized_query, normalized_domain  # Reserved for trace binding.
    if not 0 <= int(max_claims) <= 20:
        raise V5ShadowRetrievalError("max_claims must be between 0 and 20")
    if not 0 <= int(max_tokens) <= 4000:
        raise V5ShadowRetrievalError("max_tokens must be between 0 and 4000")
    maximum_sensitivity = str(max_sensitivity or "").strip().casefold()
    if maximum_sensitivity not in SENSITIVITY_RANK:
        raise V5ShadowRetrievalError("invalid max_sensitivity")
    prefixes = tuple(
        sorted(
            {
                str(value).strip().casefold()
                for value in allowed_predicate_prefixes
                if str(value).strip()
            }
        )
    )
    if not prefixes or len(prefixes) > 20:
        raise V5ShadowRetrievalError(
            "allowed_predicate_prefixes must contain 1 to 20 values"
        )
    normalized_project = str(project_key or "").strip() or None
    candidates = _candidate_map(candidate_hits)
    semantic_floor = _semantic_floor(list(candidates.values()))
    evaluated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record_ids: set[uuid.UUID] = set()
    rejected: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise V5ShadowRetrievalError(f"records[{index}] must be an object")
        owner = _uuid(record.get("owner_user_id"), f"records[{index}].owner_user_id")
        if owner != actor:
            raise V5ShadowRetrievalError("owner mismatch in V5 retrieval snapshot")
        claim_id = _uuid(record.get("claim_id"), f"records[{index}].claim_id")
        if claim_id in record_ids:
            raise V5ShadowRetrievalError("duplicate claim in V5 retrieval snapshot")
        record_ids.add(claim_id)
        if claim_id not in candidates:
            raise V5ShadowRetrievalError(
                "V5 retrieval snapshot contains a non-candidate claim"
            )

        canonical_text = _text(
            record.get("canonical_text"),
            f"records[{index}].canonical_text",
        )
        predicate = _text(
            record.get("predicate"),
            f"records[{index}].predicate",
            limit=200,
        ).casefold()
        status = str(record.get("status") or "").strip().casefold()
        sensitivity = str(record.get("sensitivity") or "").strip().casefold()
        if sensitivity not in SENSITIVITY_RANK:
            raise V5ShadowRetrievalError("invalid record sensitivity")
        policy = record.get("retrieval_policy")
        if not isinstance(policy, Mapping):
            raise V5ShadowRetrievalError("retrieval_policy must be an object")
        surface = str(policy.get("surface_policy") or "").strip().casefold()
        record_project = str(record.get("project_key") or "").strip() or None
        evidence_by_stance = _evidence_by_stance(
            record.get("evidence_by_stance"),
            f"records[{index}].evidence_by_stance",
        )
        active_evidence = {
            evidence_id
            for evidence_ids in evidence_by_stance.values()
            for evidence_id in evidence_ids
        }
        observations = _string_set(
            record.get("observation_ids"),
            f"records[{index}].observation_ids",
        )
        semantic = candidates[claim_id]
        importance = _score(record.get("importance"), "importance")
        salience = _score(record.get("salience"), "salience")
        valid_from = _time(record.get("valid_from"), "valid_from")
        valid_to = _time(record.get("valid_to"), "valid_to")
        reasons: list[str] = []

        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            reasons.append("invalid_v5_contract")
        elif metadata.get("memory_contract") != "memory_projection_v5":
            reasons.append("invalid_v5_contract")
        if not str(record.get("canonical_key") or "").startswith("v5:"):
            reasons.append("invalid_v5_contract")
        if record.get("projection_review_decision") != "authorized":
            reasons.append("projection_not_authorized")
        if record.get("projection_apply_outcome") != "applied":
            reasons.append("projection_not_applied")
        if semantic < semantic_floor:
            reasons.append("semantic_relevance")
        if status not in RETRIEVABLE_STATUSES:
            reasons.append(f"status:{status or 'missing'}")
        if not active_evidence:
            reasons.append("no_active_evidence")
        if status == "supported" and not evidence_by_stance["supports"]:
            reasons.append("no_supporting_evidence")
        if not observations:
            reasons.append("no_observation_provenance")
        if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[maximum_sensitivity]:
            reasons.append("sensitivity")
        if valid_from and evaluated_at < valid_from:
            reasons.append("not_yet_valid")
        if valid_to and evaluated_at >= valid_to:
            reasons.append("expired")
        if not _predicate_allowed(predicate, prefixes):
            reasons.append("predicate_permission")
        if surface not in CONTENT_SURFACES | NON_CONTENT_SURFACES:
            reasons.append("invalid_surface_policy")
        elif not _surface_allowed(
            surface,
            intent=normalized_intent,
            explicit_recall=bool(explicit_recall),
            project_key=normalized_project,
            record_project_key=record_project,
        ):
            reasons.append("surface_policy")

        evaluated.append(
            {
                "claim_id": claim_id,
                "canonical_text": canonical_text,
                "predicate": predicate,
                "status": status,
                "surface_policy": surface,
                "semantic_score": semantic,
                "importance": importance,
                "salience": salience,
                "sensitivity": sensitivity,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "evidence_by_stance": evidence_by_stance,
                "observation_refs": sorted(observations),
                "reason_codes": reasons,
                "token_estimate": _token_estimate(canonical_text),
            }
        )

    missing_records = set(candidates) - record_ids
    rejected["not_visible"] += len(missing_records)
    eligible = [record for record in evaluated if not record["reason_codes"]]
    eligible.sort(
        key=lambda record: (
            -record["semantic_score"],
            -record["importance"],
            -record["salience"],
            str(record["claim_id"]),
        )
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[uuid.UUID] = set()
    tokens_used = 0
    for record in eligible:
        if len(selected) >= int(max_claims):
            record["reason_codes"].append("claim_budget")
            continue
        if tokens_used + record["token_estimate"] > int(max_tokens):
            record["reason_codes"].append("token_budget")
            continue
        selected.append(record)
        selected_ids.add(record["claim_id"])
        tokens_used += record["token_estimate"]

    for record in evaluated:
        for reason in record["reason_codes"]:
            rejected[reason] += 1

    claims = [
        {
            "claim_id": str(record["claim_id"]),
            "text": record["canonical_text"],
            "predicate": record["predicate"],
            "status": record["status"],
            "surface_policy": record["surface_policy"],
            "use_instruction": _use_instruction(
                record["status"], record["surface_policy"]
            ),
            "semantic_score": round(record["semantic_score"], 6),
            "importance": record["importance"],
            "salience": record["salience"],
            "valid_from": (
                record["valid_from"].isoformat() if record["valid_from"] else None
            ),
            "valid_to": (
                record["valid_to"].isoformat() if record["valid_to"] else None
            ),
            "evidence_by_stance": record["evidence_by_stance"],
            "observation_refs": record["observation_refs"],
        }
        for record in selected
    ]
    return {
        "version": VERSION,
        "claims": claims,
        "selected_count": len(claims),
        "candidate_count": len(candidates),
        "visible_candidate_count": len(record_ids),
        "semantic_floor": round(semantic_floor, 6),
        "rejected_counts": dict(sorted(rejected.items())),
        "token_estimate": tokens_used,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }
