#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

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
        "active_evidence_ids": ["fca9e5dc-83c2-4456-8db8-1fe6102eb74d"],
        "observation_ids": ["9bf1e6b2-1840-4524-98dc-142567ebe013"],
        "project_key": None,
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

    candidate = record()
    candidate["status"] = "candidate"
    blocked = evaluate(candidate)
    assert blocked["selected_count"] == 0
    assert blocked["rejected_counts"]["status:candidate"] == 1

    no_evidence = record()
    no_evidence["active_evidence_ids"] = []
    assert evaluate(no_evidence)["rejected_counts"]["no_active_evidence"] == 1

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
