#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from scripts.memory_v1_predicate_registry_v5_1 import (
    DEFAULT_INTEGRATION,
    provider_registry,
    stable_json,
)
from scripts.memory_v1_relationship_observation_v5_1 import (
    normalize_relationship_observation,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_sha256,
    sha256_text,
)


VERSION = "memory_v1_relationship_v5_1_local_synthetic_eval_v1"
MODEL = "qwen3-14b-local-extractor"
MODEL_FILE_SHA256 = (
    "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
)
RUNTIME_REVISION = "llama.cpp-b10066-86a9c79f8"
DEFAULT_CASES = (
    "rel-v5_1-004",
    "rel-v5_1-009",
    "rel-v5_1-019",
    "rel-v5_1-024",
    "rel-v5_1-025",
    "rel-v5_1-028",
    "rel-v5_1-034",
    "rel-v5_1-039",
    "rel-v5_1-043",
    "rel-v5_1-047",
    "rel-v5_1-049",
    "rel-v5_1-057",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18080/v1/chat/completions",
    )
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--max-cases", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    return parser.parse_args()


def load_credential(name: str) -> str:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("systemd credential directory is unavailable")
    if not name or "/" in name or "\\" in name:
        raise RuntimeError("credential name is invalid")
    value = (Path(directory) / name).read_text(encoding="utf-8").strip()
    if not 32 <= len(value) <= 500 or any(character.isspace() for character in value):
        raise RuntimeError("local inference credential is invalid")
    return value


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        case_id = value["case_id"]
        if case_id in rows:
            raise RuntimeError("duplicate relationship synthetic case")
        rows[case_id] = value
    return rows


def source_class(case_id: str) -> str:
    if case_id == "rel-v5_1-043":
        return "technical_discussion"
    if case_id == "rel-v5_1-044":
        return "fiction_or_roleplay"
    if case_id == "rel-v5_1-045":
        return "question_only"
    if case_id == "rel-v5_1-046":
        return "assistant_statement"
    return "owner_assertion"


def required_predicates(expected: dict[str, Any]) -> set[str]:
    if expected["outcome"] == "extract_multiple":
        return set(expected["required_predicates"])
    predicate = expected.get("predicate")
    return {predicate} if isinstance(predicate, str) else set()


def evaluate_packet(
    case: dict[str, Any],
    packet: Any,
) -> dict[str, Any]:
    value = packet.model_dump(mode="json")
    text = case["text"]
    expected = case["expected"]
    entities = value["entity_mentions"]
    raw_relationships = [
        item
        for item in value["observations"]
        if item["predicate"].startswith(("relationship.", "social."))
    ]
    governed: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for observation in raw_relationships:
        decision = normalize_relationship_observation(
            observation,
            entities,
            text,
            source_class=source_class(case["case_id"]),
            explicit_current_state=expected.get("temporal_expectation")
            not in {"none"},
        )
        if decision.normalized_observation is not None and decision.status in {
            "accept",
            "manual_review",
        }:
            governed.append(decision.normalized_observation)
        else:
            rejected.append(
                {
                    "predicate": observation["predicate"],
                    "reason_code": decision.reason_code,
                }
            )

    required = required_predicates(expected)
    forbidden = set(expected.get("forbidden_predicates", []))
    raw_names = {item["predicate"] for item in raw_relationships}
    governed_names = {item["predicate"] for item in governed}
    expects_relationship = expected["outcome"] in {
        "extract",
        "extract_multiple",
        "supersede_state",
    }
    model_failures = sorted((required - raw_names) | (forbidden & raw_names))
    governed_failures = sorted(
        (required - governed_names)
        | (forbidden & governed_names)
        | (set() if expects_relationship else governed_names)
    )
    return {
        "model_passed": not model_failures,
        "governed_passed": not governed_failures,
        "model_failure_predicates": model_failures,
        "governed_failure_predicates": governed_failures,
        "raw_predicates": sorted(raw_names),
        "governed_predicates": sorted(governed_names),
        "rejection_codes": sorted(
            {item["reason_code"] for item in rejected}
        ),
        "raw_relationship_count": len(raw_relationships),
        "governed_relationship_count": len(governed),
    }


def main() -> int:
    args = arguments()
    selected = tuple(args.cases or DEFAULT_CASES)
    if not 1 <= args.max_cases <= 12 or len(selected) > args.max_cases:
        raise RuntimeError("relationship synthetic evaluation is limited to 12 cases")
    if len(selected) != len(set(selected)):
        raise RuntimeError("relationship synthetic cases must be unique")
    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "evals/memory_v1_relationship_v5_1_cases.jsonl")
    unknown = set(selected) - set(cases)
    if unknown:
        raise RuntimeError("unknown relationship synthetic case")
    registry = provider_registry(DEFAULT_INTEGRATION)
    if registry["runtime_active"] is not False:
        raise RuntimeError("synthetic provider registry must remain runtime disabled")
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
        api_key=load_credential(args.credential_name),
        allow_loopback_http=True,
        allow_unauthenticated_loopback=False,
    )
    provider = LocalLlamaCppProvider(
        model=args.model,
        model_file_sha256=MODEL_FILE_SHA256,
        runtime_revision=RUNTIME_REVISION,
        registry=registry,
        transport=transport,
        max_output_tokens=4096,
        timeout_seconds=args.timeout_seconds,
    )
    results: list[dict[str, Any]] = []
    for ordinal, case_id in enumerate(selected, start=1):
        case = cases[case_id]
        source = TrustedExtractionSource.create(
            job_id=str(uuid.UUID(int=ordinal)),
            source_system="public.chat_log",
            source_external_id=str(uuid.UUID(int=1000 + ordinal)),
            source_sha256=sha256_text(case["text"]),
            source_recorded_at="2026-07-20T12:00:00+00:00",
            content=case["text"],
        )
        try:
            packet = provider.extract(source)
            evaluation = evaluate_packet(case, packet)
            error_code = None
        except Exception as exc:  # sanitized diagnostic boundary
            evaluation = {
                "model_passed": False,
                "governed_passed": False,
                "model_failure_predicates": sorted(
                    required_predicates(case["expected"])
                ),
                "governed_failure_predicates": sorted(
                    required_predicates(case["expected"])
                ),
                "raw_predicates": [],
                "governed_predicates": [],
                "rejection_codes": [],
                "raw_relationship_count": 0,
                "governed_relationship_count": 0,
            }
            error_code = type(exc).__name__
        audit = provider.last_audit or {}
        results.append(
            {
                "case_id": case_id,
                "source_sha256": source.source_sha256,
                **evaluation,
                "error_code": error_code,
                "audit": {
                    key: audit.get(key)
                    for key in (
                        "request_sha256",
                        "response_sha256",
                        "response_status",
                        "error_code",
                        "prompt_tokens",
                        "completion_tokens",
                        "policy_compiler_version",
                        "compiler_repairs",
                    )
                },
            }
        )
    report = {
        "version": VERSION,
        "provider_registry_version": registry["registry_version"],
        "provider_registry_sha256": canonical_sha256(registry),
        "case_count": len(results),
        "model_pass_count": sum(item["model_passed"] for item in results),
        "governed_pass_count": sum(
            item["governed_passed"] for item in results
        ),
        "results": results,
        "checks": {
            "local_model_calls": transport.local_model_calls,
            "external_model_calls": 0,
            "database_writes": 0,
            "qdrant_writes": 0,
            "staging_writes": 0,
            "prompt_influence": 0,
        },
    }
    print(stable_json(report))
    return 0 if all(item["governed_passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
