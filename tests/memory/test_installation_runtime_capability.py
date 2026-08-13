from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governed_memory_install import authority, store_supervisor_launcher
from tools.governed_memory_install.controller_runtime import (
    CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH,
    ControllerRuntimeCapabilityError,
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
    LAUNCHER_RELATIVE_PATH,
    PACKAGE_MANIFEST_RELATIVE_PATH,
    REQUIREMENTS_LOCK_RELATIVE_PATH,
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
    _ObservedRuntime,
    _ProcessProbeResult,
    _hash_tree,
    _read_regular,
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

_SYNTHETIC_DRIVER_WHEELS = {
    "psycopg": (
        "psycopg-3.3.4-py3-none-any.whl",
        b"synthetic-runtime-capability-psycopg-wheel",
    ),
    "psycopg-binary": (
        "psycopg_binary-3.3.4-cp312-cp312-manylinux_2_17_x86_64.whl",
        b"synthetic-runtime-capability-psycopg-binary-wheel",
    ),
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


class RuntimeCapabilityTests(unittest.TestCase):
    def _runtime_package_artifacts(
        self,
        *,
        lock_bytes: bytes | None = None,
        ready_contract: bool = True,
    ) -> dict[str, bytes]:
        package_root = ROOT / "tools/governed_memory_install"
        artifacts = {
            path.relative_to(ROOT).as_posix(): path.read_bytes()
            for path in package_root.glob("*.py")
        }
        lock_path = str(REQUIREMENTS_LOCK_RELATIVE_PATH)
        if lock_bytes is None:
            lock_bytes = (ROOT / lock_path).read_bytes()
            if ready_contract:
                for distribution, (_, wheel_bytes) in _SYNTHETIC_DRIVER_WHEELS.items():
                    lines = lock_bytes.decode("ascii").splitlines()
                    index = next(
                        position
                        for position, line in enumerate(lines)
                        if line.startswith(f"{distribution}==3.3.4 ")
                    )
                    lines[index + 1] = (
                        "    --hash=sha256:"
                        + hashlib.sha256(wheel_bytes).hexdigest()
                    )
                    lock_bytes = ("\n".join(lines) + "\n").encode("ascii")
        artifacts[lock_path] = lock_bytes
        contract_path = str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
        contract_raw = (ROOT / contract_path).read_bytes()
        if ready_contract:
            contract = json.loads(contract_raw)
            cpython = contract["selected_runtime_inputs"][
                "standalone_cpython"
            ]
            cpython["archive_staged"] = True
            cpython["archive_bytes_sha256_verified_locally"] = True
            cpython["archive_member_types_verified"] = True
            cpython["archive_symlink_count"] = 1048
            cpython["archive_symlink_normalization_verified"] = True
            cpython["specification_sha256"] = "3" * 64
            cpython["expanded_payload_tree_sha256"] = "4" * 64
            driver = contract["selected_runtime_inputs"][
                "postgresql_driver"
            ]
            driver["selection_state"] = (
                "exact-selected-wheels-staged-verified-and-locked"
            )
            driver["selected_wheels"] = [
                {
                    "normalized_distribution": item[
                        "normalized_distribution"
                    ],
                    "selected_wheel_filename": _SYNTHETIC_DRIVER_WHEELS[
                        item["normalized_distribution"]
                    ][0],
                    "selected_wheel_sha256": hashlib.sha256(
                        _SYNTHETIC_DRIVER_WHEELS[
                            item["normalized_distribution"]
                        ][1]
                    ).hexdigest(),
                    "version": item["version"],
                }
                for item in driver["preferred_distributions"]
            ]
            for key in (
                "binary_native_library_closure_inspected",
                "current_controller_lock_contains_selection",
                "driver_native_postgresql_stages_packaged",
                "exact_wheel_filenames_frozen",
                "selection_ready_for_runtime_build",
                "wheel_bytes_sha256_verified_locally",
                "wheel_bytes_staged",
            ):
                driver[key] = True
            wheelhouse = contract["selected_runtime_inputs"]["wheelhouse"]
            wheelhouse["canonical_member_count"] = 6
            wheelhouse["canonical_total_bytes"] = 12345
            wheelhouse["canonical_tree_sha256"] = "5" * 64
            wheelhouse["wheelhouse_staged"] = True
            build_policy = contract["build_policy"]
            build_policy["standalone_cpython_archive_staged"] = True
            build_policy[
                "standalone_cpython_archive_content_verified"
            ] = True
            build_policy["offline_wheelhouse_staged"] = True
            build_policy["canonical_wheelhouse_identity_present"] = True
            build_policy["preferred_postgresql_driver_locked"] = True
            contract["dependency_policy"][
                "current_lock_contains_preferred_postgresql_driver"
            ] = True
            contract_raw = _canonical(contract)
        else:
            contract = json.loads(contract_raw)
            contract["build_policy"][
                "standalone_cpython_archive_staged"
            ] = False
            contract_raw = _canonical(contract)
        artifacts[contract_path] = contract_raw
        return artifacts

    def _fixture(
        self,
        *,
        distributions: dict[str, str] | None = None,
        lock_bytes: bytes | None = None,
        ready_contract: bool = True,
    ):
        package_sha = "a" * 64
        artifacts = self._runtime_package_artifacts(
            lock_bytes=lock_bytes,
            ready_contract=ready_contract,
        )
        runtime_contract = json.loads(
            artifacts[
                str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
            ].decode("ascii")
        )
        selected_cpython = runtime_contract["selected_runtime_inputs"][
            "standalone_cpython"
        ]
        runtime_contract_sha = hashlib.sha256(
            artifacts[str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)]
        ).hexdigest()
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
                        "psycopg": "3.3.4",
                        "psycopg-binary": "3.3.4",
                        "pycparser": "3.0",
                        "typing-extensions": "4.15.0",
                    }
                    if distributions is None
                    else distributions
                ),
                "interpreter_path_facts": path_facts,
                "interpreter_path_facts_sha256": path_facts_sha,
                "postgresql_driver_identity_sha256": (
                    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
                ),
                "pip_present": False,
                "platform_architecture": "x86_64",
                "platform_os": "linux",
                "python_implementation": "CPython",
                "python_version": selected_cpython["python_version"],
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
            "build_plan_sha256": "2" * 64,
            "standalone_cpython_specification_sha256": "3" * 64,
            "standalone_cpython_archive_sha256": selected_cpython[
                "archive_sha256"
            ],
            "standalone_cpython_payload_tree_sha256": "4" * 64,
            "wheelhouse_tree_sha256": "5" * 64,
            "python_implementation": "CPython",
            "python_version": selected_cpython["python_version"],
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "runtime_tree_sha256": observed.runtime_tree_sha256,
            "release_tree_sha256": observed.release_tree_sha256,
            "interpreter_sha256": observed.interpreter_sha256,
            "installed_distribution_inventory_sha256": observed.inventory_sha256,
            "interpreter_path_facts_sha256": path_facts_sha,
            "postgresql_driver_identity_sha256": (
                EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
            ),
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
            "active_production_state_changed": False,
            "persistent_controller_substrate_created": True,
            "persistent_store_resources_created": False,
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
        for field in (
            "build_plan_sha256",
            "standalone_cpython_specification_sha256",
            "standalone_cpython_archive_sha256",
            "standalone_cpython_payload_tree_sha256",
            "wheelhouse_tree_sha256",
        ):
            self.assertEqual(
                getattr(evidence, field), unused_receipt[field], field
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
        self.assertTrue(evidence.persistent_controller_substrate_created)
        self.assertFalse(evidence.persistent_store_resources_created)

    def test_current_unready_runtime_inputs_refuse_before_probe(self):
        capability, raw, unused_receipt, unused_observed, unused_process = (
            self._fixture(ready_contract=False)
        )
        with patch(
            "tools.governed_memory_install.controller_runtime."
            "_observe_filesystem"
        ) as probe, self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_input_contract_not_ready",
        ):
            verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=raw
            )
        probe.assert_not_called()

    def test_preference_metadata_cannot_substitute_for_selected_wheels(self):
        capability, raw, unused_receipt, unused_observed, unused_process = (
            self._fixture()
        )
        artifacts = dict(capability._artifacts)
        contract_path = str(CONTROLLER_RUNTIME_CONTRACT_RELATIVE_PATH)
        contract = json.loads(artifacts[contract_path].decode("ascii"))
        driver = contract["selected_runtime_inputs"]["postgresql_driver"]
        misplaced_selection = driver["selected_wheels"].pop(0)
        driver["preferred_distributions"][0].update(
            {
                "selected_wheel_filename": misplaced_selection[
                    "selected_wheel_filename"
                ],
                "selected_wheel_sha256": misplaced_selection[
                    "selected_wheel_sha256"
                ],
            }
        )
        contract_raw = _canonical(contract)
        artifacts[contract_path] = contract_raw
        contaminated = _VerifiedPackageCapability(
            replace(
                capability._evidence,
                controller_runtime_contract_sha256=hashlib.sha256(
                    contract_raw
                ).hexdigest(),
            ),
            {},
            artifacts,
            _PACKAGE_TOKEN,
        )
        with patch(
            "tools.governed_memory_install.controller_runtime."
            "_observe_filesystem"
        ) as probe, self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_input_contract_not_ready",
        ):
            verify_controller_runtime_capability(
                contaminated, runtime_build_receipt_json=raw
            )
        probe.assert_not_called()

    def test_noncanonical_or_package_mismatched_receipt_is_rejected_before_probe(self):
        capability, raw, receipt, unused_observed, unused_process = self._fixture()
        with self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_receipt_invalid",
        ):
            verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=raw + b"\n"
            )
        legacy = dict(receipt)
        del legacy["persistent_controller_substrate_created"]
        del legacy["persistent_store_resources_created"]
        legacy["persistent_resources_created"] = False
        legacy["production_state_changed"] = legacy.pop(
            "active_production_state_changed"
        )
        with self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_receipt_invalid",
        ):
            verify_controller_runtime_capability(
                capability, runtime_build_receipt_json=_canonical(legacy)
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

    def test_runtime_input_provenance_is_closed_and_authority_bound(self):
        capability, raw, receipt, unused_observed, unused_process = self._fixture()
        provenance_fields = (
            "build_plan_sha256",
            "standalone_cpython_specification_sha256",
            "standalone_cpython_archive_sha256",
            "standalone_cpython_payload_tree_sha256",
            "wheelhouse_tree_sha256",
        )
        for field in provenance_fields:
            with self.subTest(field=field, defect="missing"):
                changed = dict(receipt)
                del changed[field]
                with patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_observe_filesystem"
                ) as probe, self.assertRaisesRegex(
                    ControllerRuntimeCapabilityError,
                    "controller_runtime_receipt_invalid",
                ):
                    verify_controller_runtime_capability(
                        capability,
                        runtime_build_receipt_json=_canonical(changed),
                    )
                probe.assert_not_called()
            with self.subTest(field=field, defect="invalid"):
                changed = dict(receipt)
                changed[field] = "not-a-hash"
                with patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_observe_filesystem"
                ) as probe, self.assertRaisesRegex(
                    ControllerRuntimeCapabilityError,
                    "controller_runtime_receipt_invalid",
                ):
                    verify_controller_runtime_capability(
                        capability,
                        runtime_build_receipt_json=_canonical(changed),
                    )
                probe.assert_not_called()
            with self.subTest(field=field, defect="authority-hash"):
                changed = dict(receipt)
                changed[field] = "9" * 64
                with patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_observe_filesystem"
                ) as probe, self.assertRaisesRegex(
                    ControllerRuntimeCapabilityError,
                    "controller_runtime_package_binding_mismatch",
                ):
                    verify_controller_runtime_capability(
                        capability,
                        runtime_build_receipt_json=_canonical(changed),
                    )
                probe.assert_not_called()

        changed_archive = dict(receipt)
        changed_archive["standalone_cpython_archive_sha256"] = "9" * 64
        changed_raw = _canonical(changed_archive)
        changed_capability = _VerifiedPackageCapability(
            replace(
                capability._evidence,
                controller_runtime_receipt_sha256=hashlib.sha256(
                    changed_raw
                ).hexdigest(),
            ),
            {},
            dict(capability._artifacts),
            _PACKAGE_TOKEN,
        )
        with patch(
            "tools.governed_memory_install.controller_runtime."
            "_observe_filesystem"
        ) as probe, self.assertRaisesRegex(
            ControllerRuntimeCapabilityError,
            "controller_runtime_input_binding_mismatch",
        ):
            verify_controller_runtime_capability(
                changed_capability,
                runtime_build_receipt_json=changed_raw,
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

    def test_sealed_supervisor_launcher_is_read_as_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "store_supervisor_launcher.py"
            launcher.write_bytes(b"pass\n")
            launcher.chmod(0o555)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch(
                    "tools.governed_memory_install.controller_runtime._EXPECTED_UID",
                    os.getuid(),
                ):
                    raw, digest, mode = _read_regular(
                        descriptor,
                        launcher.name,
                        executable=True,
                    )
            finally:
                os.close(descriptor)
            self.assertEqual(raw, b"pass\n")
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(mode, 0o555)

    def test_tree_hashes_use_global_canonical_path_order(self) -> None:
        artifacts = {
            "a/z.py": b"VALUE = 1\n",
            "a.txt": b"root sibling\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_sha = self._write_release(root, artifacts)
            expected_files = {
                path: hashlib.sha256(raw).hexdigest()
                for path, raw in artifacts.items()
            }
            expected_files[str(PACKAGE_MANIFEST_RELATIVE_PATH)] = manifest_sha
            expected_directories: set[str] = set()
            for path in expected_files:
                parts = Path(path).parts
                for limit in range(1, len(parts)):
                    expected_directories.add("/".join(parts[:limit]))
            expected_entries = [
                {
                    "path": path + ("/" if path in expected_directories else ""),
                    "mode": 0o555 if path in expected_directories else 0o444,
                    "sha256": (
                        "directory"
                        if path in expected_directories
                        else expected_files[path]
                    ),
                }
                for path in sorted(expected_directories | set(expected_files))
            ]
            expected_release_sha = hashlib.sha256(
                _canonical(
                    {
                        "schema_version": (
                            "governed-memory-controller-release-tree-v1"
                        ),
                        "package_manifest_path": str(
                            PACKAGE_MANIFEST_RELATIVE_PATH
                        ),
                        "package_manifest_sha256": manifest_sha,
                        "root_mode": 0o555,
                        "entries": expected_entries,
                    }
                )
            ).hexdigest()
            expected_runtime_sha = hashlib.sha256(
                _canonical(
                    {
                        "schema_version": (
                            "governed-memory-controller-runtime-tree-v1"
                        ),
                        "entries": expected_entries,
                    }
                )
            ).hexdigest()

            self._seal_release_directories(root)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch(
                    "tools.governed_memory_install.controller_runtime."
                    "_EXPECTED_UID",
                    os.getuid(),
                ):
                    observed_release_sha = _observe_release_tree(
                        descriptor,
                        package_artifacts=artifacts,
                        package_manifest_sha256=manifest_sha,
                    )
                    observed_runtime_sha = _hash_tree(descriptor)
            finally:
                os.close(descriptor)
                self._unseal_release_directories(root)

            self.assertEqual(observed_release_sha, expected_release_sha)
            self.assertEqual(observed_runtime_sha, expected_runtime_sha)

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
                "psycopg": "3.3.4",
                "psycopg-binary": "3.3.4",
                "pycparser": "3.0",
                "typing-extensions": "4.15.0",
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


class StoreSupervisorLauncherTests(unittest.TestCase):
    def _staged_fixture(self, root: Path) -> tuple[Path, Path, str]:
        staging = root / "build-staging"
        release = staging / ("a" * 64) / "release"
        launcher = release / store_supervisor_launcher._RELATIVE
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"synthetic launcher\n")
        launcher.chmod(0o500)
        manifest = release / store_supervisor_launcher._MANIFEST_RELATIVE
        manifest.parent.mkdir(parents=True)
        raw = b'{"synthetic":"exact staged package"}\n'
        manifest.write_bytes(raw)
        manifest.chmod(0o400)
        return staging, launcher, hashlib.sha256(raw).hexdigest()

    def test_final_release_location_remains_valid_for_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package_sha256 = "b" * 64
            releases = root / "releases"
            launcher = (
                releases
                / package_sha256
                / store_supervisor_launcher._RELATIVE
            )
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"synthetic launcher\n")
            with (
                patch.object(store_supervisor_launcher, "_RELEASES", releases),
                patch.object(
                    store_supervisor_launcher, "__file__", str(launcher)
                ),
            ):
                self.assertEqual(
                    store_supervisor_launcher._verified_release_root(
                        package_sha256, staged_help_probe=False
                    ),
                    releases / package_sha256,
                )

    def test_exact_staged_help_location_requires_bound_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, launcher, package_sha256 = self._staged_fixture(root)
            with (
                patch.object(
                    store_supervisor_launcher, "_BUILD_STAGING", staging
                ),
                patch.object(
                    store_supervisor_launcher, "__file__", str(launcher)
                ),
                patch.object(
                    store_supervisor_launcher, "_ROOT_UID", os.getuid()
                ),
                patch.object(
                    store_supervisor_launcher, "_ROOT_GID", os.getgid()
                ),
            ):
                self.assertEqual(
                    store_supervisor_launcher._verified_release_root(
                        package_sha256, staged_help_probe=True
                    ),
                    staging / ("a" * 64) / "release",
                )

    def test_staged_operational_command_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, launcher, package_sha256 = self._staged_fixture(root)
            with (
                patch.object(
                    store_supervisor_launcher, "_BUILD_STAGING", staging
                ),
                patch.object(
                    store_supervisor_launcher, "__file__", str(launcher)
                ),
                self.assertRaisesRegex(
                    store_supervisor_launcher.StoreSupervisorLauncherError,
                    "launcher_release_identity_mismatch",
                ),
            ):
                store_supervisor_launcher._verified_release_root(
                    package_sha256, staged_help_probe=False
                )

    def test_staged_manifest_hash_mode_and_symlink_are_enforced(self):
        for mutation in ("hash", "mode", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                staging, launcher, package_sha256 = self._staged_fixture(root)
                manifest = (
                    staging
                    / ("a" * 64)
                    / "release"
                    / store_supervisor_launcher._MANIFEST_RELATIVE
                )
                expected = package_sha256
                if mutation == "hash":
                    expected = "c" * 64
                elif mutation == "mode":
                    manifest.chmod(0o600)
                else:
                    raw = manifest.read_bytes()
                    target = manifest.with_name("manifest.target")
                    target.write_bytes(raw)
                    target.chmod(0o400)
                    manifest.unlink()
                    manifest.symlink_to(target)
                with (
                    patch.object(
                        store_supervisor_launcher, "_BUILD_STAGING", staging
                    ),
                    patch.object(
                        store_supervisor_launcher, "__file__", str(launcher)
                    ),
                    patch.object(
                        store_supervisor_launcher, "_ROOT_UID", os.getuid()
                    ),
                    patch.object(
                        store_supervisor_launcher, "_ROOT_GID", os.getgid()
                    ),
                    self.assertRaisesRegex(
                        store_supervisor_launcher.StoreSupervisorLauncherError,
                        "launcher_staged_manifest_invalid",
                    ),
                ):
                    store_supervisor_launcher._verified_release_root(
                        expected, staged_help_probe=True
                    )


if __name__ == "__main__":
    unittest.main()
