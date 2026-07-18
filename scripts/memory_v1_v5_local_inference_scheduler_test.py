#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path

from scripts.memory_v1_v5_local_inference_scheduler import (
    APPLY_ENABLE_TOKEN,
    CANARY_CONTRACT,
    canary_command,
    canonical_owners,
    sanitized_canary_result,
    validate_arguments,
)


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "run_id": str(uuid.uuid4()),
        "max_jobs": 1,
        "max_attempts": 1,
        "lease_seconds": 900,
        "timeout_seconds": 600.0,
        "max_output_tokens": 4096,
        "rolling_window_seconds": 86400,
        "max_reserved_jobs": 12,
        "failure_threshold": 3,
        "apply": False,
        "canary": str(Path("scripts/memory_v1_v5_local_inference_canary.py")),
        "endpoint": "http://127.0.0.1:18080/v1/chat/completions",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def completed(payload: dict, returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["canary"],
        returncode,
        stdout=json.dumps(payload, sort_keys=True) + "\n",
        stderr="",
    )


def main() -> int:
    assert canonical_owners([str(OWNER), str(OWNER)]) == [OWNER]
    try:
        canonical_owners([])
    except RuntimeError:
        pass
    else:
        raise AssertionError("empty owner allowlist was accepted")

    validate_arguments(args())
    original = os.environ.get("MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY")
    try:
        os.environ.pop("MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY", None)
        try:
            validate_arguments(args(apply=True))
        except RuntimeError:
            pass
        else:
            raise AssertionError("apply was accepted without the capability token")
        os.environ["MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY"] = APPLY_ENABLE_TOKEN
        validate_arguments(args(apply=True))
    finally:
        if original is None:
            os.environ.pop("MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY", None)
        else:
            os.environ["MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY"] = original

    target = {
        "job_id": uuid.uuid4(),
        "evidence_id": uuid.uuid4(),
        "evidence_content_sha256": "a" * 64,
        "selector_version": "20260718_local_v1",
    }
    command = canary_command(args(), owner=OWNER, run_id=uuid.uuid4(), target=target)
    assert command.count("--apply") == 1
    assert command[command.index("--max-reserved-jobs") + 1] == "12"
    assert command[command.index("--failure-threshold") + 1] == "3"

    accepted = sanitized_canary_result(
        completed(
            {
                "contract_version": CANARY_CONTRACT,
                "apply": True,
                "outcome": "accepted",
                "job_id_sha256": "b" * 64,
                "local_model_calls": 1,
                "external_model_calls": 0,
                "source_text": "must not survive",
            },
            0,
        )
    )
    assert accepted["outcome"] == "accepted"
    assert "source_text" not in accepted

    rejected = sanitized_canary_result(
        completed(
            {
                "contract_version": CANARY_CONTRACT,
                "apply": True,
                "outcome": "rejected",
                "rejection_code": "local_validation_rejected",
                "local_model_calls": 1,
                "external_model_calls": 0,
            },
            1,
        )
    )
    assert rejected["outcome"] == "rejected"

    for payload, returncode in (
        ({"contract_version": "wrong", "outcome": "accepted"}, 0),
        ({"contract_version": CANARY_CONTRACT, "outcome": "accepted"}, 1),
    ):
        try:
            sanitized_canary_result(completed(payload, returncode))
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid canary result was accepted")

    print("memory_v1_v5_local_inference_scheduler_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
