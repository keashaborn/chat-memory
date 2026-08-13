from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from tools.governed_memory_install.controller_runtime import (
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
    _receipt_document,
)
from tools.governed_memory_release.controller_runtime_builder import (
    BUILD_RESULT_TYPE,
    PUBLICATION_RESULT_TYPE,
    BuildObservation,
    ControllerRuntimeBuildError,
    PublicationObservation,
    PublicationTargetObservation,
    StageObservation,
    create_controller_runtime_build_plan,
    execute_controller_runtime_build,
    parse_standalone_cpython_substrate,
)


ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _substrate(**changes: object) -> bytes:
    document: dict[str, object] = {
        "schema_version": (
            "governed-memory-controller-standalone-cpython-substrate-v1"
        ),
        "state": "externally-approved-exact-offline-substrate",
        "implementation": "CPython",
        "python_version": "3.12.3",
        "platform_os": "linux",
        "platform_architecture": "x86_64",
        "distribution_kind": "standalone-cpython-install-only",
        "archive_name": "cpython-synthetic-3.12.3-x86_64.tar",
        "archive_sha256": "a" * 64,
        "payload_tree_sha256": "b" * 64,
        "payload_root": "python/install",
        "interpreter_relative_path": "bin/python",
        "archive_members_are_regular_files_or_directories_only": True,
        "archive_contains_no_symlinks_hardlinks_or_special_files": True,
        "runtime_is_not_venv": True,
        "network_calls": 0,
    }
    document.update(changes)
    return _canonical(document)


def _package() -> tuple[bytes, dict[str, bytes]]:
    contract = (
        ROOT
        / "ops/governed_memory/installation/current/"
        "controller_runtime_contract.json"
    ).read_bytes()
    artifacts = {
        "ops/governed_memory/controller-requirements.lock": (
            ROOT / "ops/governed_memory/controller-requirements.lock"
        ).read_bytes(),
        "ops/governed_memory/installation/current/"
        "controller_runtime_contract.json": contract,
        "tools/governed_memory_install/store_supervisor_launcher.py": b"pass\n",
    }
    manifest = {
        "schema_version": "synthetic-controller-package-v1",
        "state": "repository-only",
        "artifacts": {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in sorted(artifacts.items())
        },
    }
    # Production manifests use indented JSON and a trailing newline.  The raw
    # bytes, rather than an implicit rewrite, remain package authority.
    return (
        json.dumps(manifest, indent=2, sort_keys=True).encode("ascii") + b"\n",
        artifacts,
    )


def _plan():
    manifest, artifacts = _package()
    contract = (
        ROOT
        / "ops/governed_memory/installation/current/"
        "controller_runtime_contract.json"
    ).read_bytes()
    lock = artifacts["ops/governed_memory/controller-requirements.lock"]
    plan = create_controller_runtime_build_plan(
        build_nonce="c" * 64,
        standalone_cpython_substrate_json=_substrate(),
        wheelhouse_tree_sha256="d" * 64,
        controller_runtime_contract_json=contract,
        controller_requirements_lock=lock,
        package_manifest_json=manifest,
        package_artifacts=artifacts,
    )
    return plan, manifest, artifacts


def _build_observation(plan) -> BuildObservation:
    return BuildObservation(
        result_type=BUILD_RESULT_TYPE,
        build_plan_sha256=plan.build_plan_sha256,
        substrate_specification_sha256=(
            plan.substrate.specification_sha256
        ),
        substrate_archive_sha256=plan.substrate.archive_sha256,
        substrate_payload_tree_sha256=plan.substrate.payload_tree_sha256,
        wheelhouse_tree_sha256=plan.wheelhouse_tree_sha256,
        release_closure_sha256=plan.release_closure_sha256,
        release_artifact_count=plan.release_artifact_count,
        python_implementation="CPython",
        python_version="3.12.3",
        platform_os="linux",
        platform_architecture="x86_64",
        runtime_tree_sha256="1" * 64,
        release_tree_sha256="2" * 64,
        interpreter_sha256="3" * 64,
        installed_distribution_inventory_sha256="4" * 64,
        interpreter_path_facts_sha256="5" * 64,
        supervisor_launcher_sha256=plan.supervisor_launcher_sha256,
        launcher_help_probe_sha256="7" * 64,
        pip_present=False,
        setuptools_present=False,
        wheel_present=False,
        user_site_enabled=False,
        system_site_packages_enabled=False,
        normal_venv_created=False,
        offline_substrate_regular_root_owned_nonwritable=True,
        offline_wheelhouse_regular_root_owned_nonwritable=True,
        runtime_root_mode=0o555,
        release_root_mode=0o555,
        all_members_root_owned=True,
        writable_member_count=0,
        symlink_count=0,
        hardlink_count=0,
        special_file_count=0,
        network_calls=0,
        provider_calls=0,
        production_data_read=False,
        active_production_state_changed=False,
    )


