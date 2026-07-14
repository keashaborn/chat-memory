#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid

from rag_engine.memory_v1_preference_project_shadow import (
    VERSION,
    _empty_result,
    _trace_metadata,
    run_preference_project_shadow,
)


def main() -> int:
    original = dict(os.environ)
    try:
        os.environ.pop("MEMORY_V1_SPECIALIZED_SHADOW", None)
        disabled = run_preference_project_shadow(
            str(uuid.uuid4()),
            query="Recommend music",
            request_classification="GENERAL",
        )
        if disabled != {"version": VERSION, "status": "disabled"}:
            raise AssertionError(disabled)

        os.environ["MEMORY_V1_SPECIALIZED_SHADOW"] = "1"
        os.environ["MEMORY_V1_SHADOW_USER_IDS"] = str(uuid.uuid4())
        skipped = run_preference_project_shadow(
            str(uuid.uuid4()),
            query="Recommend music",
            request_classification="GENERAL",
        )
        if skipped.get("reason") != "actor_not_allowlisted":
            raise AssertionError(skipped)
    finally:
        os.environ.clear()
        os.environ.update(original)

    intent_plan = {
        "version": "memory_intent_adapter_v2",
        "request_classification": "GENERAL",
        "reason_codes": ["explicit_music_recommendation"],
        "candidate_entities": [],
        "direct_relevance": False,
    }
    result = {
        "version": "memory_v1_preference_project_shadow_v1",
        "domains": ["music"],
        "project_key": None,
        "policy_controls": [],
        "selected_preferences": [
            {
                "preference_id": "11111111-1111-4111-8111-111111111111",
                "revision_id": "22222222-2222-4222-8222-222222222222",
                "preference_key": "music.example",
                "value": {"canonical_text": "must never enter the trace"},
            }
        ],
        "selected_project_records": [
            {
                "project_id": "33333333-3333-4333-8333-333333333333",
                "knowledge_id": "44444444-4444-4444-8444-444444444444",
                "revision_id": "55555555-5555-4555-8555-555555555555",
                "knowledge_key": "project.example",
                "lexical_score": 4,
                "canonical_text": "must never enter the trace",
            }
        ],
        "token_estimate": 42,
        "rejected_counts": {},
    }
    metadata = _trace_metadata(intent_plan, result)
    encoded = json.dumps(metadata, sort_keys=True)
    if "must never enter the trace" in encoded or "canonical_text" in encoded:
        raise AssertionError("selected content leaked into diagnostic metadata")
    if metadata["prompt_injection"] or metadata["answer_model_exposure"]:
        raise AssertionError("shadow metadata reported model exposure")
    if metadata["retrieval_activation"]:
        raise AssertionError("shadow metadata reported retrieval activation")

    skipped_plan = {
        "version": "memory_intent_adapter_v2",
        "request_classification": "GENERAL",
        "memory_intent": "none",
        "domains": [],
        "project_key": None,
        "reason_codes": ["no_governed_memory_need"],
        "candidate_entities": [],
        "direct_relevance": False,
    }
    skipped_metadata = _trace_metadata(
        skipped_plan,
        _empty_result(skipped_plan),
        decision_status="skipped",
        evaluation_elapsed_ms=12.3456,
        skip_reason="no_specialized_memory_need",
    )
    if skipped_metadata["decision_status"] != "skipped":
        raise AssertionError(skipped_metadata)
    if skipped_metadata["evaluation_elapsed_ms"] != 12.346:
        raise AssertionError(skipped_metadata)
    if skipped_metadata["skip_reason"] != "no_specialized_memory_need":
        raise AssertionError(skipped_metadata)
    skipped_encoded = json.dumps(skipped_metadata, sort_keys=True)
    if "query" in skipped_encoded or "canonical_text" in skipped_encoded:
        raise AssertionError("skipped-turn content leaked into diagnostic metadata")

    print("memory_v1_preference_project_shadow: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
