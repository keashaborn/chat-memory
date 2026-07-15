#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from rag_engine.memory_v1_preference_project_retrieval import (
    SpecializedRetrievalError,
    evaluate_specialized_memory,
)


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = (
    ROOT / "evals" / "memory_v1_preference_project_retrieval_cases_20260714.jsonl"
)
EVIDENCE_ID = "11111111-1111-4111-8111-111111111111"


def preference(
    preference_id: str,
    revision_id: str,
    key: str,
    content_hash: str,
    *,
    preference_class: str,
    domain: str,
    value: dict[str, object],
    scope: dict[str, object],
    surface: str,
    polarity: str = "prefer",
) -> dict[str, object]:
    review_id = "22222222-2222-4222-8222-222222222222"
    return {
        "preference_id": preference_id,
        "status": "active",
        "preference_class": preference_class,
        "preference_domain": domain,
        "preference_key": key,
        "value": value,
        "polarity": polarity,
        "scope": scope,
        "stability": "stable",
        "surface_policy": surface,
        "sensitivity": "high",
        "current_revision_id": revision_id,
        "head_revision_number": 1,
        "head_content_sha256": content_hash,
        "head_accepted_review_id": review_id,
        "revision_id": revision_id,
        "revision_number": 1,
        "content_sha256": content_hash,
        "accepted_review_id": review_id,
        "evidence_ids": [EVIDENCE_ID],
        "evidence_link_count": 1,
        "active_evidence_count": 1,
    }


def project_record(
    knowledge_id: str,
    revision_id: str,
    key: str,
    content_hash: str,
    *,
    kind: str,
    text: str,
    document_state: str,
    authority: str = "user_reported",
) -> dict[str, object]:
    return {
        "project_id": "08cd6a8a-5599-43d5-8d5c-b59401df8ccc",
        "project_key": "verbal-sage",
        "knowledge_id": knowledge_id,
        "revision_id": revision_id,
        "revision_number": 1,
        "knowledge_key": key,
        "knowledge_kind": kind,
        "canonical_text": text,
        "content_sha256": content_hash,
        "document_state": document_state,
        "authority_level": authority,
        "effective_from": None,
        "effective_to": None,
        "sensitivity": "high",
        "relation_count": 0,
        "evidence_ids": [EVIDENCE_ID],
        "evidence_link_count": 1,
        "active_evidence_count": 1,
    }


PREFERENCES = [
    preference(
        "3bdc1113-432d-4e8e-bcfb-6d761d1aa125",
        "ee28ae63-5aeb-4d8a-a12c-b396d0f5ca65",
        "music.concrete_surface_with_underlying_meaning",
        "75298919eff959f5d9b7e8c1e5780b096667aa1f9a4558fbd33b6340b02308e7",
        preference_class="life",
        domain="music",
        value={
            "canonical_text": "User likes songs that describe one concrete thing while implying a different underlying meaning.",
            "concrete_surface_subject": True,
            "different_underlying_meaning": True,
        },
        scope={"context": "music_recommendation"},
        surface="mention_when_relevant",
    ),
    preference(
        "4c1f4939-9fce-469d-8b8c-7ffcef1f660c",
        "09d0b335-d892-476a-bab5-dc98c74de223",
        "music.prominent_vocals_and_metaphorical_lyrics",
        "4d0058d0f766e0667c931f7ee4d1240993b9341bf72b3e2a405748688fae65ea",
        preference_class="life",
        domain="music",
        value={
            "canonical_text": "User prefers music with prominent vocals, ideally including metaphorical lyrics.",
            "prominent_vocals": True,
            "metaphorical_lyrics": "ideal",
        },
        scope={"context": "music_recommendation"},
        surface="mention_when_relevant",
    ),
    preference(
        "6c85c736-c76a-4a62-8ff7-9f36ec99a31e",
        "2cf568cb-71cb-4916-8a51-fdb0a950ac51",
        "enjoys_watching_creatures_in_woods",
        "d8dfc7a326eb8cf4970c27ead8bdfabdf7d668f75cac9ca284708cf6f4cc8f8e",
        preference_class="life",
        domain="outdoors",
        value={
            "canonical_text": "The user finds looking at creatures out in the woods peaceful."
        },
        scope={},
        surface="mention_when_relevant",
    ),
    preference(
        "a02f4efc-24f1-4552-97fe-f86faacbb52f",
        "e940c0fb-bd1e-4aff-889b-ed2406367f69",
        "personal_memory.jerry_deedee.out_of_context_surfacing",
        "24efbe64e67f71721fca9525e083c795f68f00b3ad31c89e50a3968568ef1936",
        preference_class="response",
        domain="memory_surfacing",
        value={
            "canonical_text": "Do not surface memories about Jerry or DeeDee unless they are directly relevant to the user’s current request.",
            "behavior": "surface_only_when_directly_relevant",
            "people": ["Jerry", "DeeDee"],
        },
        scope={"people": ["Jerry", "DeeDee"]},
        surface="never_surface_as_content",
        polarity="avoid",
    ),
]

