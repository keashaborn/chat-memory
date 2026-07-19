#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.memory_v1_v5_local_inference_scheduler import (
    APPLY_ENABLE_TOKEN,
    CANARY_CONTRACT,
    canary_command,
    canonical_owners,
    loopback_dsn,
    ordered_owner_targets,
    sanitized_canary_result,
    select_owner_target,
    validate_arguments,
)


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "run_id": str(uuid.uuid4()),
        "selector_version": None,
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
    assert loopback_dsn("postgresql://user@127.0.0.1:5432/memory") == (
        "postgresql://user@127.0.0.1:5432/memory"
    )
    for invalid_dsn in (
        "postgresql://user@10.0.0.1:5432/memory",
        "https://127.0.0.1/memory",
    ):
        try:
            loopback_dsn(invalid_dsn)
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-loopback database DSN was accepted")
    try:
        canonical_owners([])
    except RuntimeError:
        pass
    else:
        raise AssertionError("empty owner allowlist was accepted")

    owner_b = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")
    owner_c = uuid.UUID("d839b4bc-0bd2-4f2d-aafe-0f3f75883db8")
    target_a = {"job_id": uuid.uuid4()}
    target_b = {"job_id": uuid.uuid4()}
    target_c = {"job_id": uuid.uuid4()}
    now = datetime.now(timezone.utc)
    selected = select_owner_target(
        [
            (OWNER, {}, target_a, now),
            (owner_c, {}, target_c, None),
            (owner_b, {}, target_b, None),
        ]
    )
    assert selected == (owner_b, target_b)
    assert ordered_owner_targets(
        [
            (OWNER, {}, target_a, now),
            (owner_c, {}, target_c, None),
            (owner_b, {}, target_b, None),
        ]
    ) == [(owner_b, target_b), (owner_c, target_c), (OWNER, target_a)]
    selected = select_owner_target(
        [
            (OWNER, {}, target_a, now),
            (owner_b, {}, target_b, datetime(2026, 1, 1, tzinfo=timezone.utc)),
            (owner_c, {}, target_c, datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]
    )
    assert selected == (owner_b, target_b)
    assert select_owner_target([(OWNER, {}, None, None)]) is None

    validate_arguments(args())
    validate_arguments(args(selector_version="20260719_v5_legacy_claim_reintake_v1"))
    for invalid_selector in ("", "UPPER", "contains space", "../escape"):
        try:
            validate_arguments(args(selector_version=invalid_selector))
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid selector-version was accepted")
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
