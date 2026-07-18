#!/usr/bin/env python3
"""Apply hash-locked predicate review decisions to an immutable V5 packet."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import stat
from typing import Any
import uuid

from scripts.memory_v1_relational_extraction_v5_provider import (
    NormalizedPacket,
    canonical_sha256,
)
from scripts.memory_v1_v5_review_local_packet import (
    BUNDLE_CONTRACT,
    LocalPacketReviewError,
    REVIEW_NAMESPACE,
    _output_path,
    _repository_state,
    _secure_root,
    _secure_write,
    _sha256_valid,
    packet_quality_findings,
)
from scripts.memory_v1_v5_stage_preflight import (
    REQUEST_NAMESPACE,
    RESOLVER_VERSION,
    _schema_hash,
    _validate_extraction_packet,
    _validate_resolution_packet,
    sha256_file,
    sha256_text,
    stable_json,
)


DECISION_CONTRACT = "memory_v1_v5_local_predicate_review_decision_v1"
REVIEW_CONTRACT = "memory_v1_v5_local_predicate_review_apply_v1"
CONFIRMATION = "RECLASSIFY_HASH_LOCKED_OBSERVATIONS_ONLY"
DECISION_NAMESPACE = uuid.UUID("8eb60df2-f014-557d-b335-9ab0de47a2e8")
ALLOWED_RECLASSIFICATIONS = {
    ("pet.species", "pet.breed"): {
        "required_blocker": "open_pet_species_domain_review_required",
        "replacement_reason_code": "reviewed_pet_breed_reclassification",
        "remove_reason_codes": {"explicit_pet_species"},
    },
}
DECISION_KEYS = {
    "action",
    "expected_object_value_sha256",
    "expected_observation_sha256",
    "from_predicate",
    "observation_ref",
    "replacement_reason_code",
    "to_predicate",
}
TOP_KEYS = {
    "confirmation",
    "contract_version",
    "decision_id",
    "decision_sha256",
    "decisions",
    "owner_user_id",
    "packet_id",
    "reviewer_ref",
    "reviewer_type",
    "source_review_report_sha256",
    "source_stage_bundle_sha256",
}


class PredicateReviewError(LocalPacketReviewError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply one exact, hash-locked predicate review to a zero-write local "
            "packet bundle and emit a new zero-write staging preflight."
        )
    )
    parser.add_argument("--source-review-report", required=True)
    parser.add_argument("--source-stage-bundle", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--review-report", required=True)
    parser.add_argument("--stage-bundle", required=True)
    parser.add_argument("--review-root", default="/home/ubuntu/memory-v1-reviews")
    parser.add_argument(
        "--predicate-registry",
        default="specs/memory_v1_predicate_registry_v5.json",
    )
    parser.add_argument(
        "--extraction-schema",
        default="specs/memory_v1_relational_extraction_v5.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5.schema.json",
    )
    return parser.parse_args()


def _secure_input(path_value: str, root: Path) -> Path:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise PredicateReviewError("review input is outside the restricted review root")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PredicateReviewError("review input must be a mode-0600 regular file")
    return path


def _json_file(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PredicateReviewError(f"{label} is not a JSON object")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PredicateReviewError(f"{label} fields do not match the contract")
    return value


def _predicate_rules(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    raw = path.read_bytes()
    registry = json.loads(raw)
    if (
        not isinstance(registry, dict)
        or registry.get("registry_version") != "memory_predicate_registry_v5"
        or not isinstance(registry.get("predicates"), list)
    ):
        raise PredicateReviewError("predicate registry is invalid")
    rules = {
        item["predicate"]: item
        for item in registry["predicates"]
        if isinstance(item, dict) and isinstance(item.get("predicate"), str)
    }
    return rules, sha256_text(raw.decode("utf-8"))


def _policy_signature(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: rule[key]
        for key in (
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
        )
    }


def decision_body(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in sorted(TOP_KEYS - {"decision_sha256"})}


def decision_identity_body(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in sorted(TOP_KEYS - {"decision_id", "decision_sha256"})
    }


def expected_decision_id(value: dict[str, Any]) -> str:
    return str(
        uuid.uuid5(
            DECISION_NAMESPACE,
            canonical_sha256(decision_identity_body(value)),
        )
    )


def apply_predicate_decisions(
    packet: dict[str, Any],
    decisions: list[dict[str, Any]],
    predicate_rules: dict[str, dict[str, Any]],
    blocking_codes: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not decisions or len(decisions) > 16:
        raise PredicateReviewError("predicate review must contain 1-16 decisions")
    derived = copy.deepcopy(packet)
    observations = {
        item["observation_ref"]: item for item in derived["observations"]
    }
    if len(observations) != len(derived["observations"]):
        raise PredicateReviewError("observation references are not unique")
    seen: set[str] = set()
    transformations: list[dict[str, Any]] = []
    for index, raw_decision in enumerate(decisions):
        decision = _exact(raw_decision, DECISION_KEYS, f"decision {index}")
        observation_ref = decision["observation_ref"]
        if observation_ref in seen:
            raise PredicateReviewError("predicate review repeats an observation")
        seen.add(observation_ref)
        observation = observations.get(observation_ref)
        if observation is None:
            raise PredicateReviewError("predicate review observation is absent")
        if decision["action"] != "reclassify_predicate":
            raise PredicateReviewError("predicate review action is unsupported")
        pair = (decision["from_predicate"], decision["to_predicate"])
        policy = ALLOWED_RECLASSIFICATIONS.get(pair)
        if policy is None:
            raise PredicateReviewError("predicate reclassification is not allowlisted")
        if policy["required_blocker"] not in blocking_codes:
            raise PredicateReviewError("required semantic blocker is absent")
        if decision["replacement_reason_code"] != policy["replacement_reason_code"]:
            raise PredicateReviewError("replacement reason code is not governed")
        if observation["predicate"] != pair[0]:
            raise PredicateReviewError("source predicate differs from decision")
        if canonical_sha256(observation) != decision["expected_observation_sha256"]:
            raise PredicateReviewError("observation hash differs from decision")
        object_value = observation.get("object")
        if (
            not isinstance(object_value, dict)
            or object_value.get("kind") != "literal"
            or canonical_sha256(object_value.get("value"))
            != decision["expected_object_value_sha256"]
        ):
            raise PredicateReviewError("literal value hash differs from decision")
        source_rule = predicate_rules.get(pair[0])
        target_rule = predicate_rules.get(pair[1])
        if source_rule is None or target_rule is None:
            raise PredicateReviewError("predicate is absent from governed registry")
        if _policy_signature(source_rule) != _policy_signature(target_rule):
            raise PredicateReviewError(
                "reclassification changes persistence or resolution policy"
            )

        original_sha = canonical_sha256(observation)
        observation["predicate"] = pair[1]
        observation["reason_codes"] = sorted(
            (
                set(observation["reason_codes"])
                - set(policy["remove_reason_codes"])
            )
            | {decision["replacement_reason_code"]}
        )
        transformations.append(
            {
                "action": decision["action"],
                "from_predicate": pair[0],
                "to_predicate": pair[1],
                "observation_ref": observation_ref,
                "original_observation_sha256": original_sha,
                "derived_observation_sha256": canonical_sha256(observation),
                "object_value_sha256": decision["expected_object_value_sha256"],
                "reason_code": decision["replacement_reason_code"],
            }
        )
    return derived, transformations


def main() -> int:
    args = arguments()
    root = _secure_root(args.review_root)
    source_review_path = _secure_input(args.source_review_report, root)
    source_bundle_path = _secure_input(args.source_stage_bundle, root)
    decision_path = _secure_input(args.decision, root)
    output_review_path = _output_path(args.review_report, root)
    output_bundle_path = _output_path(args.stage_bundle, root)
    repo_root, commit = _repository_state()

    source_review = _json_file(source_review_path, "source review")
    source_bundle = _json_file(source_bundle_path, "source stage bundle")
    decision = _exact(_json_file(decision_path, "decision"), TOP_KEYS, "decision")
    if (
        decision["contract_version"] != DECISION_CONTRACT
        or decision["confirmation"] != CONFIRMATION
        or decision["reviewer_type"] != "owner_authorized_operator"
        or not isinstance(decision["decisions"], list)
        or not isinstance(decision["reviewer_ref"], str)
        or not decision["reviewer_ref"].strip()
    ):
        raise PredicateReviewError("predicate review authorization is invalid")
    for field in (
        "decision_sha256",
        "source_review_report_sha256",
        "source_stage_bundle_sha256",
    ):
        if not _sha256_valid(decision[field]):
            raise PredicateReviewError(f"predicate review {field} is invalid")
    try:
        uuid.UUID(decision["owner_user_id"])
        uuid.UUID(decision["packet_id"])
        uuid.UUID(decision["decision_id"])
    except (TypeError, ValueError):
        raise PredicateReviewError("predicate review identifiers are invalid") from None
    if decision["decision_id"] != expected_decision_id(decision):
        raise PredicateReviewError("predicate review decision ID is not deterministic")
    if canonical_sha256(decision_body(decision)) != decision["decision_sha256"]:
        raise PredicateReviewError("predicate review decision hash mismatch")
    if decision["source_review_report_sha256"] != sha256_file(source_review_path):
        raise PredicateReviewError("source review report hash mismatch")
    if decision["source_stage_bundle_sha256"] != sha256_file(source_bundle_path):
        raise PredicateReviewError("source stage bundle hash mismatch")
    if (
        source_review.get("contract_version")
        != "memory_v1_v5_local_packet_review_v1"
        or source_review.get("mode") != "owner_scoped_local_packet_review_zero_write"
        or source_review.get("review_disposition") != "manual_review_required"
        or source_bundle.get("contract_version") != BUNDLE_CONTRACT
        or source_bundle.get("mode") != "preflight_only_zero_write"
        or source_bundle.get("authorized_stage") is not False
    ):
        raise PredicateReviewError("source review artifacts are not eligible")
    if (
        decision["owner_user_id"] != source_review["owner_user_id"]
        or decision["owner_user_id"] != source_bundle["owner_user_id"]
        or decision["packet_id"] != source_review["packet_id"]
    ):
        raise PredicateReviewError("predicate review owner or packet binding differs")
    if source_bundle.get("source_report") != {
        "path": str(source_review_path),
        "sha256": decision["source_review_report_sha256"],
    }:
        raise PredicateReviewError("source stage bundle review provenance differs")
    if any(
        source_bundle.get(key) != 0
        for key in ("database_writes", "qdrant_writes", "external_model_calls")
    ):
        raise PredicateReviewError("source stage bundle is not zero-write")

    packet = json.loads(source_bundle["extraction_packet_text"])
    resolution = json.loads(source_bundle["resolution_packet_text"])
    if (
        canonical_sha256(packet) != source_bundle["extraction_packet_sha256"]
        or canonical_sha256(packet) != source_review["validator_packet_sha256"]
        or canonical_sha256(resolution) != source_bundle["resolution_packet_sha256"]
    ):
        raise PredicateReviewError("source packet hash binding differs")
    _validate_extraction_packet(packet)
    _validate_resolution_packet(resolution, packet)
    rules, registry_sha = _predicate_rules(
        (repo_root / args.predicate_registry).resolve()
    )
    derived, transformations = apply_predicate_decisions(
        packet,
        decision["decisions"],
        rules,
        set(source_review["blocking_codes"]),
    )
    derived = NormalizedPacket.model_validate(derived).model_dump(mode="json")
    _validate_extraction_packet(derived)
    _validate_resolution_packet(resolution, derived)
    if canonical_sha256(derived["entity_mentions"]) != canonical_sha256(
        packet["entity_mentions"]
    ):
        raise PredicateReviewError("predicate review changed entity mentions")

    remaining_findings = packet_quality_findings(derived)
    remaining_blockers = sorted(
        {item["code"] for item in remaining_findings if item["blocking"]}
    )
    if remaining_blockers:
        disposition = "manual_review_required"
    else:
        disposition = "stage_preflight_ready"
    extraction_text = stable_json(derived)
    resolution_text = stable_json(resolution)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    evidence_id = source_review["evidence_id"]
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            "|".join(
                (
                    decision["owner_user_id"],
                    evidence_id,
                    extraction_sha,
                    resolution_sha,
                    RESOLVER_VERSION,
                )
            ),
        )
    )
    review_id = str(
        uuid.uuid5(
            REVIEW_NAMESPACE,
            "|".join(
                (
                    decision["owner_user_id"],
                    decision["packet_id"],
                    decision["decision_sha256"],
                    extraction_sha,
                    resolution_sha,
                )
            ),
        )
    )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    review = {
        "contract_version": REVIEW_CONTRACT,
        "mode": "hash_locked_predicate_review_zero_write",
        "generated_at": now,
        "owner_user_id": decision["owner_user_id"],
        "packet_id": decision["packet_id"],
        "review_id": review_id,
        "decision_id": decision["decision_id"],
        "decision_sha256": decision["decision_sha256"],
        "source_review_report_sha256": decision["source_review_report_sha256"],
        "source_stage_bundle_sha256": decision["source_stage_bundle_sha256"],
        "evidence_id": evidence_id,
        "original_packet_sha256": source_bundle["extraction_packet_sha256"],
        "derived_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "predicate_registry_sha256": registry_sha,
        "repository_commit": commit,
        "transformations": transformations,
        "resolution_summary": source_bundle["resolution_summary"],
        "quality_findings": remaining_findings,
        "blocking_codes": remaining_blockers,
        "review_disposition": disposition,
        "zero_write_proof": {
            "database_writes": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
            "source_packet_unchanged": True,
            "derived_artifact_only": True,
        },
    }
    bundle = {
        "contract_version": BUNDLE_CONTRACT,
        "generated_at": now,
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": decision["owner_user_id"],
        "case_id": f"reviewed-local-packet-{decision['packet_id']}",
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": "memory_v1_v5_hash_locked_predicate_review",
        "extractor_version": commit,
        "source_report": {},
        "schemas": {
            "extraction_sha256": _schema_hash(
                (repo_root / args.extraction_schema).resolve()
            ),
            "resolution_sha256": _schema_hash(
                (repo_root / args.resolution_schema).resolve()
            ),
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": source_bundle["resolution_summary"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    report_sha = _secure_write(output_review_path, review)
    try:
        bundle["source_report"] = {
            "path": str(output_review_path),
            "sha256": report_sha,
        }
        bundle_sha = _secure_write(output_bundle_path, bundle)
    except BaseException:
        output_review_path.unlink(missing_ok=True)
        raise

    print(
        stable_json(
            {
                "contract_version": REVIEW_CONTRACT,
                "owner_user_id": review["owner_user_id"],
                "packet_id": review["packet_id"],
                "decision_id": review["decision_id"],
                "review_report": str(output_review_path),
                "review_report_sha256": report_sha,
                "stage_bundle": str(output_bundle_path),
                "stage_bundle_sha256": bundle_sha,
                "transformation_count": len(transformations),
                "blocking_codes": remaining_blockers,
                "review_disposition": disposition,
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
