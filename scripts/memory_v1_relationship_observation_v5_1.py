from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.memory_v1_relationship_policy_v5_1 import (
    DEFAULT_REGISTRY,
    RelationshipDecision,
    assess_relationship_proposal,
    load_registry,
    map_named_party_role,
    normalize_role,
    role_index,
)


POLICY_VERSION = "memory_v1_relationship_observation_v5_1"
SENSITIVITY_ORDER = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
SURFACE_POLICY_ADAPTER = {
    "direct_or_relevant": "direct_or_relevant",
    "explicit_person_or_relationship_context_only": "explicit_recall_only",
    "mention_when_directly_relevant": "mention_when_directly_relevant",
    "restricted_explicit_recall_only": "explicit_recall_only",
}
ROLE_SPLIT_RE = re.compile(r"[:/|]+")


def _pattern(value: str) -> str:
    words = [re.escape(part) for part in value.split("_") if part]
    return r"\b" + r"[\s_-]+".join(words) + r"s?\b"


PREDICATE_EVIDENCE_PATTERNS = {
    "relationship.caregiver_for": r"\b(?:caregiver|carer|care\s+for|caring\s+for)\b",
    "relationship.collaborator_with": r"\bcollaborat(?:e|es|ed|ing|or)s?\b",
    "relationship.has_pet": r"\b(?:pet|pets|dog|dogs|cat|cats|animal|animals)\b",
    "relationship.lives_with": r"\b(?:live|lives|lived|living)\s+(?:together\s+)?with\b",
    "relationship.manager_of": r"\b(?:boss|manager|supervisor|reports?\s+(?:directly\s+)?to|manage[sd]?)\b",
    "relationship.mentor_of": r"\bmentor(?:s|ed|ing)?\b",
    "social.avoids": r"\bavoid(?:s|ed|ing)?\b",
    "social.competes_with": r"\b(?:compet(?:e|es|ed|ing|itor)|rival(?:s|ry)?)\b",
    "social.depends_on": r"\b(?:depend(?:s|ed|ing)?\s+on|rel(?:y|ies|ied|ying)\s+on)\b",
    "social.distrusts": r"\b(?:distrust(?:s|ed|ing)?|do\s+not\s+trust|don['’]?t\s+trust|no\s+longer\s+trust)\b",
    "social.estranged_from": r"\bestrang(?:ed|ement)\b",
    "social.experiences_tension_with": r"\b(?:ongoing\s+)?tension\b",
    "social.feels_close_to": r"\bfeel(?:s|ing)?\s+(?:very\s+|emotionally\s+)?close\s+to\b",
    "social.feels_unsafe_with": r"\bfeel(?:s|ing)?\s+unsafe\b",
    "social.in_conflict_with": r"\b(?:ongoing\s+)?conflict\b",
    "social.no_contact_with": r"\bno\s+contact\b",
    "social.perceives_as_adversary": r"\b(?:consider|considers|considered|regard|regards|regarded|see|sees|saw)\b[^.!?]{0,100}\b(?:enemy|adversary)\b",
    "social.supports": r"\b(?:support|supports|supported|supporting|provides?\s+[^.!?]{0,50}\s+support)\b",
    "social.trusts": r"\btrust(?:s|ed|ing)?\b",
}


@dataclass(frozen=True)
class ObservationDecision:
    status: str
    reason_code: str
    policy_version: str
    normalized_observation: dict[str, Any] | None
    repairs: tuple[str, ...]
    manual_review_required: bool


