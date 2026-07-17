#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from rag_engine.memory_v1_atomic_spans import build_atomic_span_plan
from rag_engine.memory_v1_compound_spans import resolve_compound_plans
from rag_engine.memory_v1_evidence_persistence import (
    ExistingEvidenceRow,
    EvidencePersistencePlanError,
    build_evidence_span_plans,
    build_persistence_decisions,
    evidence_persistence_dry_run_report,
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
        created_at=datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc),
        thread_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        vantage_id="RESSE",
        request_id="evidence-persistence-test",
    )


def forced_atomic_plan(source_row: TriageSourceRow):
    decision = classify_source(source_row, artifact_entries={}, existing_evidence={})
    decision = replace(
        decision,
        primary_lane="personal_history",
        lanes=("personal_history",),
        requires_atomic_split=True,
        candidate_action="split_before_candidate",
        candidate_target="claim",
    )
    return build_atomic_span_plan(decision)


def existing_from_plan(plan, **changes) -> ExistingEvidenceRow:
    values = {
        "evidence_id": plan.evidence_id,
        "owner_user_id": plan.owner_user_id,
        "kind": plan.kind,
        "source_system": plan.source_system,
        "external_id": plan.external_id,
        "content": plan.content,
        "content_sha256": plan.content_sha256,
        "observed_at": plan.observed_at,
        "directness": plan.directness,
        "source_reliability": plan.source_reliability,
        "independence_key": plan.independence_key,
        "sensitivity": plan.sensitivity,
        "status": plan.status,
        "metadata": plan.metadata,
    }
    values.update(changes)
    return ExistingEvidenceRow(**values)


def main() -> int:
    atomic = forced_atomic_plan(
        source(
            "50000000-0000-4000-8000-000000000001",
            (
                "I went to the University of Wisconsin. "
                "My mom’s name was DeeDee my dad’s name is Jerry. "
                "I like concise answers."
            ),
        )
    )
    resolutions = resolve_compound_plans([atomic])
    plans = build_evidence_span_plans(
        [atomic],
        resolutions,
        owner_user_id=OWNER,
    )
    if not plans:
        raise AssertionError("review spans did not enter the persistence plan")
    if any(plan.disposition == "manual_split_required" for plan in plans):
        raise AssertionError("compound parent entered the persistence plan")
    if any(plan.content != atomic.source_decision.source.text[plan.char_start:plan.char_end] for plan in plans):
        raise AssertionError("planned span does not reproduce source offsets")
    if any(sha256_text(plan.content) != plan.content_sha256 for plan in plans):
        raise AssertionError("planned span content is not hash locked")
    if any(plan.metadata["candidate_creation_authorized"] for plan in plans):
        raise AssertionError("evidence plan authorized candidate creation")
    if any(plan.metadata["prompt_eligible"] for plan in plans):
        raise AssertionError("evidence plan authorized prompt use")
    if any(
        plan.disposition == "review_belief_span"
        and plan.epistemic_role != "user_belief_or_opinion"
        for plan in plans
    ):
        raise AssertionError("belief evidence lost its epistemic role")

    approximate = forced_atomic_plan(
        source(
            "50000000-0000-4000-8000-000000000002",
            (
                "I retired about five years ago. "
                "I remember thinking this working stuff really sucks. "
                "I believe antidepressant medication is minimally effective."
            ),
        )
    )
    approximate_resolutions = resolve_compound_plans([approximate])
    reviewed = build_evidence_span_plans(
        [approximate],
        approximate_resolutions,
        owner_user_id=OWNER,
    )
    if not any(
        "uncertainty_qualifier_required" in plan.review_flags
        for plan in reviewed
        if "retired about five years" in plan.content
    ):
        raise AssertionError("approximate autobiographical evidence lacks uncertainty")
    if not any(
        plan.epistemic_role == "mixed_user_assertion_and_belief"
        for plan in reviewed
        if "working stuff really sucks" in plan.content
    ):
        raise AssertionError("mixed assertion/opinion evidence lost its mixed role")
    health = [
        plan
        for plan in reviewed
        if "antidepressant medication" in plan.content
    ]
    if not health or any(plan.sensitivity != "restricted" for plan in health):
        raise AssertionError("high-stakes health belief was not restricted")
    if any(plan.epistemic_role != "user_belief_or_opinion" for plan in health):
        raise AssertionError("health belief became an external assertion")

    replay = build_evidence_span_plans(
        [atomic],
        resolutions,
        owner_user_id=OWNER,
    )
    if [plan.evidence_id for plan in replay] != [plan.evidence_id for plan in plans]:
        raise AssertionError("planned evidence IDs are not deterministic")

    first = plans[0]
    exact = existing_from_plan(first)
    exact_decisions = build_persistence_decisions(plans, [exact])
    if exact_decisions[0].action != "reuse_exact":
        raise AssertionError("exact replay was not reused")

    conflicting = existing_from_plan(first, content_sha256="0" * 64)
    conflict_decisions = build_persistence_decisions(plans, [conflicting])
    if conflict_decisions[0].action != "conflict_fail_closed":
        raise AssertionError("content conflict did not fail closed")

    covering = ExistingEvidenceRow(
        evidence_id=uuid.UUID("60000000-0000-4000-8000-000000000001"),
        owner_user_id=OWNER,
        kind="user_statement",
        source_system="public.chat_log",
        external_id=f"chat_log:{first.source_id}",
        content=first.source_content,
        content_sha256=first.source_content_sha256,
        observed_at=first.observed_at,
        directness=None,
        source_reliability=None,
        independence_key=None,
        sensitivity="medium",
        status="active",
        metadata={},
    )
    covering_decisions = build_persistence_decisions(plans, [covering])
    if covering.evidence_id not in covering_decisions[0].covering_source_evidence_ids:
        raise AssertionError("covering source evidence was not recorded")
    if covering_decisions[0].action != "plan_insert":
        raise AssertionError("covering source evidence replaced the exact span row")

    report = evidence_persistence_dry_run_report(
        covering_decisions,
        owner_user_id=OWNER,
        atomic_review_span_count=sum(
            plan.span_origin == "atomic" for plan in plans
        ),
        compound_review_span_count=sum(
            plan.span_origin == "compound_child" for plan in plans
        ),
    )
    if report["mode"] != "evidence_persistence_plan_dry_run_no_writes":
        raise AssertionError("report mode is not dry-run-only")
    if any(
        report["controls"][key]
        for key in (
            "database_writes",
            "evidence_writes",
            "candidate_writes",
            "claim_writes",
            "preference_writes",
            "qdrant_writes",
        )
    ):
        raise AssertionError("report exposes a write path")
    if report["controls"]["apply_option_exposed"]:
        raise AssertionError("report exposes an apply option")
    if any("content" in row for row in report["rows"]):
        raise AssertionError("report includes an exact span content field")

    try:
        build_evidence_span_plans(
            [atomic],
            resolutions,
            owner_user_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        )
    except EvidencePersistencePlanError:
        pass
    else:
        raise AssertionError("cross-owner plan was not rejected")

    print("memory_v1_evidence_persistence: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
