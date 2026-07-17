#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    ProviderAdapterError,
)
from scripts.memory_v1_v5_bounded_extraction_worker import (
    APPLY_ENABLE_TOKEN,
    canonical_owners,
    read_context,
    rejection_code,
    sha256_text,
    stable_json,
    validate_arguments,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.component_read = False

    def transaction(self, *, readonly: bool = False) -> FakeTransaction:
        if not readonly:
            raise AssertionError("context read must use a read-only transaction")
        return FakeTransaction()

    async def execute(self, query: str, *_args: object) -> None:
        if "set_config('app.user_id'" not in query:
            raise AssertionError("context read omitted owner actor context")

    async def fetchrow(self, query: str, *_args: object) -> dict[str, object]:
        if "read_owner_evidence_extraction_context_v5" not in query:
            raise AssertionError("unexpected context query")
        return {
            "project_id": "aaaaaaaa-0011-4000-8000-000000000011",
            "project_key": "verbal-sage",
            "binding_event_id": "aaaaaaaa-0012-4000-8000-000000000012",
        }

    async def fetch(self, query: str, *_args: object) -> list[dict[str, object]]:
        if "read_owner_project_components_v5" not in query:
            raise AssertionError("unexpected component query")
        self.component_read = True
        return [
            {
                "component_id": "aaaaaaaa-0013-4000-8000-000000000013",
                "component_key": "memory-v1",
                "display_name": "Memory V1",
                "parent_component_id": None,
                "aliases": ["memory", "memory-system", "memory-v1"],
            }
        ]


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

    fake_connection = FakeConnection()
    context = asyncio.run(
        read_context(
            fake_connection,  # type: ignore[arg-type]
            owner=owners[0],
            job={
                "job_id": "aaaaaaaa-0020-4000-8000-000000000020",
                "lease_token": "aaaaaaaa-0021-4000-8000-000000000021",
                "evidence_content_sha256": "a" * 64,
            },
            worker_id="component-scope-test",
        )
    )
    if not fake_connection.component_read:
        raise AssertionError("bound context did not read trusted components")
    if context["project_components"][0]["component_key"] != "memory-v1":
        raise AssertionError("trusted component rows were not retained")

    print("memory_v1_v5_bounded_extraction_worker_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