def relationship_role_candidates(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    candidates: list[str] = []
    for raw in (value, *ROLE_SPLIT_RE.split(value)):
        try:
            candidate = normalize_role(raw)
        except ValueError:
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def map_entity_relationship_role(
    relationship_role: Any,
    *,
    proposed_predicate: str,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> RelationshipDecision:
    accepted: list[RelationshipDecision] = []
    for candidate in relationship_role_candidates(relationship_role):
        decision = map_named_party_role(
            candidate,
            proposed_predicate=proposed_predicate,
            registry_path=registry_path,
        )
        if decision.status == "accept":
            accepted.append(decision)
    mappings = {
        (value.mapping.predicate, value.mapping.edge_direction): value
        for value in accepted
        if value.mapping is not None
    }
    if len(mappings) == 1:
        return next(iter(mappings.values()))
    if len(mappings) > 1:
        return RelationshipDecision(
            status="defer",
            reason_code="ambiguous_relationship_role",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    return RelationshipDecision(
        status="defer",
        reason_code="unregistered_or_mismatched_relationship_role",
        policy_version=POLICY_VERSION,
        mapping=None,
        manual_review_required=True,
    )


def _sentence_context(
    source_spans: Any,
    text: str,
) -> str:
    if not isinstance(source_spans, Sequence) or isinstance(
        source_spans, (str, bytes)
    ):
        return text[:5000]
    starts: list[int] = []
    ends: list[int] = []
    for span in source_spans:
        if not isinstance(span, Mapping):
            continue
        start = span.get("start")
        end = span.get("end")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(text)
        ):
            starts.append(start)
            ends.append(end)
    if not starts:
        return text[:5000]
    start = min(starts)
    end = max(ends)
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start))
    left = 0 if left < 0 else left + 1
    right_values = [
        value
        for value in (text.find(".", end), text.find("!", end), text.find("?", end))
        if value >= 0
    ]
    right = min(right_values) + 1 if right_values else len(text)
    return text[left:right].strip()[:5000]


def _role_supported(role: str, text: str) -> bool:
    return re.search(_pattern(role), text, re.IGNORECASE) is not None