PROJECT_RECORDS = [
    project_record(
        "7dde9aee-121a-4772-91bf-2d6da60ffa48",
        "c366f75f-34ee-466a-a650-d66d7c3a71eb",
        "behavior_change.ab_design_background_evaluation",
        "e6652007bf9a4ce3e204e5122f8354c667d77a3222836a094209443c9b1f9ec2",
        kind="requirement",
        text="The project should include an underlying AB-design system that can guide interventions and evaluate their effects without requiring users to manage the experimental structure directly.",
        document_state="proposed",
    ),
    project_record(
        "f0e860e2-70e5-400a-8bf4-61d96c00d3c8",
        "7c99d8e5-475c-4632-a97c-3bdb747cc0d3",
        "fractal_monism.dataset_ai_feedback_history",
        "c3b0c48d30ab14c8087ca7a57a9e222fab0c5cfe42160e66de6115bc9a08e81b",
        kind="status",
        text="Project dataset work has included AI, possible AI consciousness, and feedback mechanisms that promote or demote memories.",
        document_state="historical",
    ),
    project_record(
        "8c8d5805-82e5-4842-bb88-76759caac33a",
        "d643c5fd-6522-4df2-b2ef-f291ad73a854",
        "memory.fractal_monism_feedback_loop",
        "be6c2ed3d0c2a4211a3d8224a59163ab7c0cf8ff64c02138b9c7c2f2fffdf2f6",
        kind="roadmap",
        text="The project’s long-term goal is a Fractal Monism-informed feedback loop that weights memories to move AI responses toward more human-like behavior.",
        document_state="proposed",
    ),
    project_record(
        "fb9f310f-8624-4457-91f5-1d5308f72b1c",
        "5c730b2d-0a88-4739-8582-31166558b776",
        "memory_system.hybrid_legacy_and_cards_2026_07_02",
        "28dd6905ade0d0f867e18f32481e71a8880d6561aeaae02904ec89630b27a907",
        kind="status",
        text="At the source date, the project contained an older behavioral-learning memory system alongside newer memory cards.",
        document_state="historical",
    ),
    project_record(
        "a6ac487d-7852-4e3b-a2d0-778f8c1cdd6b",
        "4f18d33e-44ba-4280-88f9-46fba377e8be",
        "website.ai_assisted_development_history",
        "ef3aee64179670cc475c54329090bf363391465639d6106e1ea838ef81ff4bad",
        kind="status",
        text="User reports that the website was built through AI-assisted coding: the user specified desired behavior and transferred code through VS Code.",
        document_state="historical",
    ),
]


def cases() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evaluate(case: dict[str, object]) -> dict[str, object]:
    return evaluate_specialized_memory(
        preferences=PREFERENCES,
        project_records=PROJECT_RECORDS,
        query=str(case["query"]),
        memory_intent=str(case["memory_intent"]),
        domains=list(case["domains"]),
        candidate_entities=list(case["candidate_entities"]),
        direct_relevance=bool(case["direct_relevance"]),
        project_key=str(case["project_key"]),
        max_sensitivity=str(case["max_sensitivity"]),
        as_of="2026-07-14T12:00:00Z",
    )


