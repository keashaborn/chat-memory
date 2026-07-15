#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


EXPECTED_MANIFEST_SHA256 = (
    "8d31688923f3a0bb82c019b98dc6a78a867129a44157e80efc65432b60d2b649"
)
EXPECTED_CASE_COUNT = 25
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_KEYS = {"case_id", "ordinal", "source", "expected"}
SOURCE_KEYS = {
    "job_id",
    "source_external_id",
    "source_sha256",
    "source_recorded_at",
}
EXPECTED_KEYS = {
    "outcome",
    "required_projection_classes",
    "forbidden_projection_classes",
    "required_entity_roles",
    "required_predicate_families",
    "required_temporal_features",
    "required_comparison_relations",
    "required_deferrals",
    "forbidden_predicates",
    "require_manual_review",
}
OUTCOMES = {"no_observation", "defer_all", "extract", "mixed", "conditional_project"}
PROJECTION_CLASSES = {
    "direct_claim",
    "supportive_context",
    "correction",
    "life_preference",
    "response_preference",
    "project_knowledge",
    "never_surface",
}
RAW_CONTENT_KEYS = {"text", "content", "raw_text", "query", "source_text"}
FORBIDDEN_SCHEMA_PROPERTIES = {
    "owner_user_id",
    "salience",
    "importance",
    "confidence",
    "truth_confidence",
    "write_allowed",
    "approved",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schema",
        default="specs/memory_v1_relational_extraction_v5.schema.json",
    )
    parser.add_argument(
        "--cases",
        default="evals/memory_v1_relational_extraction_v5_cases.jsonl",
    )
    parser.add_argument("--manifest")
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise AssertionError(f"blank JSONL line: {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AssertionError(f"JSONL line is not an object: {line_number}")
        rows.append(value)
    return rows


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def mapping_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        yield from value.keys()
        for child in value.values():
            yield from mapping_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from mapping_keys(child)


def validate_schema(schema: dict[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise AssertionError("unexpected JSON Schema draft")
    if schema.get("additionalProperties") is not False:
        raise AssertionError("packet schema must fail closed on extra properties")
    if schema.get("properties", {}).get("contract_version", {}).get("const") != (
        "memory_v1_relational_extraction_v5"
    ):
        raise AssertionError("contract version is not fixed to V5")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not definitions:
        raise AssertionError("schema definitions are missing")
    for node in walk(schema):
        if isinstance(node, dict) and "$ref" in node:
            reference = node["$ref"]
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise AssertionError(f"non-local schema reference: {reference}")
            if reference[len(prefix) :] not in definitions:
                raise AssertionError(f"unresolved schema reference: {reference}")
        if isinstance(node, dict) and node.get("type") == "object":
            properties = node.get("properties")
            required = node.get("required", [])
            if properties is not None and not set(required).issubset(properties):
                raise AssertionError("schema requires an undefined property")
            if properties is not None and node.get("additionalProperties") is not False:
                raise AssertionError("every packet object must reject extra properties")
    property_names: set[str] = set()
    for node in walk(schema):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            property_names.update(node["properties"])
    forbidden = sorted(property_names & FORBIDDEN_SCHEMA_PROPERTIES)
    if forbidden:
        raise AssertionError(f"forbidden authoritative model properties: {forbidden}")


def validate_cases(cases: list[dict[str, Any]]) -> None:
    if len(cases) != EXPECTED_CASE_COUNT:
        raise AssertionError(f"expected {EXPECTED_CASE_COUNT} cases, got {len(cases)}")
    case_ids: set[str] = set()
    job_ids: set[str] = set()
    source_ids: set[str] = set()
    source_hashes: set[str] = set()
    previous_order: tuple[datetime, str] | None = None
    for expected_ordinal, case in enumerate(cases, 1):
        if set(case) != CASE_KEYS:
            raise AssertionError(f"case keys mismatch: {case.get('case_id')}")
        case_id = case["case_id"]
        if case_id != f"v5-{expected_ordinal:02d}" or case_id in case_ids:
            raise AssertionError(f"case identity/order mismatch: {case_id}")
        case_ids.add(case_id)
        if case["ordinal"] != expected_ordinal:
            raise AssertionError(f"ordinal mismatch: {case_id}")
        source = case["source"]
        if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
            raise AssertionError(f"source keys mismatch: {case_id}")
        job_id = str(uuid.UUID(source["job_id"]))
        source_id = str(uuid.UUID(source["source_external_id"]))
        if job_id in job_ids or source_id in source_ids:
            raise AssertionError(f"duplicate job/source: {case_id}")
        job_ids.add(job_id)
        source_ids.add(source_id)
        digest = source["source_sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AssertionError(f"invalid source digest: {case_id}")
        if digest in source_hashes:
            raise AssertionError(f"duplicate source digest: {case_id}")
        source_hashes.add(digest)
        recorded_at = datetime.fromisoformat(source["source_recorded_at"])
        if recorded_at.tzinfo is None:
            raise AssertionError(f"naive source timestamp: {case_id}")
        order_key = (recorded_at, job_id)
        if previous_order is not None and order_key >= previous_order:
            raise AssertionError(f"cases are not strict newest-first: {case_id}")
        previous_order = order_key

        expected = case["expected"]
        if not isinstance(expected, dict) or set(expected) != EXPECTED_KEYS:
            raise AssertionError(f"expected keys mismatch: {case_id}")
        if expected["outcome"] not in OUTCOMES:
            raise AssertionError(f"invalid outcome: {case_id}")
        required_classes = set(expected["required_projection_classes"])
        forbidden_classes = set(expected["forbidden_projection_classes"])
        if not required_classes.issubset(PROJECTION_CLASSES):
            raise AssertionError(f"invalid required projection class: {case_id}")
        if not forbidden_classes.issubset(PROJECTION_CLASSES):
            raise AssertionError(f"invalid forbidden projection class: {case_id}")
        if required_classes & forbidden_classes:
            raise AssertionError(f"projection class both required and forbidden: {case_id}")
        for key in EXPECTED_KEYS - {"outcome", "require_manual_review"}:
            value = expected[key]
            if not isinstance(value, list) or len(value) != len(set(value)):
                raise AssertionError(f"{key} must be a unique list: {case_id}")
        if not isinstance(expected["require_manual_review"], bool):
            raise AssertionError(f"manual-review flag must be boolean: {case_id}")
        if expected["outcome"] == "no_observation" and required_classes:
            raise AssertionError(f"no_observation case requires a projection: {case_id}")

    raw_keys = set(mapping_keys(cases)) & RAW_CONTENT_KEYS
    if raw_keys:
        raise AssertionError(f"raw-content keys found in repository fixture: {sorted(raw_keys)}")


def validate_manifest(cases: list[dict[str, Any]], path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(stable_json(manifest).encode("utf-8")).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise AssertionError(f"manifest digest mismatch: {digest}")
    manifest_sources = manifest.get("sources")
    if not isinstance(manifest_sources, list) or len(manifest_sources) != len(cases):
        raise AssertionError("manifest source count mismatch")
    for case, source in zip(cases, manifest_sources, strict=True):
        if case["source"] != source:
            raise AssertionError(f"case/manifest source mismatch: {case['case_id']}")


def main() -> int:
    args = arguments()
    schema_path = Path(args.schema)
    cases_path = Path(args.cases)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cases = load_jsonl(cases_path)
    validate_schema(schema)
    validate_cases(cases)
    if args.manifest:
        validate_manifest(cases, Path(args.manifest))
    outcomes = Counter(case["expected"]["outcome"] for case in cases)
    print(
        stable_json(
            {
                "case_count": len(cases),
                "contract_version": "memory_v1_relational_extraction_v5",
                "manifest_verified": bool(args.manifest),
                "manual_review_cases": sum(
                    int(case["expected"]["require_manual_review"]) for case in cases
                ),
                "outcomes": dict(sorted(outcomes.items())),
                "raw_content_in_fixture": False,
                "schema_validated": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
