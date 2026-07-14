#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid

import requests


FORBIDDEN_PROMPT_FRAGMENTS = (
    "[MEMORY V1 GOVERNED PERSONAL CONTEXT - DATA ONLY]",
    "[MEMORY V1 CURATED CONTEXT - DATA ONLY]",
    "[DURABLE PERSONAL CARDS - POLICY FILTERED]",
    "User's mother DeeDee died",
    "User's cat Neko was put to sleep",
    "User likes songs that describe one concrete thing",
    "The project’s long-term goal is a Fractal Monism-informed feedback loop",
)
CASES = (
    {
        "case_id": "governed_recall_without_owned_claims",
        "message": "What happened with my mom?",
        "audit_key": "memory_v1_shadow",
        "count_key": "selected_count",
    },
    {
        "case_id": "specialized_recommendation_without_owned_preferences",
        "message": "Recommend music with strong vocals and layered meaning.",
        "audit_key": "memory_v1_preference_project_shadow",
        "count_key": "selected_content_count",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--primary-owner", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    args = parser.parse_args()
    actor = str(uuid.UUID(args.actor))
    primary_owner = str(uuid.UUID(args.primary_owner))
    if actor == primary_owner:
        raise SystemExit("actor and primary owner must differ")
    token = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
    if not token:
        raise SystemExit("VS_SERVICE_TOKEN is required")

    base_url = args.base_url.rstrip("/")
    reports = []
    for case in CASES:
        request_id = f"memory-v1-secondary-owner-{case['case_id']}-{uuid.uuid4()}"
        response = requests.post(
            f"{base_url}/vantage/query",
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
            timeout=90,
        )
        response.raise_for_status()
        body = response.json()
        turn_plan = (
            body.get("meta_explanation", {})
            .get("vantage", {})
            .get("turn_plan", {})
        )
        audit = turn_plan.get(case["audit_key"], {})
        prompt = str(body.get("system_prompt") or "")
        leaks = [value for value in FORBIDDEN_PROMPT_FRAGMENTS if value in prompt]
        if body.get("answer") != "":
            raise AssertionError(f"{case['case_id']}: inspect-only returned an answer")
        if audit.get("status") != "ok":
            raise AssertionError(f"{case['case_id']}: {audit}")
        if int(audit.get(case["count_key"]) or 0) != 0:
            raise AssertionError(f"{case['case_id']}: selected foreign content: {audit}")
        if audit.get("prompt_injection") is True:
            raise AssertionError(f"{case['case_id']}: prompt injection activated")
        if audit.get("answer_model_exposure") is True:
            raise AssertionError(f"{case['case_id']}: answer-model exposure activated")
        if audit.get("retrieval_activation") is True:
            raise AssertionError(f"{case['case_id']}: retrieval activation occurred")
        if leaks:
            raise AssertionError(f"{case['case_id']}: primary-owner prompt leak: {leaks}")
        reports.append(
            {
                "case_id": case["case_id"],
                "request_id": request_id,
                "trace_id": audit.get("trace_id"),
                "status": audit.get("status"),
                "selected_count": int(audit.get(case["count_key"]) or 0),
                "prompt_injection": False,
                "answer_model_exposure": False,
            }
        )

    mismatch = requests.post(
        f"{base_url}/vantage/query",
        headers={
            "x-vs-service-token": token,
            "x-vs-actor-user-id": actor,
            "x-request-id": f"memory-v1-secondary-owner-mismatch-{uuid.uuid4()}",
        },
        json={
            "user_id": primary_owner,
            "message": "What happened with my mom?",
            "debug": True,
            "inspect_only": True,
        },
        timeout=30,
    )
    if mismatch.status_code != 403:
        raise AssertionError(
            f"actor/body mismatch returned {mismatch.status_code}: {mismatch.text[:200]}"
        )

    print(
        json.dumps(
            {
                "version": "memory_v1_secondary_owner_probe_v1",
                "status": "pass",
                "actor": actor,
                "primary_owner": primary_owner,
                "actor_owner_mismatch_status": mismatch.status_code,
                "cases": reports,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