class _FakeTransport:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.initial_stage = StageObservation("absent", None)
        self.targets = PublicationTargetObservation(
            "absent", "absent", "absent", True
        )
        self.observation = _build_observation(plan)
        self.publication_changes: dict[str, object] = {}
        self.abandon_result = StageObservation("absent", None)
        self.calls: list[str] = []

    def observe_stage(self, plan):
        self.calls.append("observe_stage")
        return self.initial_stage

    def recover_owned_partial_stage(self, plan):
        self.calls.append("recover_owned_partial_stage")
        return StageObservation("absent", None)

    def create_stage(self, plan):
        self.calls.append("create_stage")
        return StageObservation(
            "owned_ready",
            plan.build_plan_sha256,
            root_mode=0o700,
            root_uid=0,
            root_gid=0,
            marker_regular_no_follow=True,
            created_no_replace=True,
            parent_fsynced=True,
        )

    def materialize_standalone_substrate(self, plan):
        self.calls.append("materialize_standalone_substrate")

    def install_locked_offline_distributions(self, plan):
        self.calls.append("install_locked_offline_distributions")

    def remove_bootstrap_packaging_tools(self, plan):
        self.calls.append("remove_bootstrap_packaging_tools")

    def stage_exact_release(
        self, plan, *, package_manifest_json, package_artifacts
    ):
        self.calls.append("stage_exact_release")

    def seal_and_observe(self, plan):
        self.calls.append("seal_and_observe")
        return self.observation

    def observe_publication_targets(
        self, plan, *, runtime_root, receipt_path
    ):
        self.calls.append("observe_publication_targets")
        return self.targets

    def publish_no_replace(
        self,
        plan,
        *,
        runtime_root,
        receipt_path,
        runtime_build_receipt_json,
    ):
        self.calls.append("publish_no_replace")
        value = PublicationObservation(
            result_type=PUBLICATION_RESULT_TYPE,
            runtime_root=runtime_root,
            release_root=plan.final_release_root,
            receipt_path=receipt_path,
            runtime_rename_no_replace=True,
            release_rename_no_replace=True,
            receipt_create_no_replace=True,
            runtime_root_mode=0o555,
            release_root_mode=0o555,
            receipt_mode=0o400,
            root_uid=0,
            root_gid=0,
            runtime_parent_fsynced=True,
            release_parent_fsynced=True,
            receipt_file_fsynced=True,
            receipt_parent_fsynced=True,
            stage_absent=True,
        )
        return replace(value, **self.publication_changes)

    def abandon_owned_stage(self, plan):
        self.calls.append("abandon_owned_stage")
        return self.abandon_result


