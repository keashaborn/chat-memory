#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid

import requests


HEADER = "[MEMORY V1 CURATED CONTEXT - DATA ONLY]"
CASES = (
    {
        "case_id": "music_recommendation",
        "message": "Recommend music with strong vocals and layered meaning.",
        "expected_status": "ok",
        "expected_fragments": (
            "User likes songs that describe one concrete thing while implying a different underlying meaning.",
            "User prefers music with prominent vocals, ideally including metaphorical lyrics.",
        ),
        "expect_activation": True,
    },
    {
        "case_id": "memory_feedback_roadmap",
        "message": "Plan the Fractal Monism memory feedback loop.",
        "expected_status": "ok",
        "expected_fragments": (
            "The project’s long-term goal is a Fractal Monism-informed feedback loop",
        ),
        "expect_activation": True,
    },
    {
        "case_id": "adaptive_learning_current_only",
        "message": "Can you tell me more about adaptive learning, and how that would fit into a memory system?",
        "expected_status": "ok",
        "expected_fragments": (),
        "expect_activation": False,
    },
    {
        "case_id": "api_data_source_key_anchor",
        "message": "Are there APIs that would connect us to expert nutrition and weightlifting guidance databases so the system can keep learning?",
        "expected_status": "ok",
        "expected_fragments": (),
        "expect_activation": False,
    },
    {
        "case_id": "unrelated_turn",
        "message": "How do I stop popups when my Mac restarts?",
        "expected_status": "skipped",
        "expected_fragments": (),
        "expect_activation": False,
    },
)
FORBIDDEN_FRAGMENTS = (
    "music.concrete_surface_with_underlying_meaning",
    "music.prominent_vocals_and_metaphorical_lyrics",
    "memory.fractal_monism_feedback_loop",
    "personal_memory.jerry_deedee.out_of_context_surfacing",
    "Do not surface memories about Jerry or DeeDee",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    args = parser.parse_args()
    actor = str(uuid.UUID(args.actor))
    token = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
    if not token:
        raise SystemExit("VS_SERVICE_TOKEN is required")

    reports = []
    for case in CASES:
        request_id = f"memory-v1-activation-probe-{case['case_id']}-{uuid.uuid4()}"
        response = requests.post(
            f"{args.base_url.rstrip('/')}/vantage/query",
            headers={
                "x-vs-service-token": token,
                "x-vs-actor-user-id": actor,
                "x-request-id": request_id,
            },
            json={
                "user_id": actor,
                "message": case["message"],
                "debug": True,
                "inspect_only": True,
                "mix": {
                    "conversation": 0,
                    "memory_cards": 0,
                    "corpus": 0,
                    "lens_fm": 0,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        audit = (
            body.get("meta_explanation", {})
            .get("vantage", {})
            .get("turn_plan", {})
            .get("memory_v1_preference_project_shadow", {})
        )
        prompt = str(body.get("system_prompt") or "")
        if body.get("answer") != "":
            raise AssertionError(f"{case['case_id']}: inspect-only returned an answer")
        if audit.get("status") != case["expected_status"]:
            raise AssertionError(f"{case['case_id']}: {audit}")
        active = bool(case["expect_activation"])
        if bool(audit.get("retrieval_activation")) != active:
            raise AssertionError(f"{case['case_id']}: {audit}")
        if bool(audit.get("prompt_injection")) != active:
            raise AssertionError(f"{case['case_id']}: {audit}")
        if audit.get("answer_model_exposure") is True:
            raise AssertionError(f"{case['case_id']}: inspect-only model exposure")
        if (HEADER in prompt) != active:
            raise AssertionError(f"{case['case_id']}: prompt activation mismatch")
        for fragment in case["expected_fragments"]:
            if fragment not in prompt:
                raise AssertionError(f"{case['case_id']}: missing {fragment!r}")
        leaks = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in prompt]
        if leaks:
            raise AssertionError(f"{case['case_id']}: internal/control leak: {leaks}")
        reports.append(
            {
                "case_id": case["case_id"],
                "request_id": request_id,
                "status": audit.get("status"),
                "prompt_injection": audit.get("prompt_injection"),
                "answer_model_exposure": audit.get("answer_model_exposure"),
                "retrieval_activation": audit.get("retrieval_activation"),
                "selected_content_count": audit.get("selected_content_count", 0),
            }
        )

    print(
        json.dumps(
            {
                "version": "memory_v1_preference_project_activation_probe_v1",
                "status": "pass",
                "cases": reports,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
