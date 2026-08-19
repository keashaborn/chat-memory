from __future__ import annotations

import importlib.metadata
import tempfile
import unittest
from pathlib import Path

from scripts.verify_runtime_dependencies import (
    DependencyContractError,
    load_contract,
    verify_contract,
)


class RuntimeDependencyPreflightTests(unittest.TestCase):
    def _contract(self, dependencies: list[str]) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".toml",
            delete=False,
        )
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        temporary.write(
            "[project]\n"
            'requires-python = "==3.12.*"\n'
            "dependencies = [\n"
        )
        for dependency in dependencies:
            temporary.write(f'  "{dependency}",\n')
        temporary.write("]\n")
        temporary.close()
        return Path(temporary.name)

    def test_repository_runtime_contract_is_exact(self) -> None:
        python_version, pins = load_contract(Path("pyproject.toml"))
        self.assertEqual(python_version, (3, 12))
        self.assertEqual(len(pins), 12)
        self.assertEqual(pins["jsonschema"], "4.25.1")
        self.assertEqual(pins["requests"], "2.32.3")
        self.assertEqual(pins["zep-cloud"], "3.25.0")

    def test_exact_contract_passes_only_when_every_version_matches(self) -> None:
        python_version, pins = load_contract(
            self._contract(["FastAPI==0.120.4", "uvicorn[standard]==0.38.0"])
        )
        installed = {"fastapi": "0.120.4", "uvicorn": "0.38.0"}
        result = verify_contract(
            python_version,
            pins,
            actual_python=(3, 12),
            version_reader=installed.__getitem__,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["dependencies"]["verified_count"], 2)
        self.assertEqual(result["dependencies"]["issues"], [])

    def test_missing_and_mismatched_dependencies_fail_closed(self) -> None:
        python_version, pins = load_contract(
            self._contract(["fastapi==0.120.4", "jsonschema==4.25.1"])
        )

        def installed(name: str) -> str:
            if name == "jsonschema":
                raise importlib.metadata.PackageNotFoundError(name)
            return "0.119.0"

        result = verify_contract(
            python_version,
            pins,
            actual_python=(3, 11),
            version_reader=installed,
        )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(
            [issue["kind"] for issue in result["dependencies"]["issues"]],
            [
                "python_version_mismatch",
                "dependency_version_mismatch",
                "dependency_missing",
            ],
        )

    def test_non_exact_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            DependencyContractError,
            "dependency_not_exactly_pinned",
        ):
            load_contract(self._contract(["fastapi>=0.120.4"]))

    def test_duplicate_normalized_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            DependencyContractError,
            "duplicate_dependency_pin",
        ):
            load_contract(
                self._contract(["zep-cloud==3.25.0", "zep_cloud==3.25.0"])
            )
