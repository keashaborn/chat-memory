#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


TOP_KEYS = {
    "registry_version",
    "contract_version",
    "status",
    "runtime_active",
    "unknown_predicate_action",
    "object_contracts",
    "predicates",
    "legacy_compatibility",
}
PREDICATE_KEYS = {
    "predicate",
    "subject_entity_types",
    "object_contract",
    "cardinality",
    "relation_semantics",
    "temporal_semantics",
    "modalities",
    "projection_classes",
    "sensitivity_floor",
    "surface_policies",
    "manual_review_rules",
    "description",
}
LEGACY_KEYS = {
    "predicate",
    "object_kind",
    "cardinality",
    "extraction_allowed",
    "retrieval_compatible_during_cutover",
    "successor_predicates",
}
REQUIRED_ACTIVE = {
    "age.reported",
    "credential.reported",
    "health.user_reported_observation",
    "health.user_reported_uncertain_label",
    "identity.name",
    "identity.name_canonical",
    "life_event.died",
    "occupation.works_as",
    "pet.breed",
    "pet.coat_color",
    "pet.eye_color",
    "pet.hearing_status",
    "pet.sex",
    "pet.species",
    "pet.weight_reported",
    "preference.life",
    "preference.response",
    "project.constraint",
    "project.current_state",
    "project.proposed_feature",
    "project.requirement",
    "relationship.has_pet",
    "relationship.parent_of",
    "relationship.sibling_of",
    "residence.lives_at",
}
CURRENT_PRODUCTION_PREDICATES = {
    "did_activity_in_past",
    "does_activity",
    "financial_status",
    "goal",
    "has_children_names",
    "has_name",
    "identity.preferred_name",
    "life_context.active",
    "name.canonical",
    "often_takes_walks",
    "personal_event.occurred",
    "project.current",
    "raises",
    "relationship.kind",
    "spends_time_at",
    "spends_time_on",
    "spent_time_in",
    "spouse_name",
    "stopped_alcohol_use",
}
ENTITY_TYPES = {
    "self",
    "person",
    "animal",
    "organization",
    "place",
    "project",
    "object",
    "concept",
}
TEMPORAL_SEMANTICS = {
    "occurrence",
    "state_validity",
    "planned_time",
    "observation_time",
}
MODALITIES = {
    "asserted",
    "negated",
    "uncertain",
    "corrective",
    "proposed",
    "planned",
    "endorsed",
    "reported_observation",
}
PROJECTION_CLASSES = {
    "direct_claim",
    "supportive_context",
    "correction",
    "life_preference",
    "response_preference",
    "project_knowledge",
    "never_surface",
}
SURFACE_POLICIES = {
    "direct_or_relevant",
    "normalization_only",
    "mention_when_directly_relevant",
    "explicit_recall_only",
    "exact_project_scope_only",
    "relevant_recommendation_or_explicit_recall",
    "zero_token_control_only",
    "never",
}
SENSITIVITY_LEVELS = {"low", "medium", "high", "restricted"}
FORBIDDEN_KEYS = {
    "owner_user_id",
    "salience",
    "importance",
    "truth_confidence",
    "write_allowed",
    "approved",
}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
LEGACY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]*$")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry", default="specs/memory_v1_predicate_registry_v5.json"
    )
    parser.add_argument(
        "--cases", default="evals/memory_v1_relational_extraction_v5_cases.jsonl"
    )
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        yield from value
        for child in value.values():
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def require_unique_strings(value: Any, label: str, *, nonempty: bool = True) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a string list")
    if nonempty and not value:
        raise AssertionError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise AssertionError(f"{label} must contain unique values")
    return set(value)


