#!/usr/bin/env python3
from __future__ import annotations

import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops" / "systemd"
ADMIN_OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
AUTHENTICATED_OWNER_SERVICES = {
    "memory-v1-evidence-intake-dispatcher.service",
    "memory-v1-v5-chat-capture.service",
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
    assert values(scheduler[0], "--owner-user-id") == []
    assert values(scheduler[0], "--contract-profile") == ["v5_2"]
    assert scheduler[0][-1] == "--apply"

    for service in sorted(AUTHENTICATED_OWNER_SERVICES):
        commands = exec_starts(SYSTEMD / service)
        assert len(commands) == 1, service
        assert values(commands[0], "--owner-user-id") == [], service
        assert all("--apply" in command for command in commands), service

    activation = (
        SYSTEMD / "brains-memory-v1-universal-authenticated.conf"
    ).read_text(encoding="utf-8")
    for name in (
        "MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED",
        "MEMORY_V1_GOVERNED_EXPLICIT_HIGH_ALL_AUTHENTICATED",
        "MEMORY_V1_SPECIALIZED_ACTIVE_ALL_AUTHENTICATED",
        "MEMORY_V1_V5_PROJECT_SHADOW_ALL_AUTHENTICATED",
    ):
        assert f'Environment="{name}=1"' in activation

    external_commands = exec_starts(
        SYSTEMD / "memory-v1-v5-bounded-extraction.service"
    )
    assert len(external_commands) == 1
    assert values(external_commands[0], "--owner-user-id") == [ADMIN_OWNER]

    print("memory_v1_v5_multi_owner_automation_contract: PASS")


if __name__ == "__main__":
    main()
