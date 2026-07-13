#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from rag_engine.memory_v1_artifacts import ArtifactManifestEntry
from rag_engine.memory_v1_evidence_triage import (
    ExistingEvidence,
    EvidenceSourceConflict,
    TriageSourceRow,
    classify_source,
    sha256_text,
    triage_dry_run_report,
    triage_sources,
)


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def source(source_id: str, text: str, *, minute: int = 0) -> TriageSourceRow:
    return TriageSourceRow(
        source_id=uuid.UUID(source_id),
        owner_user_id=OWNER,
        source="frontend/chat:user",
        text=text,
        created_at=datetime(2026, 7, 13, 12, minute, tzinfo=timezone.utc),
        thread_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        vantage_id="RESSE",
        request_id=f"request-{minute}",
    )


def main() -> int:
    correction = source(
        "10000000-0000-4000-8000-000000000001",
        "The correct spelling is Neko, not Nemo.",
    )
    correction_decision = classify_source(
        correction, artifact_entries={}, existing_evidence={}
    )
    if correction_decision.primary_lane != "explicit_correction":
        raise AssertionError("explicit correction was not prioritized")
    if correction_decision.candidate_action != "propose_atomic_candidate":
        raise AssertionError("explicit correction did not become candidate-eligible")
    if not correction_decision.cue_spans:
        raise AssertionError("explicit correction lacks an exact cue span")

    recall = source(
        "10000000-0000-4000-8000-000000000002",
        "Was it Nemo or Neko?",
        minute=1,
    )
    recall_decision = classify_source(recall, artifact_entries={}, existing_evidence={})
    if recall_decision.candidate_action != "retain_raw_only":
        raise AssertionError("recall question was treated as a new assertion")

    mixed_correction_question = source(
        "10000000-0000-4000-8000-000000000012",
        "That's correct. What was the correct spelling of my pet's name?",
        minute=11,
    )
    mixed_correction_decision = classify_source(
        mixed_correction_question, artifact_entries={}, existing_evidence={}
    )
    if mixed_correction_decision.primary_lane == "explicit_correction":
        raise AssertionError("a correction phrase inside a question became an assertion")
    if mixed_correction_decision.candidate_action != "retain_raw_only":
        raise AssertionError("a correction recall question became candidate-eligible")

    family_recall = source(
        "10000000-0000-4000-8000-000000000013",
        "Any details about my mom that you recall?",
        minute=12,
    )
    family_recall_decision = classify_source(
        family_recall, artifact_entries={}, existing_evidence={}
    )
    if family_recall_decision.primary_lane != "recall_query":
        raise AssertionError("a family recall question was treated as personal history")

    personal = source(
        "10000000-0000-4000-8000-000000000003",
        "My mother DeeDee died last year.",
        minute=2,
    )
    personal_decision = classify_source(personal, artifact_entries={}, existing_evidence={})
    if personal_decision.primary_lane != "personal_history":
        raise AssertionError("stable personal history was not recognized")
    if personal_decision.candidate_target != "claim":
        raise AssertionError("stable personal history did not target a claim")

    temporary = source(
        "10000000-0000-4000-8000-000000000004",
        "I'm tired tonight and heading off to bed.",
        minute=3,
    )
    temporary_decision = classify_source(
        temporary, artifact_entries={}, existing_evidence={}
    )
    if temporary_decision.primary_lane != "temporary_context":
        raise AssertionError("temporary context was not recognized")
    if temporary_decision.candidate_action != "retain_evidence_only":
        raise AssertionError("temporary context became candidate-eligible")

    temporary_caregiving = source(
        "10000000-0000-4000-8000-000000000014",
        "Caring for my wife has been difficult lately.",
        minute=13,
    )
    caregiving_decision = classify_source(
        temporary_caregiving, artifact_entries={}, existing_evidence={}
    )
    if caregiving_decision.primary_lane != "temporary_context":
        raise AssertionError("current caregiving difficulty was treated as durable history")
    if caregiving_decision.candidate_action != "retain_evidence_only":
        raise AssertionError("current caregiving difficulty became candidate-eligible")

    yesterday_problem = source(
        "10000000-0000-4000-8000-000000000015",
        "Yesterday I went to work on the website, but the Mac app failed.",
        minute=14,
    )
    yesterday_decision = classify_source(
        yesterday_problem, artifact_entries={}, existing_evidence={}
    )
    if yesterday_decision.candidate_target is not None:
        raise AssertionError("a yesterday-only event targeted durable memory")

    structured = source(
        "10000000-0000-4000-8000-000000000005",
        "What were my macros and workouts last week?",
        minute=4,
    )
    structured_decision = classify_source(
        structured, artifact_entries={}, existing_evidence={}
    )
    if structured_decision.primary_lane != "structured_data_reference":
        raise AssertionError("structured domain request was not routed")
    if structured_decision.candidate_action != "route_structured_adapter":
        raise AssertionError("structured domain request targeted conversational memory")

    passive_structured_mention = source(
        "10000000-0000-4000-8000-000000000016",
        "The LifeSwitch app has a nutrition section that can track macros.",
        minute=15,
    )
    passive_structured_decision = classify_source(
        passive_structured_mention, artifact_entries={}, existing_evidence={}
    )
    if passive_structured_decision.primary_lane == "structured_data_reference":
        raise AssertionError("a project description was routed as user structured data")

    project_request = source(
        "10000000-0000-4000-8000-000000000017",
        "Could you write a summary of the Verbal Sage memory system",
        minute=16,
    )
    project_request_decision = classify_source(
        project_request, artifact_entries={}, existing_evidence={}
    )
    if project_request_decision.candidate_action != "retain_raw_only":
        raise AssertionError("a project request without punctuation became project knowledge")
    if not project_request_decision.request_like:
        raise AssertionError("a project request was not labeled as a request")

    programming_policy = source(
        "10000000-0000-4000-8000-000000000018",
        "Please don't make suggestions on programming unless I ask for them.",
        minute=17,
    )
    programming_policy_decision = classify_source(
        programming_policy, artifact_entries={}, existing_evidence={}
    )
    if programming_policy_decision.primary_lane != "response_preference":
        raise AssertionError("an explicit response policy was treated as a generic request")

    multi_sentence_history = source(
        "10000000-0000-4000-8000-000000000019",
        "I was born in Green Bay. We moved often. I had a happy childhood.",
        minute=18,
    )
    multi_sentence_decision = classify_source(
        multi_sentence_history, artifact_entries={}, existing_evidence={}
    )
    if multi_sentence_decision.candidate_action != "split_before_candidate":
        raise AssertionError("a multi-sentence history turn bypassed atomic splitting")

    response_policy = source(
        "10000000-0000-4000-8000-000000000006",
        "I want the AI to respond directly and avoid generic therapeutic empathy.",
        minute=5,
    )
    policy_decision = classify_source(
        response_policy, artifact_entries={}, existing_evidence={}
    )
    if policy_decision.primary_lane != "response_preference":
        raise AssertionError("response preference was not separated")
    if policy_decision.candidate_target != "preference":
        raise AssertionError("response preference targeted the wrong store")

    project = source(
        "10000000-0000-4000-8000-000000000007",
        "I'm building the Verbal Sage memory system as an account-owned evidence graph.",
        minute=6,
    )
    project_decision = classify_source(project, artifact_entries={}, existing_evidence={})
    if project_decision.primary_lane != "technical_project":
        raise AssertionError("project knowledge was not separated from personal history")
    if project_decision.candidate_target != "project_knowledge":
        raise AssertionError("project knowledge targeted the wrong store")

    artifact_text = "# Memory checkpoint\n\nThis is historical."
    artifact_source = source(
        "10000000-0000-4000-8000-000000000008",
        artifact_text,
        minute=7,
    )
    artifact_entry = ArtifactManifestEntry.from_mapping(
        {
            "source_id": str(artifact_source.source_id),
            "expected_sha256": hashlib.sha256(artifact_text.encode()).hexdigest(),
            "title": "Memory checkpoint",
            "artifact_kind": "technical_checkpoint",
            "authorship": "assistant",
            "body_marker": None,
            "body_authorship": None,
            "endorsement_level": "reference",
            "endorsement_explicit": False,
            "endorsement_rationale": "Historical reference.",
            "document_state": "historical",
            "extraction_policy": "review_only",
            "sensitivity": "medium",
        },
        owner_user_id=OWNER,
        manifest_version="triage_test_v1",
        source_system="public.chat_log",
    )
    evidence_id = uuid.UUID("20000000-0000-4000-8000-000000000008")
    artifact_decision = classify_source(
        artifact_source,
        artifact_entries={artifact_source.source_id: artifact_entry},
        existing_evidence={
            artifact_source.source_id: ExistingEvidence(
                evidence_id=evidence_id,
                content_sha256=sha256_text(artifact_text),
                status="active",
            )
        },
    )
    if artifact_decision.primary_lane != "artifact_archive":
        raise AssertionError("known artifact was not routed to the archive")
    if artifact_decision.evidence_action != "reuse_existing":
        raise AssertionError("known artifact did not reuse existing evidence")
    if artifact_decision.candidate_action != "artifact_archive_only":
        raise AssertionError("known artifact became a claim candidate")

    duplicate_text = "Could you summarize our conversation?"
    first_duplicate = source(
        "10000000-0000-4000-8000-000000000009",
        duplicate_text,
        minute=8,
    )
    second_duplicate = source(
        "10000000-0000-4000-8000-000000000010",
        duplicate_text,
        minute=9,
    )
    decisions = triage_sources(
        [first_duplicate, second_duplicate],
        artifact_entries={},
        existing_evidence={},
    )
    if decisions[1].duplicate_of_source_id != first_duplicate.source_id:
        raise AssertionError("duplicate source was not linked to the first occurrence")
    if decisions[0].independence_key != decisions[1].independence_key:
        raise AssertionError("duplicates received different independence keys")

    long_mixed = source(
        "10000000-0000-4000-8000-000000000011",
        (
            "I'm building the Verbal Sage memory system.\n\n"
            "I want the AI to respond directly.\n\n"
            + "This paragraph adds implementation detail. " * 30
        ),
        minute=10,
    )
    long_decision = classify_source(
        long_mixed, artifact_entries={}, existing_evidence={}
    )
    if not long_decision.requires_atomic_split:
        raise AssertionError("long mixed turn bypassed atomic splitting")
    if long_decision.candidate_action != "split_before_candidate":
        raise AssertionError("long mixed turn targeted direct candidate creation")

    report = triage_dry_run_report(
        decisions,
        owner_user_id=OWNER,
        input_cursor=None,
    )
    if report["mode"] != "dry_run_no_writes":
        raise AssertionError("triage report is not dry-run")
    if any(report["controls"][key] for key in ("database_writes", "candidate_writes", "claim_writes", "qdrant_writes")):
        raise AssertionError("triage report enabled a write path")

    bad_existing = {
        correction.source_id: ExistingEvidence(
            evidence_id=uuid.uuid4(),
            content_sha256="0" * 64,
            status="active",
        )
    }
    try:
        classify_source(
            correction,
            artifact_entries={},
            existing_evidence=bad_existing,
        )
    except EvidenceSourceConflict:
        pass
    else:
        raise AssertionError("existing evidence hash mismatch did not fail closed")

    print("memory_v1_evidence_triage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