def validate_object_contracts(contracts: Any) -> set[str]:
    if not isinstance(contracts, dict) or not contracts:
        raise AssertionError("object contracts are missing")
    names = list(contracts)
    if names != sorted(names):
        raise AssertionError("object contracts must be sorted")
    for name, contract in contracts.items():
        if not isinstance(contract, dict) or contract.get("kind") not in {"entity", "literal"}:
            raise AssertionError(f"invalid object contract: {name}")
        if contract["kind"] == "entity":
            if set(contract) != {"kind", "entity_types"}:
                raise AssertionError(f"entity object contract keys mismatch: {name}")
            entity_types = require_unique_strings(contract["entity_types"], name)
            if not entity_types <= ENTITY_TYPES or "self" in entity_types:
                raise AssertionError(f"invalid object entity type: {name}")
        else:
            if set(contract) != {
                "kind",
                "datatype",
                "value_schema",
                "allowed_units",
                "approximate_allowed",
            }:
                raise AssertionError(f"literal object contract keys mismatch: {name}")
            if contract["datatype"] not in {
                "text",
                "number",
                "boolean",
                "date",
                "duration",
                "location",
                "enum",
                "json",
            }:
                raise AssertionError(f"invalid literal datatype: {name}")
            if not isinstance(contract["value_schema"], dict) or not contract["value_schema"]:
                raise AssertionError(f"literal value schema missing: {name}")
            require_unique_strings(contract["allowed_units"], name, nonempty=False)
            if not isinstance(contract["approximate_allowed"], bool):
                raise AssertionError(f"approximate flag must be boolean: {name}")
    return set(names)


