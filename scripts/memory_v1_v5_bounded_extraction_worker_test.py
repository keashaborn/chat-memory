#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    ProviderAdapterError,
)
from scripts.memory_v1_v5_bounded_extraction_worker import (
    APPLY_ENABLE_TOKEN,
    canonical_owners,
    rejection_code,
    sha256_text,
    stable_json,
    validate_arguments,
)


def args(**overrides: object) -> SimpleNamespace:
    values = {
        "run_id": "6d7a2231-a327-46bb-9863-fd1125e74e27",
        "max_jobs": 1,
        "max_attempts": 1,
        "lease_seconds": 300,
        "timeout_seconds": 120.0,
        "max_output_tokens": 16000,
        "enable_external_call": False,
        "apply": False,
        "model": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def expect_error(callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


def main() -> int:
    validate_arguments(args())
    expect_error(
        lambda: validate_arguments(args(max_jobs=11)),
        "unbounded job count",
    )
    expect_error(
        lambda: validate_arguments(args(enable_external_call=True)),
        "dry-run external call capability",
    )
    expect_error(
        lambda: validate_arguments(args(apply=True, model="test-model")),
        "apply without explicit call flag",
    )

    old_apply = os.environ.get("MEMORY_V1_V5_BOUNDED_EXTRACTION_APPLY")
    old_calls = os.environ.get("MEMORY_V1_V5_EXTERNAL_CALLS")
    try:
        os.environ["MEMORY_V1_V5_BOUNDED_EXTRACTION_APPLY"] = (
            APPLY_ENABLE_TOKEN
        )
        os.environ["MEMORY_V1_V5_EXTERNAL_CALLS"] = EXTERNAL_CALL_ENABLE_TOKEN
        validate_arguments(
            args(apply=True,enable_external_call=True,model="test-model")
        )
    finally:
        if old_apply is None:
            os.environ.pop("MEMORY_V1_V5_BOUNDED_EXTRACTION_APPLY", None)
        else:
            os.environ["MEMORY_V1_V5_BOUNDED_EXTRACTION_APPLY"] = old_apply
        if old_calls is None:
            os.environ.pop("MEMORY_V1_V5_EXTERNAL_CALLS", None)
        else:
            os.environ["MEMORY_V1_V5_EXTERNAL_CALLS"] = old_calls

    owners = canonical_owners(
        [
            "fa111111-1111-4111-8111-111111111111",
            "fa111111-1111-4111-8111-111111111111",
            "fb222222-2222-4222-8222-222222222222",
        ]
    )
    if len(owners) != 2 or owners != sorted(owners, key=str):
        raise AssertionError("owner allowlist canonicalization changed")
    if rejection_code(
        ProviderAdapterError("model_refusal",retryable=False)
    ) != "model_refusal":
        raise AssertionError("provider rejection code was not preserved")
    if rejection_code(ValueError("sensitive raw error")) != (
        "validator_rejected"
    ):
        raise AssertionError("validator error was not sanitized")

    report = stable_json(
        {
            "owner_sha256": sha256_text(str(owners[0])),
            "route": "relational_extraction",
            "external_model_calls": 0,
        }
    )
    if str(owners[0]) in report or "source_content" in report:
        raise AssertionError("sanitized report retained a raw owner/source field")

    print("memory_v1_v5_bounded_extraction_worker_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
