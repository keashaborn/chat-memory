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


def exec_starts(path: Path) -> list[list[str]]:
    rows = [
        line.removeprefix("ExecStart=")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    if not rows:
        raise AssertionError(f"{path.name} has no ExecStart")
    return [shlex.split(row) for row in rows]


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
        commands = exec_starts(SYSTEMD / service)
        assert len(commands) == len(EXPECTED_OWNERS), service
        configured = [owner for command in commands for owner in owners(command)]
        assert len(configured) == len(EXPECTED_OWNERS), service
        assert set(configured) == EXPECTED_OWNERS, service
        assert all(command[-1] == "--apply" for command in commands), service
        assert all(len(owners(command)) == 1 for command in commands), service

    # External OpenAI extraction remains a separate admin-only circuit.
    external_commands = exec_starts(
        SYSTEMD / "memory-v1-v5-bounded-extraction.service"
    )
    assert len(external_commands) == 1
    external = owners(external_commands[0])
    assert external == ["1240822d-ac9a-4096-95aa-e2b24d36ef50"]

    print("memory_v1_v5_multi_owner_automation_contract: PASS")


if __name__ == "__main__":
    main()
