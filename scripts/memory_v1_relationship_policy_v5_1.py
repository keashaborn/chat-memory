from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


POLICY_VERSION = "memory_v1_relationship_policy_v5_1"
REGISTRY_VERSION = "memory_relationship_registry_v5_1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "specs/memory_v1_relationship_registry_v5_1.json"
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
ALLOWED_SOURCE_CLASSES = {"owner_assertion", "owner_correction"}
REJECTED_SOURCE_CLASSES = {
    "assistant_statement",
    "fiction_or_roleplay",
    "metaphor_or_non_person",
    "question_only",
    "quoted_unendorsed",
    "technical_discussion",
}


@dataclass(frozen=True)
class RoleMapping:
    predicate: str
    edge_direction: str
    family: str
    perspective: str
    sensitivity_floor: str
    surface_policy: str
    temporal_profile: str
    manual_review_rules: tuple[str, ...]


@dataclass(frozen=True)
class RelationshipDecision:
    status: str
    reason_code: str
    policy_version: str
    mapping: RoleMapping | None
    manual_review_required: bool


def normalize_role(role: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", role.strip().lower()).strip("_")
    if not ROLE_RE.fullmatch(value):
        raise ValueError("invalid relationship role")
    return value


@lru_cache(maxsize=4)
def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if registry.get("registry_version") != REGISTRY_VERSION:
        raise ValueError("relationship registry version mismatch")
    if registry.get("runtime_active") is not False:
        raise ValueError("offline relationship policy cannot load an active registry")
    return registry


@lru_cache(maxsize=4)
def role_index(path: str | Path = DEFAULT_REGISTRY) -> dict[str, tuple[RoleMapping, ...]]:
    output: dict[str, list[RoleMapping]] = {}
    for row in load_registry(path)["predicates"]:
        base = {
            "predicate": row["predicate"],
            "family": row["family"],
            "perspective": row["perspective"],
            "sensitivity_floor": row["sensitivity_floor"],
            "surface_policy": row["surface_policy"],
            "temporal_profile": row["temporal_profile"],
            "manual_review_rules": tuple(row["manual_review_rules"]),
        }
        for field, direction in (
            ("named_party_subject_roles", "named_to_self"),
            ("named_party_object_roles", "self_to_named"),
            ("named_party_symmetric_roles", "unordered_self_named"),
        ):
            for role in row[field]:
                output.setdefault(role, []).append(
                    RoleMapping(edge_direction=direction, **base)
                )
    return {role: tuple(values) for role, values in output.items()}


def map_named_party_role(
    role: str,
    *,
    proposed_predicate: str | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> RelationshipDecision:
    try:
        normalized = normalize_role(role)
    except (AttributeError, TypeError, ValueError):
        return RelationshipDecision(
            status="defer",
            reason_code="invalid_relationship_role",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    candidates = list(role_index(registry_path).get(normalized, ()))
    if proposed_predicate is not None:
        candidates = [
            value for value in candidates if value.predicate == proposed_predicate
        ]
    if not candidates:
        return RelationshipDecision(
            status="defer",
            reason_code="unregistered_or_mismatched_relationship_role",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if len(candidates) != 1:
        return RelationshipDecision(
            status="defer",
            reason_code="ambiguous_relationship_role",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    mapping = candidates[0]
    return RelationshipDecision(
        status="accept",
        reason_code="canonical_relationship_role",
        policy_version=POLICY_VERSION,
        mapping=mapping,
        manual_review_required=False,
    )


def assess_relationship_proposal(
    *,
    predicate: str,
    named_party_role: str,
    source_class: str,
    subject_entity_type: str,
    object_entity_type: str,
    self_endpoint_count: int,
    explicit_current_state: bool = True,
    third_party_edge: bool = False,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> RelationshipDecision:
    registry = load_registry(registry_path)
    by_name = {row["predicate"]: row for row in registry["predicates"]}
    if predicate in registry["forbidden_predicates"] or predicate not in by_name:
        return RelationshipDecision(
            status="defer",
            reason_code="unregistered_relationship_predicate",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if source_class in REJECTED_SOURCE_CLASSES:
        return RelationshipDecision(
            status="defer",
            reason_code=f"ineligible_relationship_source:{source_class}",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=source_class
            in {"assistant_statement", "fiction_or_roleplay", "quoted_unendorsed"},
        )
    if source_class not in ALLOWED_SOURCE_CLASSES:
        return RelationshipDecision(
            status="defer",
            reason_code="unknown_relationship_source_class",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    role_decision = map_named_party_role(
        named_party_role,
        proposed_predicate=predicate,
        registry_path=registry_path,
    )
    if role_decision.status != "accept" or role_decision.mapping is None:
        return role_decision
    row = by_name[predicate]
    if subject_entity_type not in row["subject_entity_types"]:
        return RelationshipDecision(
            status="defer",
            reason_code="relationship_subject_type_mismatch",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if object_entity_type not in row["object_entity_types"]:
        return RelationshipDecision(
            status="defer",
            reason_code="relationship_object_type_mismatch",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if self_endpoint_count not in {0, 1}:
        return RelationshipDecision(
            status="defer",
            reason_code="invalid_self_endpoint_count",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if row["family"] == "relational_state" and self_endpoint_count != 1:
        return RelationshipDecision(
            status="defer",
            reason_code="owner_perspective_requires_self_endpoint",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    if row["temporal_profile"] == "dynamic_state" and not explicit_current_state:
        return RelationshipDecision(
            status="defer",
            reason_code="dynamic_state_not_explicit",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    rules = set(row["manual_review_rules"])
    manual = (
        third_party_edge
        or self_endpoint_count == 0
        or row["sensitivity_floor"] in {"high", "restricted"}
        or bool(
            rules
            & {
                "explicit_owner_stance_required",
                "explicit_current_state_required",
                "health_context",
                "high_stakes_personal_safety",
                "interpersonal_support_context_required",
                "legal_status_claim",
                "more_specific_relation_preferred",
                "professional_confidentiality",
                "restricted_explicit_recall_only",
                "self_endpoint_required",
            }
        )
    )
    return RelationshipDecision(
        status="manual_review" if manual else "accept",
        reason_code=(
            "relationship_manual_review_required"
            if manual
            else "relationship_entailment_accepted"
        ),
        policy_version=POLICY_VERSION,
        mapping=role_decision.mapping,
        manual_review_required=manual,
    )