def main() -> int:
    for case in cases():
        result = evaluate(case)
        control_keys = [item["preference_key"] for item in result["policy_controls"]]
        preference_keys = [
            item["preference_key"] for item in result["selected_preferences"]
        ]
        project_keys = [
            item["knowledge_key"] for item in result["selected_project_records"]
        ]
        if control_keys != case["expected_controls"]:
            raise AssertionError(
                f"{case['case_id']} controls: {control_keys} != {case['expected_controls']}"
            )
        if preference_keys != case["expected_preferences"]:
            raise AssertionError(
                f"{case['case_id']} preferences: {preference_keys} != {case['expected_preferences']}"
            )
        if project_keys != case["expected_project_records"]:
            raise AssertionError(
                f"{case['case_id']} projects: {project_keys} != {case['expected_project_records']}"
            )
        if "expected_control_suppression" in case:
            suppressed = bool(result["policy_controls"][0]["would_suppress"])
            if suppressed != case["expected_control_suppression"]:
                raise AssertionError(f"{case['case_id']} control suppression changed")
        if result["prompt_injection"] or result["retrieval_activation"]:
            raise AssertionError(f"{case['case_id']} activated retrieval")
        if result["database_writes"] != 0:
            raise AssertionError(f"{case['case_id']} reported a database write")
        if any(
            item["preference_class"] == "response"
            for item in result["selected_preferences"]
        ):
            raise AssertionError("response control entered the content lane")
        if json.dumps(result, sort_keys=True, default=str) != json.dumps(
            evaluate(case), sort_keys=True, default=str
        ):
            raise AssertionError(f"{case['case_id']} was not deterministic")

    try:
        evaluate_specialized_memory(
            preferences=PREFERENCES,
            project_records=PROJECT_RECORDS,
            query="test",
            memory_intent="general",
            max_sensitivity="invalid",
        )
    except SpecializedRetrievalError:
        pass
    else:
        raise AssertionError("invalid sensitivity was accepted")

    zero_budget = evaluate_specialized_memory(
        preferences=PREFERENCES,
        project_records=PROJECT_RECORDS,
        query="Recommend music",
        memory_intent="recommendation",
        domains=["music"],
        project_key="verbal-sage",
        max_tokens=0,
        as_of="2026-07-14T12:00:00Z",
    )
    if zero_budget["selected_content_count"] != 0:
        raise AssertionError("zero token budget selected content")

    authority_low = project_record(
        "aaaaaaaa-1111-4111-8111-111111111111",
        "aaaaaaaa-2222-4222-8222-222222222222",
        "memory.architecture.alpha",
        "a" * 64,
        kind="architecture",
        text="Memory architecture baseline.",
        document_state="ratified",
        authority="user_reported",
    )
    authority_high = project_record(
        "bbbbbbbb-1111-4111-8111-111111111111",
        "bbbbbbbb-2222-4222-8222-222222222222",
        "memory.architecture.beta",
        "b" * 64,
        kind="architecture",
        text="Memory architecture baseline.",
        document_state="ratified",
        authority="approved_spec",
    )
    authority_result = evaluate_specialized_memory(
        preferences=[],
        project_records=[authority_low, authority_high],
        query="Review the memory architecture baseline.",
        memory_intent="project_status",
        domains=["memory_architecture"],
        project_key="verbal-sage",
        direct_relevance=True,
        max_project_records=1,
        as_of="2026-07-14T12:00:00Z",
    )
    selected_authority = authority_result["selected_project_records"]
    if [item["authority_level"] for item in selected_authority] != ["approved_spec"]:
        raise AssertionError(f"authority precedence failed: {selected_authority}")

    print("memory_v1_preference_project_retrieval: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