def predicate_supported_by_source(
    predicate: str,
    named_party_role: str,
    text: str,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> bool:
    special = PREDICATE_EVIDENCE_PATTERNS.get(predicate)
    if special is not None and re.search(special, text, re.IGNORECASE):
        return True
    decision = map_named_party_role(
        named_party_role,
        proposed_predicate=predicate,
        registry_path=registry_path,
    )
    return decision.status == "accept" and _role_supported(named_party_role, text)


def _defer(reason_code: str) -> ObservationDecision:
    return ObservationDecision(
        status="defer",
        reason_code=reason_code,
        policy_version=POLICY_VERSION,
        normalized_observation=None,
        repairs=(),
        manual_review_required=True,
    )


def normalize_relationship_observation(
    observation: Mapping[str, Any],
    entity_mentions: Sequence[Mapping[str, Any]],
    text: str,
    *,
    source_class: str,
    explicit_current_state: bool = True,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> ObservationDecision:
    predicate = str(observation.get("predicate", ""))
    registry = load_registry(registry_path)
    contracts = {row["predicate"]: row for row in registry["predicates"]}
    if predicate not in contracts or predicate in registry["forbidden_predicates"]:
        return _defer("unregistered_relationship_predicate")
    obj = observation.get("object")
    if not isinstance(obj, Mapping) or obj.get("kind") != "entity":
        return _defer("relationship_object_must_be_entity")
    subject_ref = observation.get("subject_entity_ref")
    object_ref = obj.get("entity_ref")
    entities = {
        item.get("entity_ref"): item
        for item in entity_mentions
        if isinstance(item, Mapping) and isinstance(item.get("entity_ref"), str)
    }
    if subject_ref not in entities or object_ref not in entities:
        return _defer("relationship_entity_reference_missing")
    if subject_ref == object_ref:
        return _defer("relationship_self_loop_forbidden")
    subject = entities[subject_ref]
    object_entity = entities[object_ref]
    endpoint_values = [subject, object_entity]
    self_entities = [value for value in endpoint_values if value.get("entity_type") == "self"]
    if len(self_entities) != 1:
        return _defer(
            "third_party_relationship_requires_manual_resolution"
            if len(self_entities) == 0
            else "invalid_self_endpoint_count"
        )
    self_entity = self_entities[0]
    named_entity = object_entity if subject is self_entity else subject
    role_decision = map_entity_relationship_role(
        named_entity.get("relationship_role"),
        proposed_predicate=predicate,
        registry_path=registry_path,
    )
    if role_decision.status != "accept" or role_decision.mapping is None:
        return _defer(role_decision.reason_code)
    role = next(
        (
            candidate
            for candidate in relationship_role_candidates(
                named_entity.get("relationship_role")
            )
            if candidate in role_index(registry_path)
            and any(
                mapping.predicate == predicate
                and mapping.edge_direction == role_decision.mapping.edge_direction
                for mapping in role_index(registry_path)[candidate]
            )
        ),
        "",
    )
    context = _sentence_context(observation.get("source_spans"), text)
    if not role or not predicate_supported_by_source(
        predicate, role, context, registry_path=registry_path
    ):
        return _defer("relationship_predicate_not_entailed_by_source")

    direction = role_decision.mapping.edge_direction
    if direction == "named_to_self":
        canonical_subject, canonical_object = named_entity, self_entity
    elif direction in {"self_to_named", "unordered_self_named"}:
        canonical_subject, canonical_object = self_entity, named_entity
    else:
        return _defer("relationship_direction_unresolved")

    proposal = assess_relationship_proposal(
        predicate=predicate,
        named_party_role=role,
        source_class=source_class,
        subject_entity_type=str(canonical_subject.get("entity_type", "")),
        object_entity_type=str(canonical_object.get("entity_type", "")),
        self_endpoint_count=1,
        explicit_current_state=explicit_current_state,
        third_party_edge=False,
        registry_path=registry_path,
    )
    if proposal.status == "defer":
        return _defer(proposal.reason_code)

    normalized = copy.deepcopy(dict(observation))
    repairs: list[str] = []
    if normalized.get("subject_entity_ref") != canonical_subject.get("entity_ref"):
        normalized["subject_entity_ref"] = canonical_subject.get("entity_ref")
        repairs.append("canonical_relationship_direction")
    normalized_object = normalized.get("object")
    if not isinstance(normalized_object, dict):
        return _defer("relationship_object_must_be_entity")
    if normalized_object.get("entity_ref") != canonical_object.get("entity_ref"):
        normalized_object["entity_ref"] = canonical_object.get("entity_ref")
        repairs.append("canonical_relationship_direction")

    contract = contracts[predicate]
    minimum = contract["sensitivity_floor"]
    current = str(normalized.get("sensitivity", ""))
    if current not in SENSITIVITY_ORDER:
        return _defer("relationship_sensitivity_invalid")
    if SENSITIVITY_ORDER[current] < SENSITIVITY_ORDER[minimum]:
        normalized["sensitivity"] = minimum
        repairs.append("relationship_sensitivity_floor")
    required_surface = SURFACE_POLICY_ADAPTER[contract["surface_policy"]]
    if normalized.get("surface_policy") != required_surface:
        normalized["surface_policy"] = required_surface
        repairs.append("relationship_surface_policy")

    if contract["family"] == "relational_state":
        modality = normalized.get("modality")
        if modality == "asserted":
            normalized["modality"] = "reported_observation"
            repairs.append("owner_perspective_modality")
        elif modality not in {"reported_observation", "uncertain", "corrective"}:
            return _defer("relational_state_modality_invalid")
    elif normalized.get("modality") not in {
        "asserted",
        "reported_observation",
        "uncertain",
        "corrective",
    }:
        return _defer("relationship_modality_invalid")

    if normalized.get("polarity") != "affirmed":
        return ObservationDecision(
            status="manual_review",
            reason_code="negated_relationship_requires_reconciliation",
            policy_version=POLICY_VERSION,
            normalized_observation=normalized,
            repairs=tuple(sorted(set(repairs))),
            manual_review_required=True,
        )
    temporal = normalized.get("temporal")
    if not isinstance(temporal, dict):
        return _defer("relationship_temporal_contract_missing")
    if temporal.get("semantic") != "state_validity":
        temporal["semantic"] = "state_validity"
        repairs.append("relationship_temporal_semantic")
    reasons = normalized.get("reason_codes")
    if isinstance(reasons, list) and "relationship_v5_1_governed" not in reasons:
        reasons.append("relationship_v5_1_governed")

    return ObservationDecision(
        status=proposal.status,
        reason_code=proposal.reason_code,
        policy_version=POLICY_VERSION,
        normalized_observation=normalized,
        repairs=tuple(sorted(set(repairs))),
        manual_review_required=proposal.manual_review_required,
    )
