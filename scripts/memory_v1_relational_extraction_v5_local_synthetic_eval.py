#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    CapturingProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "744ce1d466dfe502fb78fd0ba0a996dd723d1593f34bc456c849c20811f99e70"
)
MODEL_FILE_SHA256 = (
    "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
)


@dataclass(frozen=True)
class SyntheticCase:
    case_id: str
    content: str
    required_predicates: frozenset[str] = frozenset()
    forbidden_predicates: frozenset[str] = frozenset()
    required_projection_classes: frozenset[str] = frozenset()
    forbidden_projection_classes: frozenset[str] = frozenset()
    required_deferrals: frozenset[str] = frozenset()
    required_comparison_relations: frozenset[str] = frozenset()
    minimum_observations: int = 0
    maximum_observations: int = 8


CASES = (
    SyntheticCase(
        case_id="identity_name",
        content="My name is Avery.",
        required_predicates=frozenset({"identity.name"}),
        required_projection_classes=frozenset({"direct_claim"}),
        minimum_observations=1,
    ),
    SyntheticCase(
        case_id="question_only",
        content="What is my dog's name?",
        required_deferrals=frozenset({"question_only"}),
        maximum_observations=0,
    ),
    SyntheticCase(
        case_id="transient_state",
        content="I'm tired today.",
        required_deferrals=frozenset({"transient_state"}),
        maximum_observations=0,
    ),
    SyntheticCase(
        case_id="structured_domain",
        content="I ate two eggs and 100 grams of oats for breakfast.",
        required_deferrals=frozenset({"structured_domain"}),
        maximum_observations=0,
    ),
    SyntheticCase(
        case_id="life_preference",
        content="I really love classical music.",
        required_predicates=frozenset({"preference.life"}),
        required_projection_classes=frozenset({"life_preference"}),
        minimum_observations=1,
    ),
    SyntheticCase(
        case_id="response_preference",
        content="Please keep your answers concise and avoid bullet points.",
        required_predicates=frozenset({"preference.response"}),
        required_projection_classes=frozenset({"response_preference"}),
        minimum_observations=1,
    ),
    SyntheticCase(
        case_id="project_requirement",
        content=(
            "For the Atlas project, every memory lookup must enforce owner "
            "isolation."
        ),
        required_predicates=frozenset({"project.requirement"}),
        required_projection_classes=frozenset({"project_knowledge"}),
        required_deferrals=frozenset({"project_scope_unresolved"}),
        minimum_observations=1,
    ),
    SyntheticCase(
        case_id="occupation",
        content="I work as a civil engineer.",
        required_predicates=frozenset({"occupation.works_as"}),
        required_projection_classes=frozenset({"direct_claim"}),
        minimum_observations=1,
    ),
    SyntheticCase(
        case_id="pet_compound",
        content="My dog Koda is a male German Shepherd.",
        required_predicates=frozenset(
            {"identity.name", "pet.breed", "pet.sex", "relationship.has_pet"}
        ),
        required_projection_classes=frozenset({"direct_claim"}),
        minimum_observations=4,
    ),
    SyntheticCase(
        case_id="name_correction",
        content="My dog's name is Koda, not Cody.",
        required_predicates=frozenset({"identity.name_canonical"}),
        required_projection_classes=frozenset({"correction"}),
        required_comparison_relations=frozenset({"corrects", "supersedes"}),
        minimum_observations=1,
    ),
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the synthetic, zero-write local V5 extraction suite."
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18080/v1/chat/completions",
    )
    parser.add_argument("--model", default="qwen3-8b-local-extractor")
    parser.add_argument(
        "--model-file-sha256",
        default=MODEL_FILE_SHA256,
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--case", action="append", dest="selected_cases")
    return parser.parse_args()


def _semantic_failures(case: SyntheticCase, packet: dict) -> list[str]:
    observations = packet["observations"]
    predicates = {item["predicate"] for item in observations}
    projections = {item["projection_class"] for item in observations}
    deferrals = {item["reason_code"] for item in packet["deferrals"]}
    relations = {item["relation_type"] for item in packet["comparison_hints"]}
    failures: list[str] = []
    for item in sorted(case.required_predicates - predicates):
        failures.append(f"missing_predicate:{item}")
    for item in sorted(case.forbidden_predicates & predicates):
        failures.append(f"forbidden_predicate:{item}")
    for item in sorted(case.required_projection_classes - projections):
        failures.append(f"missing_projection:{item}")
    for item in sorted(case.forbidden_projection_classes & projections):
        failures.append(f"forbidden_projection:{item}")
    for item in sorted(case.required_deferrals - deferrals):
        failures.append(f"missing_deferral:{item}")
    for item in sorted(case.required_comparison_relations - relations):
        failures.append(f"missing_comparison:{item}")
    if len(observations) < case.minimum_observations:
        failures.append("too_few_observations")
    if len(observations) > case.maximum_observations:
        failures.append("too_many_observations")
    return failures


def _rejected_packet_shape(packet: object | None) -> dict | None:
    if packet is None:
        return None
    if hasattr(packet, "model_dump"):
        raw = packet.model_dump(mode="json")
    elif isinstance(packet, dict):
        raw = packet
    else:
        return {"packet_type": type(packet).__name__}
    entities = raw.get("entity_mentions", [])
    observations = raw.get("observations", [])
    comparisons = raw.get("comparison_hints", [])
    deferrals = raw.get("deferrals", [])
    return {
        "entity_types": sorted(
            {str(item.get("entity_type")) for item in entities}
        ),
        "predicates": sorted(
            {str(item.get("predicate")) for item in observations}
        ),
        "projection_classes": sorted(
            {str(item.get("projection_class")) for item in observations}
        ),
        "comparison_relations": sorted(
            {str(item.get("relation_type")) for item in comparisons}
        ),
        "deferrals": sorted(
            {str(item.get("reason_code")) for item in deferrals}
        ),
        "anchored_to_source_time_true": sum(
            bool(item.get("temporal", {}).get("anchored_to_source_time"))
            for item in observations
        ),
        "counts": {
            "entities": len(entities),
            "observations": len(observations),
            "comparison_hints": len(comparisons),
            "deferrals": len(deferrals),
        },
    }


def main() -> int:
    args = arguments()
    selected = set(args.selected_cases or ())
    cases = tuple(case for case in CASES if not selected or case.case_id in selected)
    unknown = selected - {case.case_id for case in CASES}
    if unknown:
        raise SystemExit(f"unknown cases: {','.join(sorted(unknown))}")
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(
        root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
        allow_loopback_http=True,
        allow_unauthenticated_loopback=True,
    )
    provider = LocalLlamaCppProvider(
        model=args.model,
        model_file_sha256=args.model_file_sha256,
        runtime_revision="llama.cpp-b10066-86a9c79f8",
        registry=registry,
        transport=transport,
        max_output_tokens=4096,
        timeout_seconds=args.timeout_seconds,
    )
    results: list[dict] = []
    for ordinal, case in enumerate(cases, start=1):
        source = TrustedExtractionSource.create(
            job_id=f"00000000-0000-4000-8000-{ordinal:012d}",
            source_system="public.chat_log",
            source_external_id=f"10000000-0000-4000-8000-{ordinal:012d}",
            source_sha256=sha256_text(case.content),
            source_recorded_at="2026-07-17T12:00:00+00:00",
            content=case.content,
        )
        capturing = CapturingProvider(provider)
        try:
            validated = validate_and_normalize(
                capturing,
                source=source,
                registry=registry,
                schema=schema,
                allowed_provider_versions={
                    LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION,
                },
                max_external_model_calls=0,
            )
            packet = validated.normalized_packet
            semantic_failures = _semantic_failures(case, packet)
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": not semantic_failures,
                    "source_sha256": source.source_sha256,
                    "semantic_failures": semantic_failures,
                    "predicates": sorted(
                        {item["predicate"] for item in packet["observations"]}
                    ),
                    "projection_classes": sorted(
                        {
                            item["projection_class"]
                            for item in packet["observations"]
                        }
                    ),
                    "deferrals": sorted(
                        {item["reason_code"] for item in packet["deferrals"]}
                    ),
                    "comparison_relations": sorted(
                        {
                            item["relation_type"]
                            for item in packet["comparison_hints"]
                        }
                    ),
                    "counts": {
                        "entities": len(packet["entity_mentions"]),
                        "observations": len(packet["observations"]),
                        "comparison_hints": len(packet["comparison_hints"]),
                        "deferrals": len(packet["deferrals"]),
                    },
                    "normalized_packet_sha256": (
                        validated.normalized_packet_sha256
                    ),
                    "provider_output_sha256": validated.provider_output_sha256,
                    "audit": provider.last_audit,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": False,
                    "source_sha256": source.source_sha256,
                    "rejection": {
                        "class": type(exc).__name__,
                        "message_sha256": sha256_text(str(exc)),
                    },
                    "rejected_packet_shape": _rejected_packet_shape(
                        capturing.last_packet
                    ),
                    "audit": provider.last_audit,
                }
            )
    passed = sum(1 for result in results if result["passed"])
    print(
        json.dumps(
            {
                "contract_version": "memory_v1_v5_local_synthetic_eval_v1",
                "passed": passed == len(results),
                "passed_cases": passed,
                "total_cases": len(results),
                "external_model_calls": provider.external_model_calls,
                "local_model_calls": provider.local_model_calls,
                "effects": {
                    "database_writes": 0,
                    "qdrant_writes": 0,
                    "redis_writes": 0,
                    "staging_writes": 0,
                    "claim_writes": 0,
                    "prompt_influence_changes": 0,
                },
                "results": results,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
