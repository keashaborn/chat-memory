#!/usr/bin/env python3
from __future__ import annotations

import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops" / "systemd"
ADMIN_OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
LEGACY_PIPELINE_OWNERS = {
    ADMIN_OWNER,
    "557ea042-cb82-48f8-9429-472e96c957ef",
    "d839b4bc-0bd2-4f2d-aafe-0f3f75883db8",
}
LEGACY_SERVICES = {
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


def values(command: list[str], flag: str) -> list[str]:
    found: list[str] = []
    for index, value in enumerate(command):
        if value == flag:
            if index + 1 == len(command):
                raise AssertionError(f"{flag} is missing its value")
            found.append(command[index + 1])
    return found


def main() -> None:
    scheduler = exec_starts(
        SYSTEMD / "memory-v1-v5-local-inference-scheduler.service"
    )
    assert len(scheduler) == 1
    assert values(scheduler[0], "--owner-user-id") == [ADMIN_OWNER]
    assert values(scheduler[0], "--contract-profile") == ["v5_2"]
    assert scheduler[0][-1] == "--apply"

    for service in sorted(LEGACY_SERVICES):
        commands = exec_starts(SYSTEMD / service)
        assert len(commands) == len(LEGACY_PIPELINE_OWNERS), service
        configured = [
            owner
            for command in commands
            for owner in values(command, "--owner-user-id")
        ]
        assert len(configured) == len(LEGACY_PIPELINE_OWNERS), service
        assert set(configured) == LEGACY_PIPELINE_OWNERS, service
        assert all(command[-1] == "--apply" for command in commands), service
        assert all(
            len(values(command, "--owner-user-id")) == 1
            for command in commands
        ), service

    external_commands = exec_starts(
        SYSTEMD / "memory-v1-v5-bounded-extraction.service"
    )
    assert len(external_commands) == 1
    assert values(external_commands[0], "--owner-user-id") == [ADMIN_OWNER]

    print("memory_v1_v5_multi_owner_automation_contract: PASS")


if __name__ == "__main__":
    main()
