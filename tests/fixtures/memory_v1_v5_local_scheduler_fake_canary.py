#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--expected-job-id", required=True)
    parser.add_argument("--expected-content-sha256", required=True)
    parser.add_argument("--selector-version", required=True)
    parser.add_argument("--contract-profile", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--lease-seconds", required=True)
    parser.add_argument("--max-attempts", required=True)
    parser.add_argument("--timeout-seconds", required=True)
    parser.add_argument("--max-output-tokens", required=True)
    parser.add_argument("--rolling-window-seconds", required=True)
    parser.add_argument("--max-reserved-jobs", required=True)
    parser.add_argument("--failure-threshold", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        raise RuntimeError("fake canary requires apply")
    if len(os.environ.get("MEMORY_V1_LOCAL_INFERENCE_API_KEY", "")) < 32:
        raise RuntimeError("fake canary credential missing")
    outcome = os.getenv("FAKE_LOCAL_CANARY_OUTCOME", "accepted")
    if outcome == "invalid":
        print("invalid envelope")
        return 2
    payload = {
        "contract_version": "memory_v1_v5_local_inference_canary_v1",
        "apply": True,
        "outcome": outcome,
        "external_model_calls": 0,
        "local_model_calls": int(outcome in {"accepted", "rejected"}),
        "zero_write_replay_proved": True,
        "source_text": "synthetic field that the scheduler must remove",
    }
    if outcome == "rejected":
        payload["rejection_code"] = "local_validation_rejected"
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 1 if outcome == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
