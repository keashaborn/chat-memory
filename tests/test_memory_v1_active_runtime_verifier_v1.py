from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_active_runtime_verifier_v1 import (
    BINDING_CONTRACT,
    RuntimeVerificationError,
    decode_json,
    sha256_bytes,
    verify_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops/systemd/memory-v1-active-runtime-manifest-v1.json"


class ActiveRuntimeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.manifest_sha256 = sha256_bytes(MANIFEST.read_bytes())
        source_hashes: dict[str, str] = {}
        units: dict[str, dict[str, str]] = {}
        sentinels: dict[str, str] = {}
        for service in self.manifest["services"]:
            source_hashes[service["unit"]] = service["source_sha256"]
            source_hashes[service["timer"]] = service["timer_source_sha256"]
            units[service["unit"]] = {
                "active_state": "inactive",
                "enabled_state": "not-found",
                "fragment_path": "",
                "load_state": "not-found",
            }
            units[service["timer"]] = {
                "active_state": "inactive",
                "enabled_state": "not-found",
                "fragment_path": "",
                "load_state": "not-found",
            }
            sentinels[service["timer_enable_sentinel"]] = "absent"
        legacy = {
            item["component"]: {
                "active_state": item["required_activation_state"],
                "enabled_state": item.get("required_enabled_state", "disabled"),
                "fragment_path": f"/etc/systemd/system/{item['component']}",
                "load_state": "loaded",
            }
            for item in self.manifest["legacy_exclusivity"]
            if "required_activation_state" in item
        }
        self.snapshot = {
            "config": {"state": "absent"},
            "installed_unit_sha256": {},
            "legacy_units": legacy,
            "openai_sdk_version": self.manifest["verification"]["openai_sdk_version"],
            "python_executable_sha256": "a" * 64,
            "repository": {
                "commit": "b" * 40,
                "tree": "c" * 40,
                "tracked_clean": True,
            },
            "sentinels": sentinels,
            "source_unit_sha256": source_hashes,
            "units": units,
        }

    def verify(self, snapshot: dict, *, phase: str, binding: dict | None = None) -> dict:
        return verify_snapshot(
            self.manifest,
            snapshot,
            manifest_sha256=self.manifest_sha256,
            phase=phase,
            binding=binding,
        )

    def test_exact_preactivation_snapshot_passes_without_binding(self) -> None:
        report = self.verify(self.snapshot, phase="preactivation")
        self.assertTrue(report["runtime_matches_manifest"])
        self.assertTrue(report["legacy_exclusivity_verified"])
        self.assertEqual(report["provider_calls"], 0)
        self.assertIsNone(report["binding_sha256"])

    def test_preactivation_fails_closed_on_unit_sentinel_config_or_legacy_drift(self) -> None:
        cases: list[dict] = []

        active = copy.deepcopy(self.snapshot)
        first_unit = self.manifest["services"][0]["unit"]
        active["units"][first_unit]["active_state"] = "active"
        cases.append(active)

        sentinel = copy.deepcopy(self.snapshot)
        first_sentinel = self.manifest["services"][0]["timer_enable_sentinel"]
        sentinel["sentinels"][first_sentinel] = "present"
        cases.append(sentinel)

        config = copy.deepcopy(self.snapshot)
        config["config"] = {
            "sha256": "d" * 64,
            "state": "root_owned_0600_regular_single_link",
        }
        cases.append(config)

        legacy = copy.deepcopy(self.snapshot)
        first_legacy = next(iter(legacy["legacy_units"]))
        legacy["legacy_units"][first_legacy]["active_state"] = "active"
        cases.append(legacy)

        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(RuntimeVerificationError):
                    self.verify(changed, phase="preactivation")

    def installed_snapshot_and_binding(self) -> tuple[dict, dict]:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["config"] = {
            "sha256": "d" * 64,
            "state": "root_owned_0600_regular_single_link",
            "size": 512,
        }
        for state in snapshot["units"].values():
            state.update(
                {
                    "active_state": "inactive",
                    "enabled_state": "disabled",
                    "fragment_path": "/etc/systemd/system/example",
                    "load_state": "loaded",
                }
            )
        snapshot["installed_unit_sha256"] = copy.deepcopy(
            snapshot["source_unit_sha256"]
        )
        binding = {
            "contract_version": BINDING_CONTRACT,
            "manifest_sha256": self.manifest_sha256,
            "openai_sdk_version": snapshot["openai_sdk_version"],
            "phase": "installed_inactive",
            "python_executable_sha256": snapshot["python_executable_sha256"],
            "repository_commit": snapshot["repository"]["commit"],
            "repository_tree": snapshot["repository"]["tree"],
            "runtime_config_sha256": snapshot["config"]["sha256"],
            "unit_sha256": snapshot["installed_unit_sha256"],
        }
        return snapshot, binding

    def test_exact_installed_inactive_snapshot_and_binding_pass(self) -> None:
        snapshot, binding = self.installed_snapshot_and_binding()
        report = self.verify(
            snapshot, phase="installed_inactive", binding=binding
        )
        self.assertTrue(report["runtime_matches_manifest"])
        self.assertIsNotNone(report["binding_sha256"])

    def test_installed_runtime_rejects_binding_or_installed_unit_drift(self) -> None:
        snapshot, binding = self.installed_snapshot_and_binding()

        wrong_commit = copy.deepcopy(binding)
        wrong_commit["repository_commit"] = "e" * 40
        with self.assertRaises(RuntimeVerificationError):
            self.verify(
                snapshot, phase="installed_inactive", binding=wrong_commit
            )

        wrong_unit = copy.deepcopy(snapshot)
        first_unit = next(iter(wrong_unit["installed_unit_sha256"]))
        wrong_unit["installed_unit_sha256"][first_unit] = "f" * 64
        with self.assertRaises(RuntimeVerificationError):
            self.verify(wrong_unit, phase="installed_inactive", binding=binding)

        with self.assertRaises(RuntimeVerificationError):
            self.verify(snapshot, phase="installed_inactive", binding=None)

    def test_duplicate_json_and_symlink_inputs_are_rejected(self) -> None:
        with self.assertRaises(RuntimeVerificationError):
            decode_json(b'{"a":1,"a":2}', label="fixture")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            from scripts.memory_v1_active_runtime_verifier_v1 import read_regular

            with self.assertRaises(RuntimeVerificationError):
                read_regular(link, label="fixture")


if __name__ == "__main__":
    unittest.main()
