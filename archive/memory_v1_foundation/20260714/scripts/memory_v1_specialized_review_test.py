#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from rag_engine.memory_v1_specialized_review import (
    SpecializedReviewError,
    build_specialized_review_report,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = "11111111-1111-4111-8111-111111111111"
BATCH = "22222222-2222-4222-8222-222222222222"


def stable_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def unit(evidence_id: str, route: str, role: str):
    return {
        "review_unit_id": evidence_id.replace("30000000", "40000000"),
        "evidence_id": evidence_id,
        "input_lock_sha256": "a" * 64,
        "content_sha256": "b" * 64,
        "primary_route": route,
        "epistemic_role": role,
        "preview": f"preview for {evidence_id}",
    }


def source_report():
    return {
        "mode": "candidate_extraction_review_plan_dry_run_no_writes",
        "plan_version": "memory_v1_candidate_extraction_review_20260713_v1",
        "owner_user_id": OWNER,
        "source_batch": {"batch_id": BATCH},
        "lanes": {
            "governed_claims": {
                "target": "claim",
                "review_units": [
                    unit(
                        "30000000-0000-4000-8000-000000000001",
                        "belief_classification_review",
                        "user_belief_or_opinion",
                    ),
                    unit(
                        "30000000-0000-4000-8000-000000000004",
                        "standard_reviewed_extraction",
                        "user_assertion",
                    ),
                ],
            },
            "response_and_life_preferences": {
                "target": "preference",
                "review_units": [
                    unit(
                        "30000000-0000-4000-8000-000000000002",
                        "canonicalization_review",
                        "response_or_life_preference",
                    )
                ],
            },
            "project_knowledge": {
                "target": "project_knowledge",
                "review_units": [
                    unit(
                        "30000000-0000-4000-8000-000000000003",
                        "canonicalization_review",
                        "project_assertion",
                    )
                ],
            },
        },
    }


def review(source_sha: str):
    controls = {
        "apply_authorized": False,
        "candidate_writes": 0,
        "claim_writes": 0,
        "preference_writes": 0,
        "project_knowledge_writes": 0,
        "qdrant_writes": 0,
        "prompt_injection": False,
        "automatic_promotion": False,
    }
    return {
        "review_version": "memory_v1_specialized_review_20260713_v1",
        "source_report_sha256": source_sha,
        "source_plan_version": "memory_v1_candidate_extraction_review_20260713_v1",
        "owner_user_id": OWNER,
        "batch_id": BATCH,
        "expected_specialized_units": 3,
        "review_authority": "test_review",
        "controls": controls,
        "external_normalization_sources": [],
        "packets": [
            {
                "route": "belief_classification_review",
                "decisions": [
                    {
                        "evidence_id": "30000000-0000-4000-8000-000000000001",
                        "target": "claim",
                        "decision": "rewrite",
                        "recommended_action": "claim_candidate_after_comparison",
                        "reviewed_epistemic_role": "user_belief_or_opinion",
                        "surface_policy": "direct_relevance_only",
                        "canonical_drafts": ["User holds a test belief."],
                        "reason_codes": ["test_belief"],
                    }
                ],
            },
            {
                "route": "canonicalization_review",
                "decisions": [
                    {
                        "evidence_id": "30000000-0000-4000-8000-000000000002",
                        "target": "preference",
                        "decision": "rewrite",
                        "recommended_action": "preference_candidate_after_schema",
                        "reviewed_epistemic_role": "response_or_life_preference",
                        "surface_policy": "silent_response_policy",
                        "canonical_drafts": ["User prefers a test response style."],
                        "reason_codes": ["test_preference"],
                    },
                    {
                        "evidence_id": "30000000-0000-4000-8000-000000000003",
                        "target": "project_knowledge",
                        "decision": "defer",
                        "recommended_action": "reextract_expanded_context",
                        "reviewed_epistemic_role": "project_assertion",
                        "surface_policy": "project_admin_only",
                        "canonical_drafts": [],
                        "reason_codes": ["test_context_missing"],
                    },
                ],
            },
        ],
    }


def expect_error(fn, label: str) -> None:
    try:
        fn()
    except SpecializedReviewError:
        return
    raise AssertionError(f"{label} did not fail closed")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_path = root / "source.json"
        review_path = root / "review.json"
        source_bytes = stable_bytes(source_report())
        source_path.write_bytes(source_bytes)
        review_data = review(hashlib.sha256(source_bytes).hexdigest())
        review_path.write_bytes(stable_bytes(review_data))

        result = build_specialized_review_report(
            source_report_path=source_path,
            review_path=review_path,
        )
        if result["summary"]["reviewed_unit_count"] != 3:
            raise AssertionError("specialized coverage is incorrect")
        if result["summary"]["candidate_records_created"] != 0:
            raise AssertionError("review created a candidate record")
        if result["summary"]["decisions"] != {"defer": 1, "rewrite": 2}:
            raise AssertionError("decision counts are incorrect")
        if not result["controls"]["every_specialized_unit_reviewed_once"]:
            raise AssertionError("coverage control is false")

        source_path.write_bytes(source_bytes + b" ")
        expect_error(
            lambda: build_specialized_review_report(
                source_report_path=source_path,
                review_path=review_path,
            ),
            "source report hash drift",
        )
        source_path.write_bytes(source_bytes)

        missing = json.loads(json.dumps(review_data))
        missing["packets"][1]["decisions"].pop()
        review_path.write_bytes(stable_bytes(missing))
        expect_error(
            lambda: build_specialized_review_report(
                source_report_path=source_path,
                review_path=review_path,
            ),
            "incomplete review coverage",
        )

        mismatched = json.loads(json.dumps(review_data))
        mismatched["packets"][0]["decisions"][0]["target"] = "preference"
        review_path.write_bytes(stable_bytes(mismatched))
        expect_error(
            lambda: build_specialized_review_report(
                source_report_path=source_path,
                review_path=review_path,
            ),
            "target-lane mismatch",
        )

    for relative in (
        "rag_engine/memory_v1_specialized_review.py",
        "scripts/memory_v1_specialized_review.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        if "--apply" in source:
            raise AssertionError(f"{relative} exposes an apply option")
        sql_mutation = re.compile(
            r"^[ \t]*(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CALL)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        if sql_mutation.search(source):
            raise AssertionError(f"{relative} contains mutation SQL")

    print("memory_v1_specialized_review: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
