#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.memory_v1_relational_v5_contract_test import load_jsonl, validate_schema


CASE_KEYS = {"case_id", "source", "expected"}
EXPECTED_KEYS = {
    "resolution_outcome",
    "required_entity_roles",
    "allowed_auto_link_bindings",
    "required_review_reasons",
    "model_durable_ids_forbidden",
    "project_creation_forbidden",
    "required_temporal_contracts",
    "forbidden_temporal_inferences",
}
RESOLUTION_OUTCOMES = {
    "none",
    "defer",
    "mixed",
    "manual_review",
    "correction_review",
}
TEMPORAL_CONTRACTS = {
    "calendar_month_range",
    "open_state_validity",
    "source_observation_time",
    "planned_time_not_occurrence",
}
FORBIDDEN_TEMPORAL = {
    "month_as_exact_day",
    "observation_time_as_event_time",
    "planned_as_completed",
    "question_time_as_event_time",
    "transient_state_as_open_validity",
}
BASE_TEMPORAL_MAP = {
    "month_precision_occurrence": "calendar_month_range",
    "open_state_validity": "open_state_validity",
    "current_state_open_validity": "open_state_validity",
    "as_of_source_observation": "source_observation_time",
    "planned_not_completed": "planned_time_not_occurrence",
}
FORBIDDEN_RESOLUTION_SCHEMA_KEYS = {
    "owner_user_id",
    "entity_key",
    "score",
    "confidence",
    "truth_confidence",
    "salience",
    "importance",
    "write_allowed",
    "approved",
}
TEMPORAL_KEYS = {
    "semantic",
    "shape",
    "basis",
    "source_form",
    "certainty",
    "precision",
    "instant",
    "calendar_range",
    "instant_range",
    "relative_offset",
    "recurrence",
    "anchored_to_source_time",
    "normalization_policy_version",
    "reason_codes",
}
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,99}$")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-schema",
        default="specs/memory_v1_relational_extraction_v5.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5.schema.json",
    )
    parser.add_argument(
        "--base-cases",
        default="evals/memory_v1_relational_extraction_v5_cases.jsonl",
    )
    parser.add_argument(
        "--cases",
        default="evals/memory_v1_temporal_entity_resolution_v5_cases.jsonl",
    )
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def property_names(value: Any) -> set[str]:
    names: set[str] = set()
    for node in walk(value):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            names |= set(node["properties"])
    return names


