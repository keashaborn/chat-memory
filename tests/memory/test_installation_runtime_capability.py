from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governed_memory_install import authority
from tools.governed_memory_install.controller_runtime import (
    ControllerRuntimeCapabilityError,
    LAUNCHER_RELATIVE_PATH,
    PACKAGE_MANIFEST_RELATIVE_PATH,
    REQUIREMENTS_LOCK_RELATIVE_PATH,
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
    _ObservedRuntime,
    _ProcessProbeResult,
    _observe_release_tree,
    _requirements_from_lock,
    _verify_launcher_module_closure,
    verified_controller_runtime_evidence,
    verify_controller_runtime_capability,
)
from tools.governed_memory_install.controller import STORES_ONLY_PLAN
from tools.governed_memory_install.package_capability import (
    _VerifiedPackageCapability,
    _PACKAGE_TOKEN,
    VerifiedPackageEvidence,
)


ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


class RuntimeCapabilityTests(unittest.TestCase):
    def _runtime_package_artifacts(
        self, *, lock_bytes: bytes | None = None
    ) -> dict[str, bytes]:
        package_root = ROOT / "tools/governed_memory_install"
        artifacts = {
            path.relative_to(ROOT).as_posix(): path.read_bytes()
            for path in package_root.glob("*.py")
        }
        lock_path = str(REQUIREMENTS_LOCK_RELATIVE_PATH)
        artifacts[lock_path] = (
            (ROOT / lock_path).read_bytes()
            if lock_bytes is None
            else lock_bytes
        )
        return artifacts

    def _fixture(
        self,
        *,
        distributions: dict[str, str] | None = None,
        lock_bytes: bytes | None = None,
    ):
        package_sha = "a" * 64
        runtime_contract_sha = "b" * 64
        artifacts = self._runtime_package_artifacts(lock_bytes=lock_bytes)
        lock_sha = hashlib.sha256(
            artifacts[str(REQUIREMENTS_LOCK_RELATIVE_PATH)]
        ).hexdigest()
        launcher_sha = hashlib.sha256(
            artifacts[str(LAUNCHER_RELATIVE_PATH)]
        ).hexdigest()
        path_facts = {
            "import_paths": [
                "lib/python3.12",
                "lib/python3.12/site-packages",
            ],
            "prefixes": {
                "base_exec_prefix": ".",
                "base_prefix": ".",
                "exec_prefix": ".",
                "prefix": ".",
            },
            "site_package_paths": ["lib/python3.12/site-packages"],
            "stdlib_paths": {
                "platstdlib": "lib/python3.12",
                "stdlib": "lib/python3.12",
            },
        }
        path_facts_sha = hashlib.sha256(_canonical(path_facts)).hexdigest()
        inventory = _canonical(
            {
                "distributions": (
                    {
                        "cffi": "2.1.0",
                        "cryptography": "49.0.0",
                        "pycparser": "3.0",
                    }
                    if distributions is None
                    else distributions
                ),
                "interpreter_path_facts": path_facts,
                "interpreter_path_facts_sha256": path_facts_sha,
                "pip_present": False,
                "platform_architecture": "x86_64",
                "platform_os": "linux",
                "python_implementation": "CPython",
                "python_version": "3.12.3",
                "setuptools_present": False,
                "system_site_packages_enabled": False,
                "user_site_enabled": False,
                "wheel_present": False,
            }
        ) + b"\n"
        observed = _ObservedRuntime(
            "e" * 64,
            "d" * 64,
            "f" * 64,
            hashlib.sha256(inventory).hexdigest(),
            inventory,
            launcher_sha,
        )
        process = _ProcessProbeResult(inventory, "1" * 64)
        receipt = {
            "schema_version": RUNTIME_RECEIPT_SCHEMA,
            "result": RUNTIME_RECEIPT_RESULT,
            "package_manifest_sha256": package_sha,
            "controller_runtime_contract_sha256": runtime_contract_sha,
            "controller_requirements_lock_sha256": lock_sha,
            "python_implementation": "CPython",
            "python_version": "3.12.3",
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "runtime_tree_sha256": observed.runtime_tree_sha256,
            "release_tree_sha256": observed.release_tree_sha256,
            "interpreter_sha256": observed.interpreter_sha256,
            "installed_distribution_inventory_sha256": observed.inventory_sha256,
            "interpreter_path_facts_sha256": path_facts_sha,
            "supervisor_launcher_sha256": launcher_sha,
            "launcher_help_probe_sha256": process.launcher_help_probe_sha256,
            "pip_present": False,
            "setuptools_present": False,
            "wheel_present": False,
            "user_site_enabled": False,
            "system_site_packages_enabled": False,
            "network_calls": 0,
            "provider_calls": 0,
            "production_data_read": False,
            "production_state_changed": False,
            "persistent_resources_created": False,
        }
        raw = _canonical(receipt)
        receipt_sha = hashlib.sha256(raw).hexdigest()
        evidence = VerifiedPackageEvidence(
            result_type="verified_dormant_install_package_v1",
            authorization_sha256="2" * 64,
            scope_sha256="3" * 64,
            candidate_git_commit="4" * 40,
            candidate_git_tree="5" * 40,
            package_manifest_sha256=package_sha,
            controller_contract_sha256="6" * 64,
            execution_plan_sha256="7" * 64,
            exact_targets_sha256="8" * 64,
            controller_runtime_receipt_sha256=receipt_sha,
            store_spec_sha256="9" * 64,
            resource_identity_implementation_sha256="0" * 64,
            controller_runtime_contract_sha256=runtime_contract_sha,
            controller_requirements_lock_sha256=lock_sha,
            supervisor_launcher_sha256=launcher_sha,
            controller_model_sha256="a" * 64,
            artifact_count=len(artifacts),
            aggregate_artifact_sha256="b" * 64,
        )
        capability = _VerifiedPackageCapability(
            evidence, {}, artifacts, _PACKAGE_TOKEN
        )
        return capability, raw, receipt, observed, process

    def _write_release(
        self,
        root: Path,
        artifacts: dict[str, bytes],
        *,
        manifest_artifacts: dict[str, str] | None = None,
    ) -> str:
        digests = {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in artifacts.items()
        }
        manifest = {
            "schema_version": "synthetic-package-manifest-v1",
            "state": "synthetic-inactive",
            "artifacts": (
                digests if manifest_artifacts is None else manifest_artifacts
            ),
        }
        manifest_raw = json.dumps(manifest, indent=2).encode("ascii") + b"\n"
        for relative, raw in {
            **artifacts,
            str(PACKAGE_MANIFEST_RELATIVE_PATH): manifest_raw,
        }.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o444)
        return hashlib.sha256(manifest_raw).hexdigest()

    def _seal_release_directories(self, root: Path) -> None:
        directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            directory.chmod(0o555)

    def _unseal_release_directories(self, root: Path) -> None:
        directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
        for directory in sorted(directories, key=lambda path: len(path.parts)):
            directory.chmod(0o755)

    def test_canonical_receipt_and_secure_probe_results_mint_opaque_capability(self):
        capability, raw, unused_receipt, observed, process = self._fixture()
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem",
            return_value=observed,
        ), patch(
            "tools.governed_memory_install.controller_runtime._run_process_probes",
            return_value=process,
        ):
            runtime = verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=raw
            )
        evidence = verified_controller_runtime_evidence(runtime)
        self.assertEqual(
            evidence.controller_runtime_receipt_sha256,
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            evidence.interpreter_path,
            evidence.runtime_root + "/bin/python",
        )
        self.assertEqual(
            evidence.package_manifest_path,
            evidence.release_root
            + "/ops/governed_memory/installation/current/package_manifest.json",
        )
        self.assertEqual(
            evidence.release_tree_sha256,
            unused_receipt["release_tree_sha256"],
        )
        self.assertIn(
            evidence.package_manifest_sha256,
            evidence.supervisor_launcher_path,
        )
        self.assertEqual(
            evidence.interpreter_path_facts_sha256,
            receipt_path_facts_sha := json.loads(raw)[
                "interpreter_path_facts_sha256"
            ],
        )
        self.assertRegex(receipt_path_facts_sha, r"^[0-9a-f]{64}$")

    def test_noncanonical_or_package_mismatched_receipt_is_rejected_before_probe(self):
        capability, raw, receipt, unused_observed, unused_process = self._fixture()
        with self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_receipt_invalid",
        ):
            verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=raw + b"\n"
            )
        changed = dict(receipt)
        changed["controller_requirements_lock_sha256"] = "f" * 64
        changed_raw = _canonical(changed)
        package_evidence = capability._evidence
        changed_package = _VerifiedPackageCapability(
            replace(
                package_evidence,
                controller_runtime_receipt_sha256=hashlib.sha256(
                    changed_raw
                ).hexdigest(),
            ),
            {},
            dict(capability._artifacts),
            _PACKAGE_TOKEN,
        )
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem"
        ) as probe:
            with self.assertRaisesRegex(
                ControllerRuntimeCapabilityError,
                "controller_runtime_package_binding_mismatch",
            ):
                verify_controller_runtime_capability(
                    changed_package, runtime_build_receipt_json=changed_raw
                )
            probe.assert_not_called()

    def test_filesystem_or_process_drift_is_rejected(self):
        capability, raw, unused_receipt, observed, process = self._fixture()
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem",
            return_value=replace(observed, interpreter_sha256="0" * 64),
        ):
            with self.assertRaisesRegex(
                ControllerRuntimeCapabilityError,
                "controller_runtime_filesystem_binding_mismatch",
            ):
                verify_controller_runtime_capability(
                    capability, runtime_build_receipt_json=raw
                )
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem",
            return_value=replace(observed, release_tree_sha256="0" * 64),
        ):
            with self.assertRaisesRegex(
                ControllerRuntimeCapabilityError,
                "controller_runtime_filesystem_binding_mismatch",
            ):
                verify_controller_runtime_capability(
                    capability, runtime_build_receipt_json=raw
                )
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem",
            return_value=observed,
        ), patch(
            "tools.governed_memory_install.controller_runtime._run_process_probes",
            return_value=replace(process, launcher_help_probe_sha256="0" * 64),
        ):
            with self.assertRaisesRegex(
                ControllerRuntimeCapabilityError,
                "controller_runtime_process_binding_mismatch",
            ):
                verify_controller_runtime_capability(
                    capability, runtime_build_receipt_json=raw
                )

    def test_release_tree_is_exact_and_manifest_has_canonical_path_authority(self):
        artifacts = {
            "tools/governed_memory_install/__init__.py": b"",
            "tools/governed_memory_install/example.py": b"VALUE = 1\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_sha = self._write_release(root, artifacts)
            self._seal_release_directories(root)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch(
                    "tools.governed_memory_install.controller_runtime._EXPECTED_UID",
                    os.getuid(),
                ):
                    first = _observe_release_tree(
                        descriptor,
                        package_artifacts=artifacts,
                        package_manifest_sha256=manifest_sha,
                    )
                    second = _observe_release_tree(
                        descriptor,
                        package_artifacts=artifacts,
                        package_manifest_sha256=manifest_sha,
                    )
            finally:
                os.close(descriptor)
                self._unseal_release_directories(root)
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{64}$")

            canonical = root / str(PACKAGE_MANIFEST_RELATIVE_PATH)
            misplaced = root / "package_manifest.json"
            canonical.rename(misplaced)
            self._seal_release_directories(root)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch(
                    "tools.governed_memory_install.controller_runtime._EXPECTED_UID",
                    os.getuid(),
                ), self.assertRaisesRegex(
                    ControllerRuntimeCapabilityError,
                    "controller_release_tree_set_mismatch",
                ):
                    _observe_release_tree(
                        descriptor,
                        package_artifacts=artifacts,
                        package_manifest_sha256=manifest_sha,
                    )
            finally:
                os.close(descriptor)
                self._unseal_release_directories(root)

    def test_release_tree_rejects_extra_symlink_content_and_manifest_drift(self):
        artifacts = {
            "tools/governed_memory_install/__init__.py": b"",
            "tools/governed_memory_install/example.py": b"VALUE = 1\n",
        }
        scenarios = ("extra", "symlink", "content", "manifest", "writable")
        for scenario in scenarios:
            with self.subTest(
                scenario=scenario
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest_sha = self._write_release(
                    root,
                    artifacts,
                    manifest_artifacts=(
                        {} if scenario == "manifest" else None
                    ),
                )
                if scenario == "extra":
                    (root / "unexpected.txt").write_bytes(b"unexpected\n")
                elif scenario == "symlink":
                    target = root / "tools/governed_memory_install/example.py"
                    target.unlink()
                    target.symlink_to(
                        root / str(PACKAGE_MANIFEST_RELATIVE_PATH)
                    )
                elif scenario == "content":
                    target = root / "tools/governed_memory_install/example.py"
                    target.chmod(0o644)
                    target.write_bytes(b"VALUE = 2\n")
                    target.chmod(0o444)
                elif scenario == "writable":
                    (root / "tools/governed_memory_install/example.py").chmod(
                        0o644
                    )
                self._seal_release_directories(root)
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with patch(
                        "tools.governed_memory_install.controller_runtime."
                        "_EXPECTED_UID",
                        os.getuid(),
                    ), self.assertRaises(ControllerRuntimeCapabilityError):
                        _observe_release_tree(
                            descriptor,
                            package_artifacts=artifacts,
                            package_manifest_sha256=manifest_sha,
                        )
                finally:
                    os.close(descriptor)
                    self._unseal_release_directories(root)

    def test_exact_launcher_local_module_closure_is_sealed(self):
        artifacts = self._runtime_package_artifacts()
        reachable = _verify_launcher_module_closure(artifacts)
        self.assertIn(
            "tools.governed_memory_install.store_supervisor", reachable
        )
        self.assertIn(
            "tools.governed_memory_install.resource_identity", reachable
        )
        missing = dict(artifacts)
        del missing["tools/governed_memory_install/host_boundary.py"]
        with self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_release_module_closure_invalid",
        ):
            _verify_launcher_module_closure(missing)

    def test_requirements_lock_is_strict_and_exact(self):
        lock = (ROOT / str(REQUIREMENTS_LOCK_RELATIVE_PATH)).read_bytes()
        self.assertEqual(
            _requirements_from_lock(lock),
            {
                "cffi": "2.1.0",
                "cryptography": "49.0.0",
                "pycparser": "3.0",
            },
        )
        malformed = (
            b"cffi==2.1.0\n",
            b"cffi>=2.1.0 --hash=sha256:" + b"0" * 64 + b"\n",
            b"cffi==2.1.0 --hash=sha512:" + b"0" * 64 + b"\n",
            b"cffi==2.1.0 --hash=sha256:" + b"0" * 63 + b"\n",
            (
                b"cffi==2.1.0 --hash=sha256:"
                + b"0" * 64
                + b"\nCFFI==2.1.0 --hash=sha256:"
                + b"1" * 64
                + b"\n"
            ),
        )
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                ControllerRuntimeCapabilityError,
                "controller_runtime_requirements_lock_invalid",
            ):
                _requirements_from_lock(raw)

    def test_installed_distribution_inventory_must_equal_lock(self):
        mismatches = (
            {
                "cryptography": "49.0.0",
                "pycparser": "3.0",
            },
            {
                "cffi": "2.1.0",
                "cryptography": "49.0.0",
                "pycparser": "3.0",
                "unexpected": "1.0",
            },
            {
                "cffi": "2.1.0",
                "cryptography": "48.0.0",
                "pycparser": "3.0",
            },
        )
        for distributions in mismatches:
            with self.subTest(distributions=distributions):
                capability, raw, unused_receipt, observed, process = self._fixture(
                    distributions=distributions
                )
                with patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_observe_filesystem",
                    return_value=observed,
                ), patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_run_process_probes",
                    return_value=process,
                ), self.assertRaisesRegex(
                    ControllerRuntimeCapabilityError,
                    "controller_runtime_distribution_inventory_mismatch",
                ):
                    verify_controller_runtime_capability(
                        capability, runtime_build_receipt_json=raw
                    )

    def test_malformed_bound_lock_refuses_before_filesystem_probe(self):
        capability, raw, unused_receipt, unused_observed, unused_process = (
            self._fixture(lock_bytes=b"cffi==2.1.0\n")
        )
        with patch(
            "tools.governed_memory_install.controller_runtime._observe_filesystem"
        ) as probe, self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_requirements_lock_invalid",
        ):
            verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=raw
            )
        probe.assert_not_called()

    def test_systemd_template_uses_exact_release_launcher_not_module_search(self):
        unit = (
            __import__("pathlib").Path(
                "ops/governed_memory/installation/systemd/"
                "governed-memory-stores.service.in"
            ).read_text(encoding="ascii")
        )
        exact = (
            "/bin/python -I -B "
            "/opt/governed-memory-controller/releases/"
            "@PACKAGE_MANIFEST_SHA256@/tools/governed_memory_install/"
            "store_supervisor_launcher.py --package-manifest-sha256 "
            "@PACKAGE_MANIFEST_SHA256@"
        )
        self.assertIn(exact, unit)
        self.assertNotIn("-m tools.governed_memory_install", unit)

    def test_inventory_probe_derives_and_confines_import_paths(self):
        from tools.governed_memory_install.controller_runtime import (
            _INVENTORY_PROBE,
        )

        self.assertIn("resolved.relative_to(runtime_root)", _INVENTORY_PROBE)
        self.assertIn("site.getsitepackages", _INVENTORY_PROBE)
        self.assertIn(
            "system_site_packages_enabled = any", _INVENTORY_PROBE
        )
        self.assertNotIn(
            "system_site_packages_enabled=False", _INVENTORY_PROBE
        )
        self.assertIn(
            'if set(prefixes.values()) != {"."}', _INVENTORY_PROBE
        )


if __name__ == "__main__":
    unittest.main()
