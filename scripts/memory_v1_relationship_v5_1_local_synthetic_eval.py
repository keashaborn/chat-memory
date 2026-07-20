#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import traceback
import uuid
from pathlib import Path
from typing import Any

from scripts.memory_v1_predicate_registry_v5_1 import stable_json
from scripts.memory_v1_predicate_runtime_profile import (
    load_runtime_profile,
)
from scripts.memory_v1_relationship_observation_v5_1 import (
    normalize_relationship_observation,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_sha256,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


VERSION = "memory_v1_relationship_v5_1_local_synthetic_eval_v2"
REPORT_DIRECTORY = Path("/home/ubuntu/brains/snapshots")
REPORT_NAME_RE = re.compile(
    r"^memory_v1_relationship_v5_1_[a-z0-9_]{1,120}\.json$"
)
MODEL = "qwen3-14b-local-extractor"
MODEL_FILE_SHA256 = (
    "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0"
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
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--max-cases", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--report-path", type=Path)
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


def case_source_class(case: dict[str, Any]) -> str:
    explicit = case.get("source_class")
    if explicit is not None:
        if explicit not in {
            "assistant_statement",
            "fiction_or_roleplay",
            "owner_assertion",
            "question_only",
            "technical_discussion",
        }:
            raise RuntimeError("relationship case source_class is invalid")
        return explicit
    return source_class(case["case_id"])


def observation_direction(
    observation: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> str:
    subject = entities.get(str(observation.get("subject_entity_ref")))
    obj = observation.get("object")
    object_entity = (
        entities.get(str(obj.get("entity_ref")))
        if isinstance(obj, dict) and obj.get("kind") == "entity"
        else None
    )
    if subject is None or object_entity is None:
        return "invalid"
    subject_self = subject.get("entity_type") == "self"
    object_self = object_entity.get("entity_type") == "self"
    if subject_self and not object_self:
        return "self_to_named"
    if object_self and not subject_self:
        return "named_to_self"
    return "invalid"


def temporal_expectation_passes(
    observation: dict[str, Any],
    expectation: str,
) -> bool:
    if expectation in {"none", "mixed"}:
        return True
    temporal = observation.get("temporal")
    if not isinstance(temporal, dict):
        return False
    if temporal.get("semantic") != "state_validity":
        return False
    shape = temporal.get("shape")
    if expectation in {"open", "dynamic_open", "event_independent"}:
        return shape == "open_interval"
    if expectation == "closed_or_bounded":
        if shape == "bounded_interval":
            return True
        if shape != "open_interval":
            return False
        instant_range = temporal.get("instant_range")
        calendar_range = temporal.get("calendar_range")
        return bool(
            (
                isinstance(instant_range, dict)
                and instant_range.get("lower") is None
                and instant_range.get("upper") is not None
            )
            or (
                isinstance(calendar_range, dict)
                and calendar_range.get("lower") is None
                and calendar_range.get("upper") is not None
            )
        )
    raise RuntimeError("relationship temporal expectation is invalid")


def required_predicates(expected: dict[str, Any]) -> set[str]:
    if expected["outcome"] == "extract_multiple":
        return set(expected["required_predicates"])
    predicate = expected.get("predicate")
    return {predicate} if isinstance(predicate, str) else set()


def evaluate_packet(
    case: dict[str, Any],
    packet: Any,
) -> dict[str, Any]:
    value = (
        packet.model_dump(mode="json")
        if hasattr(packet, "model_dump")
        else packet
    )
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
            source_class=case_source_class(case),
            explicit_current_state=expected.get("temporal_expectation")
            not in {"none"},
        )
        if decision.normalized_observation is not None and decision.status in {
            "accept",
            "manual_review",
        }:
            governed.append(
                {
                    "observation": decision.normalized_observation,
                    "manual_review_required": (
                        decision.manual_review_required
                        or decision.status == "manual_review"
                    ),
                }
            )
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
    governed_names = {
        item["observation"]["predicate"] for item in governed
    }
    expects_relationship = expected["outcome"] in {
        "extract",
        "extract_multiple",
        "supersede_state",
    }
    model_failures = sorted((required - raw_names) | (forbidden & raw_names))
    governed_failures = set(required - governed_names)
    governed_failures.update(forbidden & governed_names)
    if not expects_relationship:
        governed_failures.update(governed_names)
    expected_direction = expected.get("edge_direction")
    expected_temporal = str(expected.get("temporal_expectation", "none"))
    expected_manual = bool(expected.get("manual_review", False))
    entities_by_ref = {
        str(item.get("entity_ref")): item
        for item in entities
        if isinstance(item, dict)
    }
    governed_required = [
        item for item in governed
        if item["observation"]["predicate"] in required
    ]
    if expects_relationship and expected_direction not in {None, "mixed"}:
        allowed_directions = (
            {"self_to_named"}
            if expected_direction == "unordered_self_named"
            else {str(expected_direction)}
        )
        if any(
            observation_direction(item["observation"], entities_by_ref)
            not in allowed_directions
            for item in governed_required
        ):
            governed_failures.add("relationship_direction")
    if expects_relationship and expected_temporal != "mixed":
        if any(
            not temporal_expectation_passes(
                item["observation"], expected_temporal
            )
            for item in governed_required
        ):
            governed_failures.add("relationship_temporal")
    if expects_relationship and governed_required:
        actual_manual = any(
            item["manual_review_required"] for item in governed_required
        )
        if actual_manual != expected_manual:
            governed_failures.add("relationship_manual_review")
    expected_polarity = expected.get("polarity")
    if expected_polarity is not None and any(
        item["observation"].get("polarity") != expected_polarity
        for item in governed_required
    ):
        governed_failures.add("relationship_polarity")
    allowed_modalities = expected.get("allowed_modalities")
    if allowed_modalities is not None and any(
        item["observation"].get("modality") not in set(allowed_modalities)
        for item in governed_required
    ):
        governed_failures.add("relationship_modality")
    minimum_counts = expected.get("minimum_counts", {})
    for predicate, minimum in minimum_counts.items():
        count = sum(
            item["observation"]["predicate"] == predicate
            for item in governed
        )
        if count < int(minimum):
            governed_failures.add(f"{predicate}:count")
    governed_failures = sorted(governed_failures)
    return {
        "model_passed": not model_failures,
        "governed_passed": not governed_failures,
        "model_failure_predicates": model_failures,
        "governed_failure_predicates": governed_failures,
        "raw_predicates": sorted(raw_names),
        "governed_predicates": sorted(governed_names),
        "governed_directions": sorted(
            {
                observation_direction(item["observation"], entities_by_ref)
                for item in governed
            }
        ),
        "governed_temporal_shapes": sorted(
            {
                str(item["observation"].get("temporal", {}).get("shape"))
                for item in governed
            }
        ),
        "governed_manual_review_count": sum(
            bool(item["manual_review_required"]) for item in governed
        ),
        "rejection_codes": sorted(
            {item["reason_code"] for item in rejected}
        ),
        "raw_relationship_count": len(raw_relationships),
        "governed_relationship_count": len(governed),
    }


def main() -> int:
    args = arguments()
    if args.all_cases and args.cases:
        raise RuntimeError("--all-cases and --case are mutually exclusive")
    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "evals/memory_v1_relationship_v5_1_cases.jsonl")
    selected = tuple(
        sorted(cases)
        if args.all_cases
        else (args.cases or DEFAULT_CASES)
    )
    if not 1 <= args.max_cases <= 200 or len(selected) > args.max_cases:
        raise RuntimeError("relationship synthetic evaluation exceeds case limit")
    if len(selected) != len(set(selected)):
        raise RuntimeError("relationship synthetic cases must be unique")
    unknown = set(selected) - set(cases)
    if unknown:
        raise RuntimeError("unknown relationship synthetic case")
    profile = load_runtime_profile(root, "v5_1")
    registry = load_registry(
        profile.registry_path,
        profile.registry_artifact_sha256,
    )
    schema = load_schema(
        profile.schema_path,
        profile.schema_artifact_sha256,
        expected_contract_version=profile.contract_version,
    )
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
            validated = validate_and_normalize(
                provider,
                source=source,
                registry=registry,
                schema=schema,
                allowed_provider_versions={
                    LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION,
                },
                max_external_model_calls=0,
            )
            evaluation = evaluate_packet(
                case,
                validated.normalized_packet,
            )
            error_code = None
            error_location = None
            error_detail_sha256 = None
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
            frames = traceback.extract_tb(exc.__traceback__)
            error_code = type(exc).__name__
            error_location = frames[-1].name if frames else "unknown"
            error_detail_sha256 = canonical_sha256(str(exc))
        audit = provider.last_audit or {}
        results.append(
            {
                "case_id": case_id,
                "source_sha256": source.source_sha256,
                **evaluation,
                "error_code": error_code,
                "error_location": error_location,
                "error_detail_sha256": error_detail_sha256,
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
                        "validation_exception_class",
                        "validation_error_count",
                        "validation_error_types",
                        "validation_error_locations",
                        "normalized_unknown_deferral_reason_count",
                        "normalized_unknown_deferral_reason_sha256s",
                        "relationship_shape_count",
                        "relationship_shapes",
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
    report_payload = stable_json(report)
    if args.report_path is not None:
        report_path = args.report_path.expanduser()
        if (
            not report_path.is_absolute()
            or report_path.parent.resolve() != REPORT_DIRECTORY.resolve()
            or REPORT_NAME_RE.fullmatch(report_path.name) is None
        ):
            raise RuntimeError("relationship report path is outside the audit directory")
        descriptor = os.open(
            report_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(report_payload)
        print(
            stable_json(
                {
                    "case_count": report["case_count"],
                    "checks": report["checks"],
                    "governed_pass_count": report["governed_pass_count"],
                    "model_pass_count": report["model_pass_count"],
                    "report_path": str(report_path),
                    "report_sha256": hashlib.sha256(
                        report_payload.encode("utf-8")
                    ).hexdigest(),
                    "version": VERSION,
                }
            )
        )
    else:
        print(report_payload)
    return 0 if all(item["governed_passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
