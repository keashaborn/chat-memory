#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from rag_engine.memory_v1_atomic_spans import (
    atomic_span_dry_run_report,
    build_atomic_span_plan,
)
from rag_engine.memory_v1_evidence_triage import (
    TriageSourceRow,
    classify_source,
    sha256_text,
)


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def source(source_id: str, text: str) -> TriageSourceRow:
    return TriageSourceRow(
        source_id=uuid.UUID(source_id),
        owner_user_id=OWNER,
        source="frontend/chat:user",
        text=text,
        created_at=datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc),
        thread_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        vantage_id="RESSE",
        request_id="atomic-span-test",
    )


def split_decision(source_row: TriageSourceRow):
    decision = classify_source(
        source_row,
        artifact_entries={},
        existing_evidence={},
    )
    if decision.candidate_action != "split_before_candidate":
        raise AssertionError(
            f"fixture did not become split-required: {decision.candidate_action}"
        )
    return decision


def forced_split_plan(
    source_id: str,
    text: str,
    *,
    parent_lane: str = "general_conversation",
):
    source_row = source(source_id, text)
    decision = classify_source(source_row, artifact_entries={}, existing_evidence={})
    decision = replace(
        decision,
        primary_lane=parent_lane,
        lanes=(parent_lane,),
        requires_atomic_split=True,
        candidate_action="split_before_candidate",
        candidate_target=None,
    )
    return build_atomic_span_plan(decision)


