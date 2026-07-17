#!/usr/bin/env python3
from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from rag_engine.memory_v1_candidate_review import (
    CandidateReviewError,
    CandidateReviewManifest,
    EvidenceReviewRow,
    build_review_units,
    candidate_review_dry_run_report,
    verify_review_batch,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
BATCH = uuid.UUID("22222222-2222-4222-8222-222222222222")


def sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def row(
    evidence_id: str,
    content: str,
    target: str,
    disposition: str,
    epistemic_role: str,
    *,
    flags: tuple[str, ...] = (),
    sensitivity: str = "high",
) -> EvidenceReviewRow:
    content_hash = sha(content)
    return EvidenceReviewRow(
        owner_user_id=OWNER,
        batch_id=BATCH,
        evidence_id=uuid.UUID(evidence_id),
        external_id=f"chat_log:test:span:{evidence_id}",
        ledger_content_sha256=content_hash,
        operation="inserted",
        kind="user_statement",
        content=content,
        content_sha256=content_hash,
        observed_at=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        sensitivity=sensitivity,
        status="active",
        metadata={
            "candidate_creation_authorized": False,
            "candidate_target": target,
            "disposition": disposition,
            "epistemic_role": epistemic_role,
            "prompt_eligible": False,
            "retrieval_eligible": False,
            "review_flags": list(flags),
            "span_origin": "atomic",
        },
    )


def manifest(rows: list[EvidenceReviewRow]) -> CandidateReviewManifest:
    def count(values):
        return dict(sorted(Counter(values).items()))

    return CandidateReviewManifest(
        path=Path("test-manifest.json"),
        sha256="a" * 64,
        manifest_version="memory_v1_candidate_review_20260713_v1",
        plan_version="memory_v1_candidate_extraction_review_20260713_v1",
        owner_user_id=OWNER,
        batch_id=BATCH,
        batch_key="test-batch",
        evidence_plan_version="memory_v1_evidence_persistence_20260713_v1",
        input_fingerprint_sha256="b" * 64,
        reviewed_report_sha256="c" * 64,
        authorization_manifest_sha256="d" * 64,
        expected_evidence_rows=len(rows),
        expected_operations=count(item.operation for item in rows),
        expected_candidate_targets=count(
            str(item.metadata["candidate_target"]) for item in rows
        ),
        expected_dispositions=count(str(item.metadata["disposition"]) for item in rows),
        expected_epistemic_roles=count(
            str(item.metadata["epistemic_role"]) for item in rows
        ),
        expected_sensitivities=count(item.sensitivity for item in rows),
        expected_span_origins=count(str(item.metadata["span_origin"]) for item in rows),
        expected_review_flags=count(
            flag for item in rows for flag in item.metadata["review_flags"]
        ),
        expected_existing_claim_candidates=0,
        expected_existing_preferences=0,
        controls={
            "apply_authorized": False,
            "database_transaction": "read_only",
            "candidate_writes": 0,
            "claim_writes": 0,
            "preference_writes": 0,
            "project_knowledge_writes": 0,
            "qdrant_writes": 0,
            "prompt_injection": False,
            "network_model_calls": False,
            "full_evidence_text_in_report": False,
            "human_review_required": True,
        },
    )


def batch(m: CandidateReviewManifest):
    return {
        "batch_id": m.batch_id,
        "owner_user_id": m.owner_user_id,
        "batch_key": m.batch_key,
        "plan_version": m.evidence_plan_version,
        "input_fingerprint_sha256": m.input_fingerprint_sha256,
        "reviewed_report_sha256": m.reviewed_report_sha256,
        "authorization_manifest_sha256": m.authorization_manifest_sha256,
        "expected_evidence_count": m.expected_evidence_rows,
        "inserted_count": m.expected_operations.get("inserted", 0),
        "reused_count": m.expected_operations.get("reused", 0),
        "status": "committed",
    }


def controls():
    return {
        "effective_role_brains_app": True,
        "transaction_read_only": True,
        "batch_forced_rls": True,
        "evidence_forced_rls": True,
        "existing_claim_candidates_for_batch": 0,
        "existing_preferences_for_batch": 0,
        "project_knowledge_table_exists": False,
    }


def expect_error(fn, label: str) -> None:
    try:
        fn()
    except CandidateReviewError:
        return
    raise AssertionError(f"{label} did not fail closed")


def main() -> int:
    rows = [
        row(
            "30000000-0000-4000-8000-000000000001",
            "My correct pet name is Neko.",
            "claim",
            "review_claim_span",
            "user_assertion",
        ),
        row(
            "30000000-0000-4000-8000-000000000002",
            "I believe this health claim is uncertain.",
            "claim",
            "review_belief_span",
            "user_belief_or_opinion",
            flags=(
                "belief_not_external_fact",
                "high_stakes_health_belief_review_required",
                "sensitivity_review_required",
            ),
            sensitivity="restricted",
        ),
        row(
            "30000000-0000-4000-8000-000000000003",
            "I prefer concise responses.",
            "preference",
            "review_preference_span",
            "response_or_life_preference",
        ),
        row(
            "30000000-0000-4000-8000-000000000004",
            "Memory ownership uses the Supabase user ID.",
            "project_knowledge",
            "review_project_span",
            "project_assertion",
        ),
    ]
    m = manifest(rows)
    verify_review_batch(m, batch(m), rows, controls())
    units = build_review_units(m, rows)
    replay = build_review_units(m, rows)
    if units != replay:
        raise AssertionError("review plan is not deterministic")
    if len({unit.target for unit in units}) != 3:
        raise AssertionError("three targets were not preserved")
    belief = next(unit for unit in units if unit.epistemic_role == "user_belief_or_opinion")
    if belief.primary_route != "restricted_domain_review":
        raise AssertionError("restricted belief was not routed to domain review")
    if "preserve_as_user_viewpoint_not_external_fact" not in belief.review_requirements:
        raise AssertionError("belief safety requirement was lost")

    report = candidate_review_dry_run_report(m, batch(m), units, controls())
    if report["mode"] != "candidate_extraction_review_plan_dry_run_no_writes":
        raise AssertionError("report is not dry-run-only")
    if set(report["lanes"]) != {
        "governed_claims",
        "response_and_life_preferences",
        "project_knowledge",
    }:
        raise AssertionError("target lanes are not separated")
    for lane_name, lane in report["lanes"].items():
        if any("content" in unit for unit in lane["review_units"]):
            raise AssertionError(f"{lane_name} exposes exact evidence content")
    if report["summary"]["extracted_candidate_count"] != 0:
        raise AssertionError("dry-run report extracted a candidate")
    if report["controls"]["apply_option_exposed"]:
        raise AssertionError("dry-run report exposes apply")

    cross_owner = replace(rows[0], owner_user_id=uuid.uuid4())
    expect_error(
        lambda: verify_review_batch(manifest([cross_owner]), batch(manifest([cross_owner])), [cross_owner], controls()),
        "cross-owner row",
    )
    bad_hash = replace(rows[0], content="changed")
    expect_error(
        lambda: verify_review_batch(manifest([bad_hash]), batch(manifest([bad_hash])), [bad_hash], controls()),
        "content hash drift",
    )
    bad_target = replace(
        rows[0],
        metadata={**rows[0].metadata, "candidate_target": "preference"},
    )
    expect_error(
        lambda: verify_review_batch(manifest([bad_target]), batch(manifest([bad_target])), [bad_target], controls()),
        "target/disposition mismatch",
    )
    authorized = replace(
        rows[0],
        metadata={**rows[0].metadata, "candidate_creation_authorized": True},
    )
    expect_error(
        lambda: verify_review_batch(manifest([authorized]), batch(manifest([authorized])), [authorized], controls()),
        "unauthorized candidate enablement",
    )

    for relative in (
        "rag_engine/memory_v1_candidate_review.py",
        "scripts/memory_v1_candidate_review_dry_run.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        if "--apply" in source:
            raise AssertionError(f"{relative} exposes an apply option")
        sql_mutation = re.compile(
            r"^[ \t]*(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CALL)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        if sql_mutation.search(source):
            raise AssertionError(f"{relative} contains database mutation SQL")

    print("memory_v1_candidate_review: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
