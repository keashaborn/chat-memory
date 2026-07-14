#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from rag_engine.memory_v1_preference_project_extraction import (
    EvidenceState,
    PreferenceProjectExtractionError,
    build_extraction_plan,
    load_extraction_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "ops"
    / "manifests"
    / "memory_v1_preference_project_extraction_20260713.json"
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def database_controls():
    return {
        "effective_role_brains_app": True,
        "transaction_read_only": True,
        "specialized_tables_forced_rls": True,
        "evidence_forced_rls": True,
        "preference_candidate_insert_allowed": True,
        "project_candidate_insert_allowed": True,
        "direct_preference_review_insert_denied": True,
        "direct_project_review_insert_denied": True,
        "direct_user_preference_insert_denied": True,
        "direct_project_head_insert_denied": True,
        "project_registration_execute_allowed": True,
        "existing_preference_candidates": 0,
        "existing_project_candidates": 0,
        "existing_project_spaces": 0,
        "existing_preference_reviews": 0,
        "existing_project_reviews": 0,
        "existing_durable_preferences": 0,
        "existing_durable_project_heads": 0,
    }


def fixtures():
    manifest = load_extraction_manifest(MANIFEST)
    decisions = []
    states = []
    observed = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    for index, mapping in enumerate(manifest.mappings):
        content_sha = sha(f"evidence-{index}")
        target = mapping["target"]
        decisions.append(
            {
                "evidence_id": mapping["evidence_id"],
                "review_unit_id": f"review-unit-{index}",
                "input_lock_sha256": sha(f"input-{index}"),
                "content_sha256": content_sha,
                "target": target,
                "decision": "rewrite",
                "recommended_action": (
                    "preference_candidate_after_schema"
                    if target == "preference"
                    else "project_candidate_after_schema"
                ),
                "canonical_drafts": [f"Canonical reviewed draft {index}."],
                "reason_codes": ["reviewed_mapping_test"],
            }
        )
        states.append(
            EvidenceState(
                evidence_id=uuid.UUID(mapping["evidence_id"]),
                ledger_content_sha256=content_sha,
                content_sha256=content_sha,
                observed_at=observed,
                sensitivity="high",
                status="active",
            )
        )
    for index in range(2):
        decisions.append(
            {
                "evidence_id": f"deferred-project-{index}",
                "target": "project_knowledge",
                "decision": "defer",
                "recommended_action": "reextract_expanded_context",
                "canonical_drafts": [],
            }
        )
    report = {"packets": [{"route": "test", "decisions": decisions}]}
    return manifest, report, states


def expect_error(fn, label: str) -> None:
    try:
        fn()
    except PreferenceProjectExtractionError:
        return
    raise AssertionError(f"{label} did not fail closed")


def main() -> int:
    manifest, report, states = fixtures()
    controls = database_controls()
    first = build_extraction_plan(manifest, report, states, controls)
    replay = build_extraction_plan(manifest, report, states, controls)
    if first != replay:
        raise AssertionError("dry-run extraction plan is not deterministic")
    if first["mode"] != "preference_project_extraction_dry_run_no_writes":
        raise AssertionError("wrong dry-run mode")
    if first["summary"]["preference_candidates_proposed"] != 3:
        raise AssertionError("preference proposal count changed")
    if first["summary"]["project_candidates_proposed"] != 5:
        raise AssertionError("project proposal count changed")
    if first["summary"]["candidate_records_created"] != 0:
        raise AssertionError("dry run claims to create candidates")
    if first["controls"]["apply_option_exposed"]:
        raise AssertionError("dry run exposes apply")
    if first["controls"]["persistence_authorized"]:
        raise AssertionError("dry run authorizes persistence")
    candidate_ids = {
        row["candidate_id"]
        for lane in (first["preference_lane"], first["project_lane"])
        for row in lane["candidates"]
    }
    if len(candidate_ids) != 8:
        raise AssertionError("candidate IDs are not unique")
    if any(
        "preview" in row or "raw_content" in row
        for lane in (first["preference_lane"], first["project_lane"])
        for row in lane["candidates"]
    ):
        raise AssertionError("raw evidence leaked into the report")
    project_statuses = [
        row for row in first["project_lane"]["candidates"] if row["knowledge_kind"] == "status"
    ]
    if not project_statuses or any(row["effective_at"] is None for row in project_statuses):
        raise AssertionError("historical project status lost effective time")
    bad_states = list(states)
    bad_states[0] = replace(bad_states[0], content_sha256="0" * 64)
    expect_error(
        lambda: build_extraction_plan(manifest, report, bad_states, controls),
        "source evidence hash mutation",
    )
    bad_controls = dict(controls)
    bad_controls["existing_preference_candidates"] = 1
    expect_error(
        lambda: build_extraction_plan(manifest, report, states, bad_controls),
        "existing target mutation",
    )
    bad_report = {"packets": [{"decisions": [dict(item) for item in report["packets"][0]["decisions"]]}]}
    bad_report["packets"][0]["decisions"][0]["recommended_action"] = "retain_evidence_only"
    expect_error(
        lambda: build_extraction_plan(manifest, bad_report, states, controls),
        "review action mutation",
    )
    print("memory_v1_preference_project_extraction_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
