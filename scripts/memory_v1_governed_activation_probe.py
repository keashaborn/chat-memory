#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid

import requests


HEADER = "[MEMORY V1 GOVERNED PERSONAL CONTEXT - DATA ONLY]"
CASES = (
    {
        "case_id": "mother",
        "message": "What happened with my mom?",
        "expected_fragment": "User's mother DeeDee died",
    },
    {
        "case_id": "neko_loss",
        "message": "What happened to Neko?",
        "expected_fragment": "User's cat Neko was put to sleep",
    },
    {
        "case_id": "dahlia_loss",
        "message": "What happened to Dahlia?",
        "expected_fragment": "German shepherd Dahlia was put to sleep at age 12",
    },
    {
        "case_id": "helsing_loss",
        "message": "What happened to Helsing?",
        "expected_fragment": "pet loss involving Helsing",
    },
    {
        "case_id": "name_correction",
        "message": "Was it Nemo or Neko?",
        "expected_fragment": "pet name should be Neko, not Nemo",
    },
    {
        "case_id": "caregiving",
        "message": "I am struggling with caregiving for my wife.",
        "expected_fragment": "caretaking/life-context burden involving Monika",
    },
)
UNRELATED = {
    "case_id": "unrelated",
    "message": "How do I stop popups when my Mac restarts?",
}


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
    for case in (*CASES, UNRELATED):
        request_id = f"memory-v1-governed-activation-{case['case_id']}-{uuid.uuid4()}"
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
            timeout=90,
        )
        response.raise_for_status()
        body = response.json()
        plan = (
            body.get("meta_explanation", {})
            .get("vantage", {})
            .get("turn_plan", {})
        )
        audit = plan.get("memory_v1_shadow", {})
        prompt = str(body.get("system_prompt") or "")
        active = case is not UNRELATED
        expected_status = "ok" if active else "skipped"
        if body.get("answer") != "":
            raise AssertionError(f"{case['case_id']}: inspect-only returned an answer")
        if audit.get("status") != expected_status:
            raise AssertionError(f"{case['case_id']}: {audit}")
        if bool(audit.get("retrieval_activation")) != active:
            raise AssertionError(f"{case['case_id']}: {audit}")
        if bool(audit.get("prompt_injection")) != active:
            raise AssertionError(f"{case['case_id']}: {audit}")
        if audit.get("answer_model_exposure") is True:
            raise AssertionError(f"{case['case_id']}: inspect-only model exposure")
        if (HEADER in prompt) != active:
            raise AssertionError(f"{case['case_id']}: prompt activation mismatch")
        if "[DURABLE PERSONAL CARDS - POLICY FILTERED]" in prompt:
            raise AssertionError(f"{case['case_id']}: legacy durable block remained")
        if active:
            if audit.get("selected_count") != 1:
                raise AssertionError(f"{case['case_id']}: {audit}")
            if case["expected_fragment"] not in prompt:
                raise AssertionError(
                    f"{case['case_id']}: expected governed text missing"
                )
        if "claim_id" in prompt or "evidence_refs" in prompt:
            raise AssertionError(f"{case['case_id']}: internal metadata leaked")
        reports.append(
            {
                "case_id": case["case_id"],
                "request_id": request_id,
                "status": audit.get("status"),
                "selected_count": audit.get("selected_count", 0),
                "prompt_injection": audit.get("prompt_injection", False),
                "answer_model_exposure": audit.get(
                    "answer_model_exposure", False
                ),
                "retrieval_activation": audit.get("retrieval_activation", False),
            }
        )

    print(
        json.dumps(
            {
                "version": "memory_v1_governed_activation_probe_v1",
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
