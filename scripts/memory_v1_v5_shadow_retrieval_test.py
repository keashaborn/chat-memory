#!/usr/bin/env python3
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_retrieval import (
    V5ShadowRetrievalError,
    evaluate_v5_shadow_claims,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"


def record() -> dict:
    return {
        "owner_user_id": OWNER,
        "claim_id": CLAIM,
        "canonical_key": "v5:" + "a" * 64,
        "canonical_text": "The user works as a personal trainer.",
        "predicate": "occupation.works_as",
        "status": "supported",
        "sensitivity": "medium",
        "importance": 0.6,
        "salience": 0.5,
        "valid_from": "2026-07-14T17:15:20+00:00",
        "valid_to": None,
        "metadata": {"memory_contract": "memory_projection_v5"},
        "retrieval_policy": {
            "surface_policy": "direct_or_relevant",
            "projection_class": "direct_claim",
        },
        "projection_review_decision": "authorized",
        "projection_apply_outcome": "applied",
        "evidence_by_stance": {
            "supports": ["fca9e5dc-83c2-4456-8db8-1fe6102eb74d"],
            "opposes": [],
            "qualifies": [],
            "context": [],
        },
        "observation_ids": ["9bf1e6b2-1840-4524-98dc-142567ebe013"],
        "project_key": None,
        "component_key": None,
    }


def evaluate(value: dict) -> dict:
    return evaluate_v5_shadow_claims(
        OWNER,
        query="What kind of work do I do?",
        intent="profile_recall",
        domain="profile",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[value],
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def main() -> None:
    selected = evaluate(record())
    assert selected["selected_count"] == 1
    assert selected["claims"][0]["predicate"] == "occupation.works_as"
    assert "score" not in selected["claims"][0]
    assert selected["prompt_injection"] is False
    assert selected["answer_model_exposure"] is False
    assert selected["retrieval_activation"] is False
    assert selected["rejected_counts"] == {}

    directly_relevant = record()
    directly_relevant["retrieval_policy"][
        "surface_policy"
    ] = "mention_when_directly_relevant"
    directly_relevant_selected = evaluate(directly_relevant)
    assert directly_relevant_selected["selected_count"] == 1
    assert (
        directly_relevant_selected["claims"][0]["use_instruction"]
        == "mention_only_when_directly_relevant"
    )
    assert directly_relevant_selected["rejected_counts"] == {}

    candidate = record()
    candidate["status"] = "candidate"
    blocked = evaluate(candidate)
    assert blocked["selected_count"] == 0
    assert blocked["rejected_counts"]["status:candidate"] == 1

    not_visible = evaluate_v5_shadow_claims(
        OWNER,
        query="What kind of work do I do?",
        intent="profile_recall",
        domain="profile",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[],
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert not_visible["selected_count"] == 0
    assert not_visible["visible_candidate_count"] == 0
    assert not_visible["rejected_counts"] == {"not_visible": 1}

    no_evidence = record()
    no_evidence["evidence_by_stance"]["supports"] = []
    assert evaluate(no_evidence)["rejected_counts"]["no_active_evidence"] == 1

    unsupported = record()
    unsupported["evidence_by_stance"]["supports"] = []
    unsupported["evidence_by_stance"]["opposes"] = [
        "36e92633-08fd-441f-8d1d-27f16c2a3479"
    ]
    assert evaluate(unsupported)["rejected_counts"]["no_supporting_evidence"] == 1

    no_observation = record()
    no_observation["observation_ids"] = []
    assert (
        evaluate(no_observation)["rejected_counts"]["no_observation_provenance"] == 1
    )

    wrong_policy = record()
    wrong_policy["retrieval_policy"] = {"surface": "direct"}
    assert (
        evaluate(wrong_policy)["rejected_counts"]["invalid_surface_policy"] == 1
    )

    zero_token = record()
    zero_token["retrieval_policy"]["surface_policy"] = "zero_token_control_only"
    assert evaluate(zero_token)["selected_count"] == 0

    component = record()
    component["retrieval_policy"]["surface_policy"] = "exact_project_scope_only"
    component["project_key"] = "verbal-sage"
    component["component_key"] = "memory-v1"
    component_selected = evaluate_v5_shadow_claims(
        OWNER,
        query="What is the Memory V1 requirement?",
        intent="technical_project_recall",
        domain="project",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[component],
        project_key="verbal-sage",
        component_key="memory-v1",
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert component_selected["selected_count"] == 1
    assert component_selected["claims"][0]["component_key"] == "memory-v1"

    sibling_blocked = evaluate_v5_shadow_claims(
        OWNER,
        query="What is the RESSE requirement?",
        intent="technical_project_recall",
        domain="project",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[component],
        project_key="verbal-sage",
        component_key="resse",
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert sibling_blocked["selected_count"] == 0
    assert sibling_blocked["rejected_counts"]["surface_policy"] == 1

    root_blocked = evaluate_v5_shadow_claims(
        OWNER,
        query="What is the root project requirement?",
        intent="technical_project_recall",
        domain="project",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[component],
        project_key="verbal-sage",
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert root_blocked["selected_count"] == 0

    root = deepcopy(component)
    root["component_key"] = None
    root_selected = evaluate_v5_shadow_claims(
        OWNER,
        query="What is the root project requirement?",
        intent="technical_project_recall",
        domain="project",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[root],
        project_key="verbal-sage",
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert root_selected["selected_count"] == 1

    try:
        evaluate_v5_shadow_claims(
            OWNER,
            query="Invalid component-only scope",
            intent="technical_project_recall",
            domain="project",
            allowed_predicate_prefixes=["occupation."],
            candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
            records=[component],
            component_key="memory-v1",
        )
    except V5ShadowRetrievalError as exc:
        assert "requires project_key" in str(exc)
    else:
        raise AssertionError("component-only retrieval scope was accepted")

    expired = record()
    expired["valid_to"] = "2026-07-15T00:00:00+00:00"
    assert evaluate(expired)["rejected_counts"]["expired"] == 1

    disallowed = record()
    disallowed_result = evaluate_v5_shadow_claims(
        OWNER,
        query="Tell me about nutrition.",
        intent="nutrition",
        domain="nutrition",
        allowed_predicate_prefixes=["nutrition."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
        records=[disallowed],
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert disallowed_result["rejected_counts"]["predicate_permission"] == 1

    other_owner = record()
    other_owner["owner_user_id"] = OTHER
    try:
        evaluate(other_owner)
    except V5ShadowRetrievalError as exc:
        assert "owner mismatch" in str(exc)
    else:
        raise AssertionError("cross-owner snapshot was accepted")

    duplicate = record()
    try:
        evaluate_v5_shadow_claims(
            OWNER,
            query="What kind of work do I do?",
            intent="profile_recall",
            domain="profile",
            allowed_predicate_prefixes=["occupation."],
            candidate_hits=[{"claim_id": CLAIM, "semantic_score": 0.91}],
            records=[duplicate, deepcopy(duplicate)],
        )
    except V5ShadowRetrievalError as exc:
        assert "duplicate claim" in str(exc)
    else:
        raise AssertionError("duplicate snapshot claim was accepted")

    print("memory_v1_v5_shadow_retrieval: PASS")


if __name__ == "__main__":
    main()
