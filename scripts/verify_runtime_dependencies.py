#!/usr/bin/env python3
from __future__ import annotations

"""Fail closed when the SeeBx runtime does not match its exact package contract."""

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "seebx-runtime-dependency-preflight-v1"
EXACT_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==(?P<version>[^;\s]+)$"
)
PYTHON_PIN = re.compile(r"^==(?P<major>\d+)\.(?P<minor>\d+)\.\*$")


class DependencyContractError(RuntimeError):
    pass


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def load_contract(path: Path) -> tuple[tuple[int, int], dict[str, str]]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DependencyContractError("pyproject_unreadable") from error

    project = document.get("project")
    if not isinstance(project, Mapping):
        raise DependencyContractError("project_contract_missing")

    python_raw = project.get("requires-python")
    python_match = PYTHON_PIN.fullmatch(str(python_raw or ""))
    if python_match is None:
        raise DependencyContractError("python_version_not_exactly_pinned")
    python_version = (
        int(python_match.group("major")),
        int(python_match.group("minor")),
    )

    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise DependencyContractError("dependency_contract_missing")

    pins: dict[str, str] = {}
    for dependency in dependencies:
        match = EXACT_PIN.fullmatch(str(dependency))
        if match is None:
            raise DependencyContractError("dependency_not_exactly_pinned")
        name = _canonical_name(match.group("name"))
        if name in pins:
            raise DependencyContractError("duplicate_dependency_pin")
        pins[name] = match.group("version")
    return python_version, dict(sorted(pins.items()))


def verify_contract(
    python_version: tuple[int, int],
    pins: Mapping[str, str],
    *,
    actual_python: tuple[int, int] | None = None,
    version_reader: Callable[[str], str] = importlib.metadata.version,
) -> dict[str, Any]:
    running_python = actual_python or (sys.version_info.major, sys.version_info.minor)
    issues: list[dict[str, str]] = []
    if running_python != python_version:
        issues.append(
            {
                "kind": "python_version_mismatch",
                "name": "python",
                "expected": ".".join(map(str, python_version)),
                "actual": ".".join(map(str, running_python)),
            }
        )

    verified_count = 0
    for name, expected in sorted(pins.items()):
        try:
            actual = version_reader(name)
        except importlib.metadata.PackageNotFoundError:
            issues.append(
                {
                    "kind": "dependency_missing",
                    "name": name,
                    "expected": expected,
                    "actual": "missing",
                }
            )
            continue
        if actual != expected:
            issues.append(
                {
                    "kind": "dependency_version_mismatch",
                    "name": name,
                    "expected": expected,
                    "actual": actual,
                }
            )
            continue
        verified_count += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not issues else "fail",
        "python": {
            "expected": ".".join(map(str, python_version)),
            "actual": ".".join(map(str, running_python)),
        },
        "dependencies": {
            "expected_count": len(pins),
            "verified_count": verified_count,
            "issues": issues,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "pyproject.toml",
    )
    arguments = parser.parse_args(argv)
    try:
        python_version, pins = load_contract(arguments.pyproject)
        result = verify_contract(python_version, pins)
    except DependencyContractError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "contract_error": str(error),
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
