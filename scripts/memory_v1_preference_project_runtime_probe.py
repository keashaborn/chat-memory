#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid

import requests


CASES = (
    {
        "case_id": "music_recommendation",
        "message": "Recommend music with strong vocals and layered meaning.",
        "expected_status": "ok",
        "expected_preferences": [
            "music.concrete_surface_with_underlying_meaning",
            "music.prominent_vocals_and_metaphorical_lyrics",
        ],
        "expected_projects": [],
    },
    {
        "case_id": "memory_feedback_roadmap",
        "message": "Plan the Fractal Monism memory feedback loop.",
        "expected_status": "ok",
        "expected_preferences": [],
        "expected_projects": ["memory.fractal_monism_feedback_loop"],
    },
    {
        "case_id": "unrelated_turn",
        "message": "How do I stop popups when my Mac restarts?",
        "expected_status": "skipped",
        "expected_preferences": [],
        "expected_projects": [],
    },
)
FORBIDDEN_PROMPT_FRAGMENTS = (
    "music.concrete_surface_with_underlying_meaning",
    "music.prominent_vocals_and_metaphorical_lyrics",
    "memory.fractal_monism_feedback_loop",
    "User likes songs that describe one concrete thing",
    "User prefers music with prominent vocals",
    "The project’s long-term goal is a Fractal Monism-informed feedback loop",
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
        request_id = f"memory-v1-specialized-probe-{case['case_id']}-{uuid.uuid4()}"
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
        shadow = (
            body.get("meta_explanation", {})
            .get("vantage", {})
            .get("turn_plan", {})
            .get("memory_v1_preference_project_shadow", {})
        )
        prompt = str(body.get("system_prompt") or "")
        prompt_leaks = [value for value in FORBIDDEN_PROMPT_FRAGMENTS if value in prompt]
        if body.get("answer") != "":
            raise AssertionError(f"{case['case_id']}: inspect-only returned an answer")
        if shadow.get("status") != case["expected_status"]:
            raise AssertionError(f"{case['case_id']}: {shadow}")
        if shadow.get("selected_preference_keys", []) != case["expected_preferences"]:
            raise AssertionError(f"{case['case_id']}: {shadow}")
        if shadow.get("selected_project_keys", []) != case["expected_projects"]:
            raise AssertionError(f"{case['case_id']}: {shadow}")
        if prompt_leaks:
            raise AssertionError(
                f"{case['case_id']}: specialized content entered prompt: {prompt_leaks}"
            )
        if shadow.get("prompt_injection") is True:
            raise AssertionError(f"{case['case_id']}: prompt injection activated")
        if shadow.get("answer_model_exposure") is True:
            raise AssertionError(f"{case['case_id']}: answer model exposure activated")
        reports.append(
            {
                "case_id": case["case_id"],
                "http_status": response.status_code,
                "shadow": shadow,
                "answer_empty": True,
                "specialized_prompt_leaks": prompt_leaks,
            }
        )

    print(
        json.dumps(
            {
                "version": "memory_v1_preference_project_runtime_probe_v1",
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
