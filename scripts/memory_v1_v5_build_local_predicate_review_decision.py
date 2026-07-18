#!/usr/bin/env python3
"""Build one sanitized, hash-locked local predicate review decision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_apply_local_predicate_review import (
    ALLOWED_RECLASSIFICATIONS,
    CONFIRMATION,
    DECISION_CONTRACT,
    PredicateReviewError,
    decision_body,
    expected_decision_id,
)
from scripts.memory_v1_v5_review_local_packet import (
    BUNDLE_CONTRACT,
    _output_path,
    _secure_root,
    _secure_write,
)
from scripts.memory_v1_v5_stage_preflight import sha256_file


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an immutable decision for one exact observation without "
            "emitting source prose or literal values."
        )
    )
    parser.add_argument("--source-review-report", required=True)
    parser.add_argument("--source-stage-bundle", required=True)
    parser.add_argument("--observation-ref", required=True)
    parser.add_argument("--from-predicate", required=True)
    parser.add_argument("--to-predicate", required=True)
    parser.add_argument("--reviewer-ref", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-root", default="/home/ubuntu/memory-v1-reviews")
    parser.add_argument("--confirm", required=True)
    return parser.parse_args()


def _load_secure_json(path_value: str, root: Path, label: str) -> tuple[Path, dict]:
    from scripts.memory_v1_v5_apply_local_predicate_review import (
        _json_file,
        _secure_input,
    )

    path = _secure_input(path_value, root)
    return path, _json_file(path, label)


def build_decision(
    *,
    source_review: dict,
    source_bundle: dict,
    source_review_sha256: str,
    source_bundle_sha256: str,
    observation_ref: str,
    from_predicate: str,
    to_predicate: str,
    reviewer_ref: str,
) -> dict:
    pair = (from_predicate, to_predicate)
    policy = ALLOWED_RECLASSIFICATIONS.get(pair)
    if policy is None:
        raise PredicateReviewError("predicate reclassification is not allowlisted")
    if (
        source_review.get("contract_version")
        != "memory_v1_v5_local_packet_review_v1"
        or source_review.get("review_disposition") != "manual_review_required"
        or source_bundle.get("contract_version") != BUNDLE_CONTRACT
        or source_bundle.get("mode") != "preflight_only_zero_write"
        or source_bundle.get("authorized_stage") is not False
    ):
        raise PredicateReviewError("source review artifacts are not eligible")
    if policy["required_blocker"] not in source_review.get("blocking_codes", []):
        raise PredicateReviewError("required semantic blocker is absent")
    packet = json.loads(source_bundle["extraction_packet_text"])
    if canonical_sha256(packet) != source_bundle["extraction_packet_sha256"]:
        raise PredicateReviewError("source packet hash binding differs")
    matches = [
        item
        for item in packet.get("observations", [])
        if item.get("observation_ref") == observation_ref
    ]
    if len(matches) != 1 or matches[0].get("predicate") != from_predicate:
        raise PredicateReviewError("source observation is absent or differs")
    observation = matches[0]
    object_value = observation.get("object")
    if not isinstance(object_value, dict) or object_value.get("kind") != "literal":
        raise PredicateReviewError("source observation is not a literal")
    decision = {
        "confirmation": CONFIRMATION,
        "contract_version": DECISION_CONTRACT,
        "decision_id": "",
        "decision_sha256": "",
        "decisions": [
            {
                "action": "reclassify_predicate",
                "expected_object_value_sha256": canonical_sha256(
                    object_value.get("value")
                ),
                "expected_observation_sha256": canonical_sha256(observation),
                "from_predicate": from_predicate,
                "observation_ref": observation_ref,
                "replacement_reason_code": policy["replacement_reason_code"],
                "to_predicate": to_predicate,
            }
        ],
        "owner_user_id": source_review["owner_user_id"],
        "packet_id": source_review["packet_id"],
        "reviewer_ref": reviewer_ref.strip(),
        "reviewer_type": "owner_authorized_operator",
        "source_review_report_sha256": source_review_sha256,
        "source_stage_bundle_sha256": source_bundle_sha256,
    }
    if not decision["reviewer_ref"]:
        raise PredicateReviewError("reviewer reference is empty")
    decision["decision_id"] = expected_decision_id(decision)
    decision["decision_sha256"] = canonical_sha256(decision_body(decision))
    return decision


def main() -> int:
    args = arguments()
    if args.confirm != CONFIRMATION:
        raise PredicateReviewError("exact predicate review confirmation is required")
    root = _secure_root(args.review_root)
    review_path, source_review = _load_secure_json(
        args.source_review_report, root, "source review"
    )
    bundle_path, source_bundle = _load_secure_json(
        args.source_stage_bundle, root, "source stage bundle"
    )
    output_path = _output_path(args.output, root)
    decision = build_decision(
        source_review=source_review,
        source_bundle=source_bundle,
        source_review_sha256=sha256_file(review_path),
        source_bundle_sha256=sha256_file(bundle_path),
        observation_ref=args.observation_ref,
        from_predicate=args.from_predicate,
        to_predicate=args.to_predicate,
        reviewer_ref=args.reviewer_ref,
    )
    output_sha = _secure_write(output_path, decision)
    print(
        json.dumps(
            {
                "contract_version": DECISION_CONTRACT,
                "decision_id": decision["decision_id"],
                "decision_sha256": decision["decision_sha256"],
                "decision_path": str(output_path),
                "decision_file_sha256": output_sha,
                "transformation_count": len(decision["decisions"]),
                "source_prose_emitted": False,
                "literal_values_emitted": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
