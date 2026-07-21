#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "memory_v1_predicate_registry_v5_2.json"
SCHEMA = ROOT / "memory_v1_relational_extraction_v5_2.schema.json"

EXPECTED_NEW = {
    "education.attended",
    "employment.worked_for",
    "stance.reported",
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
    "reported_belief",
}
PROJECTION_CLASSES = {
    "direct_claim",
    "supportive_context",
    "correction",
    "life_preference",
    "response_preference",
    "project_knowledge",
    "reported_stance",
    "never_surface",
}
SURFACE_POLICIES = {
    "direct_or_relevant",
    "normalization_only",
    "mention_when_directly_relevant",
    "explicit_recall_only",
    "exact_project_scope_only",
    "relevant_recommendation_or_explicit_recall",
    "relevant_recall_or_explicit_recall",
    "zero_token_control_only",
    "never",
}


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def schema_enum(schema: dict, name: str) -> set[str]:
    values = schema["$defs"]["observation"]["properties"][name]["enum"]
    if not isinstance(values, list) or len(values) != len(set(values)):
        raise AssertionError(f"invalid schema enum: {name}")
    return set(values)


def main() -> int:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    if registry["registry_version"] != "memory_predicate_registry_v5_2":
        raise AssertionError("registry version mismatch")
    if registry["contract_version"] != "memory_v1_relational_extraction_v5_2":
        raise AssertionError("registry contract mismatch")
    if schema["properties"]["contract_version"]["const"] != (
        "memory_v1_relational_extraction_v5_2"
    ):
        raise AssertionError("schema contract mismatch")
    if schema["properties"]["predicate_registry_version"] != {
        "const": "memory_predicate_registry_v5_2"
    }:
        raise AssertionError("schema registry binding mismatch")
    if registry["runtime_active"] is not False or registry["status"] != "proposed":
        raise AssertionError("V5.2 must remain inactive and proposed")
    if registry["unknown_predicate_action"] != "defer_unregistered_predicate":
        raise AssertionError("unknown predicates must fail closed")

    contracts = registry["object_contracts"]
    if list(contracts) != sorted(contracts):
        raise AssertionError("object contracts are not sorted")
    for required in {"entity.organization", "literal.reported_stance"}:
        if required not in contracts:
            raise AssertionError(f"missing object contract: {required}")

    predicates = registry["predicates"]
    names = [row["predicate"] for row in predicates]
    if names != sorted(names) or len(names) != len(set(names)):
        raise AssertionError("predicates are not sorted and unique")
    if not EXPECTED_NEW <= set(names):
        raise AssertionError("V5.2 predicate coverage is incomplete")

    by_name = {row["predicate"]: row for row in predicates}
    for row in predicates:
        if not set(row["modalities"]) <= MODALITIES:
            raise AssertionError(f"unknown modality: {row['predicate']}")
        if not set(row["projection_classes"]) <= PROJECTION_CLASSES:
            raise AssertionError(f"unknown projection class: {row['predicate']}")
        if not set(row["surface_policies"]) <= SURFACE_POLICIES:
            raise AssertionError(f"unknown surface policy: {row['predicate']}")
        if row["object_contract"] not in contracts:
            raise AssertionError(f"unknown object contract: {row['predicate']}")

    stance = by_name["stance.reported"]
    if stance["modalities"] != ["reported_belief", "uncertain"]:
        raise AssertionError("reported stance modality drifted")
    if stance["projection_classes"] != ["reported_stance"]:
        raise AssertionError("reported stance collapsed into factual claims")
    if stance["surface_policies"] != ["relevant_recall_or_explicit_recall"]:
        raise AssertionError("reported stance surface policy drifted")
    stance_schema = contracts["literal.reported_stance"]["value_schema"]
    if set(stance_schema["required"]) != {
        "topic_key", "topic_text", "position", "orientation", "context"
    }:
        raise AssertionError("reported stance object is incomplete")

    for predicate in ("education.attended", "employment.worked_for"):
        row = by_name[predicate]
        if row["object_contract"] != "entity.organization":
            raise AssertionError(f"organization relation drifted: {predicate}")
        if row["projection_classes"] != ["direct_claim"]:
            raise AssertionError(f"relation is not a direct claim: {predicate}")

    if schema_enum(schema, "modality") != MODALITIES:
        raise AssertionError("schema modality enum differs from registry contract")
    if schema_enum(schema, "projection_class") != PROJECTION_CLASSES:
        raise AssertionError("schema projection enum differs from registry contract")
    if schema_enum(schema, "surface_policy") != SURFACE_POLICIES:
        raise AssertionError("schema surface enum differs from registry contract")

    print(stable_json({
        "contract_version": registry["contract_version"],
        "new_predicates": sorted(EXPECTED_NEW),
        "predicate_count": len(predicates),
        "registry_sha256": hashlib.sha256(stable_json(registry).encode()).hexdigest(),
        "status": "PASS",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