class ControllerRuntimeBuilderTests(unittest.TestCase):
    def test_plan_is_exact_offline_non_venv_and_path_confined(self):
        plan, unused_manifest, unused_artifacts = _plan()
        self.assertRegex(plan.build_plan_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            plan.stage_root,
            "/var/lib/governed-memory-controller/build-staging/"
            + plan.build_plan_sha256,
        )
        self.assertEqual(
            plan.substrate.archive_path,
            "/var/lib/governed-memory-controller/offline/cpython/"
            + "a" * 64
            + "/cpython-synthetic-3.12.3-x86_64.tar",
        )
        self.assertEqual(
            plan.wheelhouse_root,
            "/var/lib/governed-memory-controller/offline/wheelhouses/"
            + "d" * 64,
        )
        self.assertTrue(
            plan.final_release_root.startswith(
                "/opt/governed-memory-controller/releases/"
            )
        )
        rendered_operations = " ".join(plan.operations).lower()
        self.assertNotIn("create_venv", rendered_operations)
        self.assertNotIn("python -m venv", rendered_operations)
        self.assertIn("non_venv_interpreter", rendered_operations)
        self.assertIn("atomic_rename_each_root_no_replace", plan.operations[-2])
        self.assertEqual(
            plan.required_distributions,
            {
                "cffi": "2.1.0",
                "cryptography": "49.0.0",
                "pycparser": "3.0",
            },
        )

    def test_substrate_requires_external_exact_selection_and_canonical_bytes(self):
        selected = parse_standalone_cpython_substrate(_substrate())
        self.assertEqual(selected.archive_sha256, "a" * 64)
        refused = (
            _substrate(state="identity-not-selected"),
            _substrate(runtime_is_not_venv=False),
            _substrate(network_calls=1),
            _substrate() + b"\n",
        )
        for raw in refused:
            with self.subTest(raw=raw), self.assertRaises(
                ControllerRuntimeBuildError
            ):
                parse_standalone_cpython_substrate(raw)

    def test_release_manifest_must_close_every_exact_artifact(self):
        plan, manifest, artifacts = _plan()
        changed = dict(artifacts)
        changed[next(iter(changed))] += b"changed"
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_release_closure_invalid"
        ):
            create_controller_runtime_build_plan(
                build_nonce=plan.build_nonce,
                standalone_cpython_substrate_json=_substrate(),
                wheelhouse_tree_sha256=plan.wheelhouse_tree_sha256,
                controller_runtime_contract_json=(
                    ROOT
                    / "ops/governed_memory/installation/current/"
                    "controller_runtime_contract.json"
                ).read_bytes(),
                controller_requirements_lock=artifacts[
                    "ops/governed_memory/controller-requirements.lock"
                ],
                package_manifest_json=manifest,
                package_artifacts=changed,
            )

    def test_success_builds_verifier_canonical_receipt_and_closed_paths(self):
        plan, manifest, artifacts = _plan()
        transport = _FakeTransport(plan)
        result = execute_controller_runtime_build(
            plan,
            package_manifest_json=manifest,
            package_artifacts=artifacts,
            transport=transport,
        )
        raw, receipt = _receipt_document(result.runtime_build_receipt_json)
        self.assertEqual(raw, result.runtime_build_receipt_json)
        self.assertEqual(receipt["schema_version"], RUNTIME_RECEIPT_SCHEMA)
        self.assertEqual(receipt["result"], RUNTIME_RECEIPT_RESULT)
        self.assertFalse(receipt["active_production_state_changed"])
        self.assertTrue(receipt["persistent_controller_substrate_created"])
        self.assertFalse(receipt["persistent_store_resources_created"])
        self.assertEqual(
            result.controller_runtime_receipt_sha256,
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            result.runtime_root,
            "/opt/governed-memory-controller/runtimes/"
            + result.controller_runtime_receipt_sha256,
        )
        self.assertEqual(
            result.receipt_path,
            "/var/lib/governed-memory-controller/runtime-receipts/"
            + result.controller_runtime_receipt_sha256
            + ".json",
        )
        self.assertEqual(transport.calls[-1], "publish_no_replace")
        self.assertNotIn("abandon_owned_stage", transport.calls)

    def test_exact_owned_partial_stage_is_recovered_but_foreign_is_refused(self):
        plan, manifest, artifacts = _plan()
        recoverable = _FakeTransport(plan)
        recoverable.initial_stage = StageObservation(
            "owned_partial",
            plan.build_plan_sha256,
            root_mode=0o700,
            root_uid=0,
            root_gid=0,
            marker_regular_no_follow=True,
            created_no_replace=True,
            parent_fsynced=True,
        )
        execute_controller_runtime_build(
            plan,
            package_manifest_json=manifest,
            package_artifacts=artifacts,
            transport=recoverable,
        )
        self.assertEqual(
            recoverable.calls[:3],
            ["observe_stage", "recover_owned_partial_stage", "create_stage"],
        )

        foreign = _FakeTransport(plan)
        foreign.initial_stage = StageObservation("foreign", "e" * 64)
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_stage_invalid"
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=foreign,
            )
        self.assertEqual(foreign.calls, ["observe_stage"])

        untrusted_marker = _FakeTransport(plan)
        untrusted_marker.initial_stage = StageObservation(
            "owned_partial", plan.build_plan_sha256
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_stage_invalid"
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=untrusted_marker,
            )
        self.assertEqual(untrusted_marker.calls, ["observe_stage"])

    def test_existing_final_path_refuses_and_current_stage_is_abandoned(self):
        plan, manifest, artifacts = _plan()
        transport = _FakeTransport(plan)
        transport.targets = PublicationTargetObservation(
            "absent", "present", "absent", True
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_final_path_exists_or_partial",
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=transport,
            )
        self.assertEqual(transport.calls[-1], "abandon_owned_stage")
        self.assertNotIn("publish_no_replace", transport.calls)

        insecure_absence = _FakeTransport(plan)
        insecure_absence.targets = PublicationTargetObservation(
            "absent", "absent", "absent", False
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_final_path_exists_or_partial",
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=insecure_absence,
            )
        self.assertEqual(insecure_absence.calls[-1], "abandon_owned_stage")

    def test_build_evidence_and_publication_fsync_fail_closed(self):
        plan, manifest, artifacts = _plan()
        unsealed = _FakeTransport(plan)
        unsealed.observation = replace(
            unsealed.observation, runtime_root_mode=0o755
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_observation_invalid"
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=unsealed,
            )
        self.assertEqual(unsealed.calls[-1], "abandon_owned_stage")

        unflushed = _FakeTransport(plan)
        unflushed.publication_changes = {"receipt_parent_fsynced": False}
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_publication_invalid"
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=unflushed,
            )
        # Once publication begins, automatic deletion is forbidden.  A partial
        # final publication is left for explicit evidence-backed recovery.
        self.assertNotIn("abandon_owned_stage", unflushed.calls)

    def test_failed_owned_stage_cleanup_escalates_to_explicit_review(self):
        plan, manifest, artifacts = _plan()
        transport = _FakeTransport(plan)
        transport.observation = replace(
            transport.observation, special_file_count=1
        )
        transport.abandon_result = StageObservation(
            "owned_ready",
            plan.build_plan_sha256,
            root_mode=0o700,
            root_uid=0,
            root_gid=0,
            marker_regular_no_follow=True,
            created_no_replace=True,
            parent_fsynced=True,
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_partial_stage_requires_review",
        ):
            execute_controller_runtime_build(
                plan,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()