def main() -> int:
    mixed_text = (
        "I was born in Green Bay. "
        "What did I tell you about my mom? "
        "Yesterday I was tired. "
        "I prefer direct answers."
    )
    mixed_source = source("30000000-0000-4000-8000-000000000001", mixed_text)
    mixed_plan = build_atomic_span_plan(split_decision(mixed_source))
    if len(mixed_plan.spans) != 4:
        raise AssertionError(f"expected four exact spans, got {len(mixed_plan.spans)}")
    dispositions = [span.disposition for span in mixed_plan.spans]
    if dispositions != [
        "review_claim_span",
        "exclude_question",
        "retain_temporary_evidence",
        "review_preference_span",
    ]:
        raise AssertionError(f"unexpected mixed dispositions: {dispositions}")
    for span in mixed_plan.spans:
        if mixed_text[span.char_start : span.char_end] != span.content:
            raise AssertionError("source offsets do not reproduce span content")
        if sha256_text(span.content) != span.content_sha256:
            raise AssertionError("span hash does not match exact source content")

    replay = build_atomic_span_plan(split_decision(mixed_source))
    if [span.span_id for span in replay.spans] != [
        span.span_id for span in mixed_plan.spans
    ]:
        raise AssertionError("span IDs are not deterministic")

    correction_text = (
        "I must have misspelled the name. "
        "The correct spelling is Neko, not Nemo. "
        "What was the correct spelling?"
    )
    correction_plan = build_atomic_span_plan(
        split_decision(
            source("30000000-0000-4000-8000-000000000002", correction_text)
        )
    )
    if correction_plan.spans[1].span_kind != "correction_assertion":
        raise AssertionError("declarative correction was not isolated")
    if correction_plan.spans[1].disposition != "review_claim_span":
        raise AssertionError("declarative correction did not become review-only")
    if correction_plan.spans[2].disposition != "exclude_question":
        raise AssertionError("correction question became candidate-bearing")

    structured_text = (
        "I have been tracking my macros this week. "
        "My goal is to understand the trend. "
        "I prefer concise summaries."
    )
    structured_plan = build_atomic_span_plan(
        split_decision(
            source("30000000-0000-4000-8000-000000000003", structured_text)
        )
    )
    if structured_plan.spans[0].disposition != "route_structured_adapter":
        raise AssertionError("structured state did not route to its adapter")

    pasted_text = (
        "# Verbal Sage memory system export\n"
        "assistant_profile.view assistant_profile.apply_builtin allowed\n"
        "memory_owner.filter memory_claim_v1.read allowed\n"
        + "project_permission.rule allowed\n" * 45
    )
    pasted_decision = split_decision(
        source("30000000-0000-4000-8000-000000000004", pasted_text)
    )
    pasted_decision = replace(pasted_decision, content_authorship="mixed_or_quoted")
    pasted_plan = build_atomic_span_plan(pasted_decision)
    if not pasted_plan.source_dump_detected:
        raise AssertionError("structured source dump was not detected at source level")
    if any(
        span.disposition != "exclude_quoted_or_pasted"
        for span in pasted_plan.spans
    ):
        raise AssertionError("pasted data emitted a review candidate")

    compound_text = "I was born in Green Bay and " + ("this clause continues " * 35)
    compound_source = source(
        "30000000-0000-4000-8000-000000000005", compound_text
    )
    compound_decision = classify_source(
        compound_source, artifact_entries={}, existing_evidence={}
    )
    if compound_decision.candidate_action != "split_before_candidate":
        raise AssertionError("long compound fixture did not require splitting")
    compound_plan = build_atomic_span_plan(compound_decision)
    if compound_plan.spans[0].disposition != "manual_split_required":
        raise AssertionError("overlong compound span was treated as atomic")
    if not compound_plan.spans[0].requires_manual_split:
        raise AssertionError("overlong compound span lacks manual split flag")

    project_preference_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000006",
        "Verbal Sage is the app I am building. In my free time I like audiobooks.",
        parent_lane="technical_project",
    )
    if project_preference_plan.spans[1].disposition != "review_preference_span":
        raise AssertionError("project parent coerced an isolated preference into project memory")

    contextual_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000007",
        "That was kind of fun. I was always the type of guy that was never into.",
        parent_lane="personal_history",
    )
    if [span.disposition for span in contextual_plan.spans] != [
        "retain_context_only",
        "retain_context_only",
    ]:
        raise AssertionError("evaluative context or incomplete history became durable memory")

    self_description_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000008",
        "I consider myself a blue-collar guy.",
        parent_lane="personal_history",
    )
    if self_description_plan.spans[0].disposition != "review_claim_span":
        raise AssertionError("stable self-description did not become a review-only claim")

    multi_preference_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000009",
        "I like Jim Croce, and I also like Billy Joel.",
    )
    if multi_preference_plan.spans[0].disposition != "manual_split_required":
        raise AssertionError("multiple preferences were treated as one atomic span")

    subjective_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000010",
        "They are very nice people.",
        parent_lane="personal_history",
    )
    if subjective_plan.spans[0].disposition != "review_belief_span":
        raise AssertionError("subjective evaluation was treated as an external fact")

    leading_question_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000011",
        "And why am I building this?",
    )
    if leading_question_plan.spans[0].disposition != "exclude_question":
        raise AssertionError("leading conjunction hid a direct question")

    hedged_history_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000012",
        "I started out taking I think three classes at a time.",
        parent_lane="personal_history",
    )
    if hedged_history_plan.spans[0].span_kind != "uncertain_biographical_assertion":
        raise AssertionError("hedged personal history lost its uncertainty qualifier")

    conditional_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000013",
        "If I exercise, I think I recover better.",
    )
    if conditional_plan.spans[0].disposition != "review_belief_span":
        raise AssertionError("complete conditional belief was suppressed as a fragment")

    conditional_fragment_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000016",
        "In my opinion, if I got people exercising and eating healthy.",
    )
    if conditional_fragment_plan.spans[0].disposition != "retain_context_only":
        raise AssertionError("incomplete conditional fragment became durable memory")

    mixed_evaluation_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000014",
        "I grew up in the 1970s, which was pretty cool.",
        parent_lane="personal_history",
    )
    if mixed_evaluation_plan.spans[0].disposition != "manual_split_required":
        raise AssertionError("history plus evaluation was treated as one atomic fact")

    transcription_plan = forced_split_plan(
        "30000000-0000-4000-8000-000000000015",
        "There are many farmers there and I want to Luxembourg Casco high school.",
        parent_lane="personal_history",
    )
    if transcription_plan.spans[0].disposition != "manual_split_required":
        raise AssertionError("speech-to-text school history was treated as a preference")

    report = atomic_span_dry_run_report(
        [
            mixed_plan,
            correction_plan,
            structured_plan,
            pasted_plan,
            compound_plan,
            project_preference_plan,
            contextual_plan,
            self_description_plan,
            multi_preference_plan,
            subjective_plan,
            leading_question_plan,
            hedged_history_plan,
            conditional_plan,
            conditional_fragment_plan,
            mixed_evaluation_plan,
            transcription_plan,
        ],
        owner_user_id=OWNER,
        batch_source_count=16,
    )
    if report["mode"] != "atomic_span_dry_run_no_writes":
        raise AssertionError("atomic report is not dry-run")
    if any(
        report["controls"][key]
        for key in (
            "database_writes",
            "evidence_writes",
            "candidate_writes",
            "claim_writes",
            "qdrant_writes",
        )
    ):
        raise AssertionError("atomic report enabled a write path")
    if report["controls"]["full_source_text_in_report"]:
        raise AssertionError("atomic report duplicates full source text")
    if not all(item["coverage_verified"] for item in report["sources"]):
        raise AssertionError("atomic report contains unverified source coverage")

    print("memory_v1_atomic_spans: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
