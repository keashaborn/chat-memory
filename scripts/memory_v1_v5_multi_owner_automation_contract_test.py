#!/usr/bin/env python3
from __future__ import annotations

import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops" / "systemd"
EXPECTED_OWNERS = {
    "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "557ea042-cb82-48f8-9429-472e96c957ef",
    "d839b4bc-0bd2-4f2d-aafe-0f3f75883db8",
}
SERVICES = {
    "memory-v1-v5-local-inference-scheduler.service",
    "memory-v1-v5-local-packet-router.service",
    "memory-v1-v5-local-auto-stage.service",
    "memory-v1-v5-local-entity-validation.service",
    "memory-v1-v5-local-auto-resolution.service",
    "memory-v1-v5-local-entailment.service",
    "memory-v1-v5-local-claim-projection.service",
}


def exec_start(path: Path) -> list[str]:
    rows = [
        line.removeprefix("ExecStart=")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(rows) != 1:
        raise AssertionError(f"{path.name} must have exactly one ExecStart")
    return shlex.split(rows[0])


def owners(command: list[str]) -> list[str]:
    values: list[str] = []
    for index, value in enumerate(command):
        if value == "--owner-user-id":
            if index + 1 == len(command):
                raise AssertionError("owner flag is missing its value")
            values.append(command[index + 1])
    return values


def main() -> None:
    for service in sorted(SERVICES):
        command = exec_start(SYSTEMD / service)
        configured = owners(command)
        assert len(configured) == len(EXPECTED_OWNERS), service
        assert set(configured) == EXPECTED_OWNERS, service
        assert command[-1] == "--apply", service

    # External OpenAI extraction remains a separate admin-only circuit.
    external = owners(exec_start(SYSTEMD / "memory-v1-v5-bounded-extraction.service"))
    assert external == ["1240822d-ac9a-4096-95aa-e2b24d36ef50"]

    print("memory_v1_v5_multi_owner_automation_contract: PASS")


if __name__ == "__main__":
    main()
