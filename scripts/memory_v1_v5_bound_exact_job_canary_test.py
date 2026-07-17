#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
)
from scripts.memory_v1_v5_bound_exact_job_canary import (
    APPLY_ENABLE_TOKEN,
    claim_exact_target,
    plan_exact_target,
    read_pinned_context,
    validate_arguments,
)


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
JOB = uuid.UUID("788d0258-7227-46e2-8382-d13e6a122722")
BINDING = uuid.UUID("e8738381-c3be-4395-bfb2-75bfd42949e9")
PROJECT = uuid.UUID("08cd6a8a-5599-43d5-8d5c-b59401df8ccc")
THREAD = uuid.UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
HASH = "e" * 64


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, *, binding: uuid.UUID = BINDING) -> None:
        self.binding = binding
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self, *, readonly: bool = False) -> FakeTransaction:
        self.calls.append((f"transaction:{readonly}", ()))
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> None:
        if "set_config('app.user_id'" not in query:
            raise AssertionError("actor context was omitted")
        self.calls.append(("set_actor", args))

    async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
        self.calls.append((query, args))
        if "claim_owner_bound_evidence_job_v5" in query:
            if "claim_owner_evidence_extraction_job_v1" in query:
                raise AssertionError("runner used queue-scanning claim function")
            if args[:4] != (
                uuid.UUID("11111111-1111-4111-8111-111111111111"),
                JOB,
                HASH,
                BINDING,
            ):
                raise AssertionError("exact claim arguments changed")
            return {
                "job_id": JOB,
                "lease_token": uuid.UUID(
                    "22222222-2222-4222-8222-222222222222"
                ),
                "evidence_content_sha256": HASH,
                "status": "processing",
                "apply_outcome": "applied",
            }
        if "read_owner_evidence_extraction_context_v5" in query:
            return {
                "thread_id": THREAD,
                "project_id": PROJECT,
                "project_key": "verbal-sage",
                "binding_event_id": self.binding,
            }
        if "FROM memory.evidence_extraction_job AS job" in query:
            return {
                "job_id": JOB,
                "status": "pending",
                "attempts": 0,
                "route": "relational_extraction",
                "evidence_content_sha256": HASH,
                "binding_event_id": BINDING,
                "project_id": PROJECT,
                "project_key": "verbal-sage",
                "thread_id": THREAD,
            }
        raise AssertionError("unexpected fetchrow query")

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.calls.append((query, args))
        if "read_owner_project_components_v5" not in query:
            raise AssertionError("unexpected component query")
        if "WHERE component_key" in query:
            return [{"component_key": "memory-v1", "status": "active"}]
        return [
            {
                "component_id": uuid.UUID(
                    "aaaaaaaa-0013-4000-8000-000000000013"
                ),
                "component_key": "memory-v1",
                "display_name": "Memory V1",
                "parent_component_id": None,
                "aliases": ["memory-v1"],
            }
        ]


def args(**overrides: object) -> SimpleNamespace:
    values = {
        "owner_user_id": str(OWNER),
        "job_id": str(JOB),
        "expected_content_sha256": HASH,
        "binding_event_id": str(BINDING),
        "expected_project_key": "verbal-sage",
        "expected_component_key": "memory-v1",
        "run_id": "33333333-3333-4333-8333-333333333333",
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


async def async_checks() -> None:
    connection = FakeConnection()
    plan = await plan_exact_target(
        connection,  # type: ignore[arg-type]
        owner=OWNER,
        job_id=JOB,
        expected_content_sha256=HASH,
        binding_event_id=BINDING,
        expected_project_key="verbal-sage",
        expected_component_key="memory-v1",
    )
    if plan["status"] != "pending" or plan["attempts"] != 0:
        raise AssertionError("exact plan lost pristine target state")
    if str(OWNER) in str(plan) or str(JOB) in str(plan):
        raise AssertionError("exact plan leaked raw owner or job ids")

    claimed = await claim_exact_target(
        connection,  # type: ignore[arg-type]
        owner=OWNER,
        operation_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        job_id=JOB,
        expected_content_sha256=HASH,
        binding_event_id=BINDING,
        worker_id="exact-canary-test",
        lease_seconds=300,
    )
    if claimed["status"] != "processing":
        raise AssertionError("exact claim did not preserve returned state")

    context, binding = await read_pinned_context(
        connection,  # type: ignore[arg-type]
        owner=OWNER,
        job=claimed,
        worker_id="exact-canary-test",
        expected_binding_event_id=BINDING,
        expected_project_key="verbal-sage",
        expected_component_key="memory-v1",
    )
    if context["binding_event_id"] != BINDING:
        raise AssertionError("reviewed binding was not pinned")
    if binding.components[0].component_key != "memory-v1":
        raise AssertionError("reviewed component was not retained")

    try:
        await read_pinned_context(
            FakeConnection(
                binding=uuid.UUID("44444444-4444-4444-8444-444444444444")
            ),  # type: ignore[arg-type]
            owner=OWNER,
            job=claimed,
            worker_id="exact-canary-test",
            expected_binding_event_id=BINDING,
            expected_project_key="verbal-sage",
            expected_component_key="memory-v1",
        )
    except RuntimeError as exc:
        if "binding changed" not in str(exc):
            raise
    else:
        raise AssertionError("changed binding was accepted")


def main() -> int:
    validate_arguments(args())
    expect_error(
        lambda: validate_arguments(args(enable_external_call=True)),
        "dry-run external call capability",
    )
    expect_error(
        lambda: validate_arguments(
            args(apply=True, enable_external_call=True, model="gpt-test")
        ),
        "apply without capability",
    )
    old_apply = os.environ.get("MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY")
    old_calls = os.environ.get("MEMORY_V1_V5_EXTERNAL_CALLS")
    try:
        os.environ["MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY"] = APPLY_ENABLE_TOKEN
        os.environ["MEMORY_V1_V5_EXTERNAL_CALLS"] = EXTERNAL_CALL_ENABLE_TOKEN
        validate_arguments(
            args(apply=True, enable_external_call=True, model="gpt-test")
        )
    finally:
        if old_apply is None:
            os.environ.pop("MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY", None)
        else:
            os.environ["MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY"] = old_apply
        if old_calls is None:
            os.environ.pop("MEMORY_V1_V5_EXTERNAL_CALLS", None)
        else:
            os.environ["MEMORY_V1_V5_EXTERNAL_CALLS"] = old_calls

    source = Path(__file__).with_name(
        "memory_v1_v5_bound_exact_job_canary.py"
    ).read_text(encoding="utf-8")
    if "claim_owner_evidence_extraction_job_v1" in source:
        raise AssertionError("exact runner contains queue-scanning claim function")
    if "ORDER BY priority" in source or "LIMIT 1" in source:
        raise AssertionError("exact runner contains queue-selection logic")
    asyncio.run(async_checks())
    print("memory_v1_v5_bound_exact_job_canary_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