def validate_registry(registry: dict[str, Any]) -> None:
    if set(registry) != TOP_KEYS:
        raise AssertionError("registry top-level keys mismatch")
    if registry["registry_version"] != "memory_predicate_registry_v5":
        raise AssertionError("registry version mismatch")
    if registry["contract_version"] != "memory_v1_relational_extraction_v5":
        raise AssertionError("contract version mismatch")
    if registry["status"] != "proposed" or registry["runtime_active"] is not False:
        raise AssertionError("registry must remain proposed and runtime-disabled")
    if registry["unknown_predicate_action"] != "defer_unregistered_predicate":
        raise AssertionError("unknown predicates must fail closed")
    forbidden = sorted(set(walk_keys(registry)) & FORBIDDEN_KEYS)
    if forbidden:
        raise AssertionError(f"forbidden authoritative registry keys: {forbidden}")

    object_contracts = validate_object_contracts(registry["object_contracts"])
    predicates = registry["predicates"]
    if not isinstance(predicates, list):
        raise AssertionError("predicates must be a list")
    names = [row.get("predicate") for row in predicates if isinstance(row, dict)]
    if len(names) != len(predicates) or names != sorted(names) or len(names) != len(set(names)):
        raise AssertionError("active predicates must be unique and sorted")
    if set(names) != REQUIRED_ACTIVE:
        raise AssertionError("active predicate set mismatch")

    by_name: dict[str, dict[str, Any]] = {}
    for row in predicates:
        if set(row) != PREDICATE_KEYS:
            raise AssertionError(f"predicate keys mismatch: {row.get('predicate')}")
        name = row["predicate"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise AssertionError(f"invalid active predicate name: {name}")
        subjects = require_unique_strings(row["subject_entity_types"], name)
        if not subjects <= ENTITY_TYPES:
            raise AssertionError(f"invalid subject entity type: {name}")
        if row["object_contract"] not in object_contracts:
            raise AssertionError(f"unknown object contract: {name}")
        if row["cardinality"] not in {"one_active", "many"}:
            raise AssertionError(f"invalid cardinality: {name}")
        if row["relation_semantics"] not in {"property", "directed", "symmetric"}:
            raise AssertionError(f"invalid relation semantics: {name}")
        if not require_unique_strings(row["temporal_semantics"], name) <= TEMPORAL_SEMANTICS:
            raise AssertionError(f"invalid temporal semantic: {name}")
        if not require_unique_strings(row["modalities"], name) <= MODALITIES:
            raise AssertionError(f"invalid modality: {name}")
        if not require_unique_strings(row["projection_classes"], name) <= PROJECTION_CLASSES:
            raise AssertionError(f"invalid projection class: {name}")
        if row["sensitivity_floor"] not in SENSITIVITY_LEVELS:
            raise AssertionError(f"invalid sensitivity floor: {name}")
        if not require_unique_strings(row["surface_policies"], name) <= SURFACE_POLICIES:
            raise AssertionError(f"invalid surface policy: {name}")
        rules = require_unique_strings(row["manual_review_rules"], name, nonempty=False)
        if any(not LEGACY_NAME_RE.fullmatch(rule) for rule in rules):
            raise AssertionError(f"invalid manual-review rule: {name}")
        if not isinstance(row["description"], str) or not row["description"].strip():
            raise AssertionError(f"missing predicate description: {name}")
        by_name[name] = row

    response = by_name["preference.response"]
    if response["projection_classes"] != ["response_preference"]:
        raise AssertionError("response preference must remain in its own projection class")
    if response["surface_policies"] != ["zero_token_control_only"]:
        raise AssertionError("response preference must compile to zero-token control")
    for name, row in by_name.items():
        if name.startswith("project."):
            if row["subject_entity_types"] != ["project"]:
                raise AssertionError(f"project predicate subject drift: {name}")
            if row["surface_policies"] != ["exact_project_scope_only"]:
                raise AssertionError(f"project scope policy drift: {name}")
            if "trusted_project_scope_required" not in row["manual_review_rules"]:
                raise AssertionError(f"project scope review missing: {name}")
    for name in {
        "health.user_reported_observation",
        "health.user_reported_uncertain_label",
    }:
        row = by_name[name]
        if row["sensitivity_floor"] not in {"high", "restricted"}:
            raise AssertionError(f"health sensitivity downgrade: {name}")
        if "asserted" in row["modalities"]:
            raise AssertionError(f"health report cannot become asserted truth: {name}")
        if not row["manual_review_rules"]:
            raise AssertionError(f"health review rule missing: {name}")

    legacy = registry["legacy_compatibility"]
    if not isinstance(legacy, list):
        raise AssertionError("legacy compatibility must be a list")
    legacy_names = [row.get("predicate") for row in legacy if isinstance(row, dict)]
    if len(legacy_names) != len(legacy) or legacy_names != sorted(legacy_names):
        raise AssertionError("legacy predicates must be unique and sorted")
    if set(legacy_names) != CURRENT_PRODUCTION_PREDICATES:
        raise AssertionError("production legacy predicate coverage mismatch")
    if set(legacy_names) & set(names):
        raise AssertionError("active and legacy predicate sets overlap")
    for row in legacy:
        name = row["predicate"]
        if set(row) != LEGACY_KEYS or not LEGACY_NAME_RE.fullmatch(name):
            raise AssertionError(f"legacy predicate shape mismatch: {name}")
        if row["object_kind"] not in {"entity", "literal"}:
            raise AssertionError(f"legacy object kind mismatch: {name}")
        if row["cardinality"] not in {"one", "many"}:
            raise AssertionError(f"legacy cardinality mismatch: {name}")
        if row["extraction_allowed"] is not False:
            raise AssertionError(f"legacy extraction must be disabled: {name}")
        if row["retrieval_compatible_during_cutover"] is not True:
            raise AssertionError(f"legacy cutover compatibility missing: {name}")
        successors = require_unique_strings(
            row["successor_predicates"], name, nonempty=False
        )
        if not successors <= REQUIRED_ACTIVE:
            raise AssertionError(f"unknown legacy successor: {name}")


def load_case_predicates(path: Path) -> set[str]:
    required: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise AssertionError(f"blank case line: {line_number}")
        row = json.loads(line)
        values = row.get("expected", {}).get("required_predicate_families")
        required |= require_unique_strings(values, f"case {line_number}", nonempty=False)
    return required


def validate_case_coverage(registry: dict[str, Any], cases_path: Path) -> None:
    active = {row["predicate"] for row in registry["predicates"]}
    missing = sorted(load_case_predicates(cases_path) - active)
    if missing:
        raise AssertionError(f"V5 case predicates missing from registry: {missing}")


def main() -> int:
    args = arguments()
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    validate_registry(registry)
    validate_case_coverage(registry, Path(args.cases))
    digest = hashlib.sha256(stable_json(registry).encode("utf-8")).hexdigest()
    print(
        stable_json(
            {
                "active_predicates": len(registry["predicates"]),
                "case_predicates_covered": True,
                "legacy_predicates": len(registry["legacy_compatibility"]),
                "registry_sha256": digest,
                "registry_version": registry["registry_version"],
                "runtime_active": registry["runtime_active"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
