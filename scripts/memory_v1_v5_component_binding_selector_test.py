#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import uuid

import memory_v1_v5_component_binding_selector as selector


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
PROJECT = "08cd6a8a-5599-43d5-8d5c-b59401df8ccc"
THREAD = "1d9d4583-c447-4d60-9daa-e508fe2c7426"


def snapshot() -> dict:
    return {
        "registry_rows": 1,
        "registry": {
            "project_id": PROJECT,
            "project_key": "verbal-sage",
            "component_id": "32812672-ff8c-4f84-af7e-1cf64b65dbe5",
            "component_key": "memory-v1",
            "display_name": "Memory V1",
            "aliases": ["memory-v1"],
        },
        "summary": {
            "pending_jobs": 7,
            "pending_threads": 1,
            "matched_evidence": 1,
            "candidate_threads": 1,
        },
        "candidates": [
            {
                "thread_id": THREAD,
                "thread_sha256": selector.sha256_text(THREAD),
                "pending_record_count": 7,
                "matched_record_count": 1,
                "matched_evidence_sha256": ["a" * 64],
                "current_project_id": None,
                "current_project_key": None,
            }
        ],
    }


def build(value: dict, expected: int = 1) -> dict:
    return selector.build_report(
        owner=OWNER,
        project_key="verbal-sage",
        component_key="memory-v1",
        expected_candidates=expected,
        snapshot=value,
    )


def expect_error(value: dict, expected: int = 1) -> None:
    try:
        build(value, expected)
    except selector.SelectorError:
        return
    raise AssertionError("unsafe selector input was accepted")


def main() -> int:
    first = build(snapshot())
    second = build(snapshot())
    action = first["actions"][0]
    assert action["action"] == "propose_bind"
    assert action["reason_code"] == "explicit_registered_component_name"
    assert action["operation_id"] == second["actions"][0]["operation_id"]
    assert first["controls"]["database_writes"] == 0
    assert first["controls"]["raw_content_in_report"] is False
    serialized = json.dumps(first, sort_keys=True)
    assert '"content"' not in serialized
    assert '"text"' not in serialized

    already = snapshot()
    already["candidates"][0]["current_project_id"] = PROJECT
    assert build(already)["actions"][0]["action"] == "already_bound_same_project"

    conflict = snapshot()
    conflict["candidates"][0]["current_project_id"] = (
        "99999999-9999-4999-8999-999999999999"
    )
    expect_error(conflict)

    changed = snapshot()
    changed["candidates"].append(copy.deepcopy(changed["candidates"][0]))
    changed["candidates"][1]["thread_id"] = (
        "2d9d4583-c447-4d60-9daa-e508fe2c7426"
    )
    expect_error(changed, expected=1)

    invalid_hash = snapshot()
    invalid_hash["candidates"][0]["matched_evidence_sha256"] = ["raw text"]
    expect_error(invalid_hash)

    print("memory_v1_v5_component_binding_selector_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