def unique_strings(value: Any, label: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a string list")
    if len(value) != len(set(value)):
        raise AssertionError(f"{label} must contain unique values")
    return set(value)


def validate_resolution_schema(schema: dict[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise AssertionError("unexpected resolution JSON Schema draft")
    if schema.get("additionalProperties") is not False:
        raise AssertionError("resolution packet must reject extra properties")
    if schema.get("properties", {}).get("contract_version", {}).get("const") != (
        "memory_v1_entity_resolution_review_v5"
    ):
        raise AssertionError("resolution contract version mismatch")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not definitions:
        raise AssertionError("resolution definitions are missing")
    for node in walk(schema):
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise AssertionError(f"non-local resolution reference: {ref}")
            if ref.removeprefix("#/$defs/") not in definitions:
                raise AssertionError(f"unresolved resolution reference: {ref}")
        if isinstance(node, dict) and node.get("type") == "object":
            properties = node.get("properties")
            if properties is not None:
                if node.get("additionalProperties") is not False:
                    raise AssertionError("resolution objects must reject extra properties")
                if not set(node.get("required", [])) <= set(properties):
                    raise AssertionError("resolution object requires unknown property")
    forbidden = sorted(property_names(schema) & FORBIDDEN_RESOLUTION_SCHEMA_KEYS)
    if forbidden:
        raise AssertionError(f"forbidden resolver-authoritative properties: {forbidden}")
    feature_properties = set(
        definitions.get("feature_vector", {}).get("properties", {})
    )
    if "score" in feature_properties or "confidence" in feature_properties:
        raise AssertionError("entity features must not collapse into a scalar score")
    resolution_properties = set(
        definitions.get("resolution", {}).get("properties", {})
    )
    required = {
        "entity_ref",
        "mention_sha256",
        "action",
        "decision_state",
        "selected_entity_id",
        "proposed_entity",
        "candidate_set_sha256",
        "candidate_set",
        "review_reason_codes",
        "decision_sha256",
    }
    if resolution_properties != required:
        raise AssertionError("resolution fields mismatch")


def validate_cases(
    cases: list[dict[str, Any]], base_cases: list[dict[str, Any]]
) -> None:
    if len(cases) != 25 or len(base_cases) != 25:
        raise AssertionError("both V5 fixtures must contain 25 cases")
    for ordinal, (case, base) in enumerate(zip(cases, base_cases, strict=True), 1):
        if set(case) != CASE_KEYS:
            raise AssertionError(f"case keys mismatch: {ordinal}")
        if case["case_id"] != f"v5-{ordinal:02d}" or case["case_id"] != base["case_id"]:
            raise AssertionError(f"case identity/order mismatch: {ordinal}")
        if case["source"] != base["source"]:
            raise AssertionError(f"source binding mismatch: {case['case_id']}")
        expected = case["expected"]
        if not isinstance(expected, dict) or set(expected) != EXPECTED_KEYS:
            raise AssertionError(f"expected keys mismatch: {case['case_id']}")
        if expected["resolution_outcome"] not in RESOLUTION_OUTCOMES:
            raise AssertionError(f"resolution outcome mismatch: {case['case_id']}")
        roles = unique_strings(expected["required_entity_roles"], "required roles")
        base_roles = set(base["expected"]["required_entity_roles"])
        if roles != base_roles:
            raise AssertionError(f"entity role drift: {case['case_id']}")
        auto = unique_strings(expected["allowed_auto_link_bindings"], "auto links")
        if not auto <= {"user:self"} or not auto <= roles:
            raise AssertionError(f"unsafe auto-link binding: {case['case_id']}")
        reasons = unique_strings(expected["required_review_reasons"], "review reasons")
        if any(not REASON_RE.fullmatch(reason) for reason in reasons):
            raise AssertionError(f"invalid review reason: {case['case_id']}")
        if expected["model_durable_ids_forbidden"] is not True:
            raise AssertionError(f"model durable IDs allowed: {case['case_id']}")
        if not isinstance(expected["project_creation_forbidden"], bool):
            raise AssertionError(f"project creation flag mismatch: {case['case_id']}")
        if "project:unresolved" in roles:
            if expected["project_creation_forbidden"] is not True:
                raise AssertionError(f"unresolved project may be created: {case['case_id']}")
            if "trusted_project_scope_required" not in reasons:
                raise AssertionError(f"project scope review missing: {case['case_id']}")
        temporal = unique_strings(
            expected["required_temporal_contracts"], "temporal contracts"
        )
        if not temporal <= TEMPORAL_CONTRACTS:
            raise AssertionError(f"unknown temporal contract: {case['case_id']}")
        mapped = {
            BASE_TEMPORAL_MAP[item]
            for item in base["expected"]["required_temporal_features"]
        }
        if not mapped <= temporal:
            raise AssertionError(f"temporal feature drift: {case['case_id']}")
        forbidden = unique_strings(
            expected["forbidden_temporal_inferences"], "forbidden temporal"
        )
        if not forbidden <= FORBIDDEN_TEMPORAL:
            raise AssertionError(f"unknown forbidden temporal rule: {case['case_id']}")
        base_deferrals = set(base["expected"]["required_deferrals"])
        if "question_only" in base_deferrals and "question_time_as_event_time" not in forbidden:
            raise AssertionError(f"question temporal guard missing: {case['case_id']}")
        if "transient_state" in base_deferrals and "transient_state_as_open_validity" not in forbidden:
            raise AssertionError(f"transient temporal guard missing: {case['case_id']}")
        if expected["resolution_outcome"] in {"manual_review", "correction_review"} and not reasons:
            raise AssertionError(f"manual resolution reason missing: {case['case_id']}")

    raw_keys = property_names({"properties": {}})  # typed empty set
    for node in walk(cases):
        if isinstance(node, dict):
            raw_keys |= set(node) & {"text", "content", "raw_text", "query", "source_text"}
    if raw_keys:
        raise AssertionError(f"raw source keys found: {sorted(raw_keys)}")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssertionError("instant must include timezone")
    return parsed


def validate_temporal_value(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != TEMPORAL_KEYS:
        raise AssertionError("temporal value keys mismatch")
    if value["normalization_policy_version"] != "memory_temporal_normalization_v5":
        raise AssertionError("temporal policy mismatch")
    unique_strings(value["reason_codes"], "temporal reasons")
    basis = value["basis"]
    forms = {
        "instant": value["instant"],
        "calendar": value["calendar_range"],
        "instant_range": value["instant_range"],
        "relative": value["relative_offset"],
        "recurring": value["recurrence"],
    }
    present = {name for name, item in forms.items() if item is not None}
    if basis == "none":
        if present or value["shape"] != "none" or value["semantic"] != "none":
            raise AssertionError("none temporal value has content")
    elif basis == "instant":
        allowed = {"instant"} if value["shape"] == "instant" else {"instant_range"}
        if present != allowed:
            raise AssertionError("instant temporal form mismatch")
    elif basis == "calendar":
        if present != {"calendar"} or value["precision"] not in {"day", "month", "year"}:
            raise AssertionError("calendar temporal form mismatch")
    elif basis == "relative":
        if present != {"relative"} or value["source_form"] != "relative":
            raise AssertionError("relative temporal form mismatch")
        if value["anchored_to_source_time"] is not True:
            raise AssertionError("relative temporal value lacks trusted anchor")
    elif basis == "recurring":
        if present != {"recurring"} or value["shape"] != "recurring":
            raise AssertionError("recurring temporal form mismatch")
    else:
        raise AssertionError("unknown temporal basis")
    if value["precision"] == "relative":
        raise AssertionError("relative is not precision")
    if value["instant"] is not None:
        _parse_datetime(value["instant"])
    for key, parser in (("calendar_range", date.fromisoformat), ("instant_range", _parse_datetime)):
        range_value = value[key]
        if range_value is None:
            continue
        if set(range_value) != {"lower", "upper", "bounds"} or range_value["bounds"] != "[)":
            raise AssertionError("temporal range must use canonical half-open bounds")
        lower = parser(range_value["lower"]) if range_value["lower"] else None
        upper = parser(range_value["upper"]) if range_value["upper"] else None
        if lower is not None and upper is not None and upper <= lower:
            raise AssertionError("temporal upper bound must follow lower bound")
    relative = value["relative_offset"]
    if relative is not None:
        if set(relative) != {"direction", "magnitude", "unit", "approximate", "anchor_source"}:
            raise AssertionError("relative offset keys mismatch")
        if relative["magnitude"] <= 0 or relative["anchor_source"] != "evidence_observed_at":
            raise AssertionError("relative offset anchor mismatch")
    recurrence = value["recurrence"]
    if recurrence is not None and recurrence != {"kind": "unspecified_repeated"}:
        raise AssertionError("unsupported recurrence contract")


def main() -> int:
    args = arguments()
    model_schema = json.loads(Path(args.model_schema).read_text(encoding="utf-8"))
    resolution_schema = json.loads(
        Path(args.resolution_schema).read_text(encoding="utf-8")
    )
    base_cases = load_jsonl(Path(args.base_cases))
    cases = load_jsonl(Path(args.cases))
    validate_schema(model_schema)
    validate_resolution_schema(resolution_schema)
    validate_cases(cases, base_cases)
    print(
        stable_json(
            {
                "case_count": len(cases),
                "model_schema_sha256": hashlib.sha256(
                    stable_json(model_schema).encode("utf-8")
                ).hexdigest(),
                "raw_content_in_fixture": False,
                "resolution_schema_sha256": hashlib.sha256(
                    stable_json(resolution_schema).encode("utf-8")
                ).hexdigest(),
                "temporal_entity_fixture_sha256": hashlib.sha256(
                    stable_json(cases).encode("utf-8")
                ).hexdigest(),
                "zero_write": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
