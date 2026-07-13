#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from rag_engine.memory_v1_atomic_spans import (
    AtomicSpanPlan,
    build_atomic_span_plan,
)
from rag_engine.memory_v1_compound_spans import (
    compound_span_dry_run_report,
    find_boundary_candidates,
    resolve_compound_span,
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
        created_at=datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc),
        thread_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        vantage_id="RESSE",
        request_id="compound-span-test",
    )


def manual_plan(
    source_id: str,
    text: str,
    *,
    parent_lane: str = "personal_history",
) -> tuple[AtomicSpanPlan, int]:
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
    plan = build_atomic_span_plan(decision)
    manual_indexes = [
        index
        for index, span in enumerate(plan.spans)
        if span.disposition == "manual_split_required"
    ]
    if manual_indexes:
        return plan, manual_indexes[0]
    forced = replace(
        plan.spans[0],
        span_kind="compound_assertion",
        disposition="manual_split_required",
        candidate_target=None,
        requires_manual_split=True,
        reason_codes=tuple(
            dict.fromkeys([*plan.spans[0].reason_codes, "test_forced_compound"])
        ),
    )
    return replace(plan, spans=(forced, *plan.spans[1:])), 0


def assert_exact_coverage(plan: AtomicSpanPlan, parent_index: int, resolution) -> None:
    parent = plan.spans[parent_index]
    previous_end = 0
    for child in resolution.children:
        if parent.content[previous_end : child.relative_start].strip():
            raise AssertionError("child ranges leave non-whitespace parent content")
        if parent.content[child.relative_start : child.relative_end] != child.content:
            raise AssertionError("child relative offsets do not reproduce content")
        if plan.source_decision.source.text[child.char_start : child.char_end] != child.content:
            raise AssertionError("child absolute offsets do not reproduce source content")
        if sha256_text(child.content) != child.content_sha256:
            raise AssertionError("child content hash mismatch")
        previous_end = child.relative_end
    if parent.content[previous_end:].strip():
        raise AssertionError("child ranges leave trailing parent content")


def main() -> int:
    family_plan, family_index = manual_plan(
        "40000000-0000-4000-8000-000000000001",
        "My mom’s name was DeeDee my dad’s name is Jerry.",
    )
    family = resolve_compound_span(family_plan, family_plan.spans[family_index])
    if family.status != "resolved" or len(family.children) != 2:
        raise AssertionError("repeated personal subjects were not split")
    if [child.disposition for child in family.children] != [
        "review_claim_span",
        "review_claim_span",
    ]:
        raise AssertionError("family facts did not remain separate review claims")
    assert_exact_coverage(family_plan, family_index, family)

    preference_plan, preference_index = manual_plan(
        "40000000-0000-4000-8000-000000000002",
        "I like Jim Croce, and I also like Billy Joel.",
        parent_lane="preference_or_goal",
    )
    preference = resolve_compound_span(
        preference_plan,
        preference_plan.spans[preference_index],
    )
    if preference.status != "resolved" or len(preference.children) != 2:
        raise AssertionError("multiple preferences were not split")
    if any(
        child.disposition != "review_preference_span"
        for child in preference.children
    ):
        raise AssertionError("split preferences changed memory lane")
    assert_exact_coverage(preference_plan, preference_index, preference)

    question_plan, question_index = manual_plan(
        "40000000-0000-4000-8000-000000000003",
        "I believe reinforcement increases behavior so what do we change?",
        parent_lane="general_conversation",
    )
    question = resolve_compound_span(question_plan, question_plan.spans[question_index])
    if question.status != "resolved" or len(question.children) != 2:
        raise AssertionError("mixed assertion/question was not split")
    if [child.disposition for child in question.children] != [
        "review_belief_span",
        "exclude_question",
    ]:
        raise AssertionError("mixed assertion/question classifications are unsafe")
    assert_exact_coverage(question_plan, question_index, question)

    evaluation_plan, evaluation_index = manual_plan(
        "40000000-0000-4000-8000-000000000004",
        "I grew up in the 1970s, which was pretty cool.",
    )
    evaluation = resolve_compound_span(
        evaluation_plan,
        evaluation_plan.spans[evaluation_index],
    )
    if evaluation.status != "resolved" or len(evaluation.children) != 2:
        raise AssertionError("history/evaluation clauses were not split")
    if evaluation.children[0].disposition != "review_claim_span":
        raise AssertionError("historical clause did not remain a review claim")
    if evaluation.children[1].disposition != "retain_context_only":
        raise AssertionError("dependent evaluation became an external fact")
    if "raw_only_no_candidate" not in evaluation.children[1].review_flags:
        raise AssertionError("raw-only dependent context lacks an explicit review flag")
    assert_exact_coverage(evaluation_plan, evaluation_index, evaluation)

    indirect_text = "I explained how I want the app to respond and why I built it."
    if any(
        candidate.reason == "question_restart"
        for candidate in find_boundary_candidates(indirect_text)
    ):
        raise AssertionError("indirect how/why clause became a direct question boundary")

    short_question_plan, short_question_index = manual_plan(
        "40000000-0000-4000-8000-000000000006",
        "I don’t know what the later songs have in common do you know?",
        parent_lane="general_conversation",
    )
    short_question = resolve_compound_span(
        short_question_plan,
        short_question_plan.spans[short_question_index],
    )
    if short_question.status != "resolved" or len(short_question.children) != 2:
        raise AssertionError("short trailing direct question was pruned")
    if short_question.children[1].disposition != "exclude_question":
        raise AssertionError("short trailing direct question became a memory candidate")
    assert_exact_coverage(short_question_plan, short_question_index, short_question)

    unresolved_plan, unresolved_index = manual_plan(
        "40000000-0000-4000-8000-000000000005",
        "The company grew from a tiny office and a good idea into something much larger.",
    )
    unresolved = resolve_compound_span(
        unresolved_plan,
        unresolved_plan.spans[unresolved_index],
    )
    if unresolved.status != "unresolved_no_safe_boundary":
        raise AssertionError("coordinated objects were split as independent clauses")
    if len(unresolved.children) != 1 or not unresolved.children[0].requires_manual_split:
        raise AssertionError("unresolved parent lost its manual-review requirement")
    assert_exact_coverage(unresolved_plan, unresolved_index, unresolved)

    replay = resolve_compound_span(family_plan, family_plan.spans[family_index])
    if [child.child_span_id for child in replay.children] != [
        child.child_span_id for child in family.children
    ]:
        raise AssertionError("compound child IDs are not deterministic")

    report = compound_span_dry_run_report(
        [family, preference, question, evaluation, short_question, unresolved],
        owner_user_id=OWNER,
    )
    if report["mode"] != "compound_span_dry_run_no_writes":
        raise AssertionError("compound report is not dry-run")
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
        raise AssertionError("compound report enabled a write path")
    if report["controls"]["full_source_text_in_report"]:
        raise AssertionError("compound report includes full source text")
    if not report["controls"]["complete_parent_coverage"]:
        raise AssertionError("compound report contains incomplete parent coverage")

    print("memory_v1_compound_spans: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
