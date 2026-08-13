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
    SELECTED_CPYTHON_ARCHIVE_NAME,
    SELECTED_CPYTHON_ARCHIVE_SHA256,
    SELECTED_CPYTHON_VERSION,
    WHEELHOUSE_TREE_SCHEMA,
    BuildObservation,
    ControllerRuntimeBuildError,
    PublicationObservation,
    PublicationTargetObservation,
    StageObservation,
    canonical_wheelhouse_identity,
    create_controller_runtime_build_plan,
    execute_controller_runtime_build,
    parse_standalone_cpython_substrate,
)


ROOT = Path(__file__).resolve().parents[2]

_WHEELHOUSE_ARTIFACTS = {
    "cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl": (
        b"synthetic-cffi-wheel"
    ),
    "cryptography-49.0.0-cp311-abi3-manylinux_2_34_x86_64.whl": (
        b"synthetic-cryptography-wheel"
    ),
    "psycopg-3.3.4-py3-none-any.whl": b"synthetic-psycopg-wheel",
    "psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64."
    "manylinux_2_17_x86_64.whl": (
        b"synthetic-psycopg-binary-wheel"
    ),
    "pycparser-3.0-py3-none-any.whl": b"synthetic-pycparser-wheel",
}


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
        "python_version": SELECTED_CPYTHON_VERSION,
        "platform_os": "linux",
        "platform_architecture": "x86_64",
        "distribution_kind": "standalone-cpython-install-only",
        "archive_name": SELECTED_CPYTHON_ARCHIVE_NAME,
        "archive_sha256": SELECTED_CPYTHON_ARCHIVE_SHA256,
        "payload_tree_sha256": "b" * 64,
        "payload_root": "python",
        "interpreter_relative_path": "bin/python",
        "archive_members_are_regular_files_or_directories_only": True,
        "archive_contains_no_symlinks_hardlinks_or_special_files": True,
        "runtime_is_not_venv": True,
        "network_calls": 0,
    }
    document.update(changes)
    return _canonical(document)


def _wheelhouse() -> dict[str, bytes]:
    return dict(_WHEELHOUSE_ARTIFACTS)


def _synthetic_lock() -> bytes:
    lines = ["# synthetic exact controller wheel closure"]
    for distribution, filename in (
        ("cffi", "cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"),
        (
            "cryptography",
            "cryptography-49.0.0-cp311-abi3-manylinux_2_34_x86_64.whl",
        ),
        ("psycopg", "psycopg-3.3.4-py3-none-any.whl"),
        (
            "psycopg-binary",
            "psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64."
            "manylinux_2_17_x86_64.whl",
        ),
        ("pycparser", "pycparser-3.0-py3-none-any.whl"),
    ):
        version = filename.split("-", 2)[1]
        digest = hashlib.sha256(_WHEELHOUSE_ARTIFACTS[filename]).hexdigest()
        lines.extend(
            (
                f"{distribution}=={version} \\",
                f"    --hash=sha256:{digest}",
            )
        )
    return ("\n".join(lines) + "\n").encode("ascii")


def _future_ready_contract() -> bytes:
    document = json.loads(
        (
            ROOT
            / "ops/governed_memory/installation/current/"
            "controller_runtime_contract.json"
        ).read_text(encoding="ascii")
    )
    driver = document["selected_runtime_inputs"]["postgresql_driver"]
    cpython = document["selected_runtime_inputs"]["standalone_cpython"]
    cpython["selection_state"] = "exact-offline-substrate-staged-and-verified"
    cpython["archive_staged"] = True
    cpython["archive_bytes_sha256_verified_locally"] = True
    cpython["archive_member_types_verified"] = True
    cpython["specification_sha256"] = hashlib.sha256(_substrate()).hexdigest()
    cpython["payload_tree_sha256"] = "b" * 64
    selected_wheels = {
        "psycopg": "psycopg-3.3.4-py3-none-any.whl",
        "psycopg-binary": (
            "psycopg_binary-3.3.4-cp312-cp312-"
            "manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
        ),
    }
    for item in driver["preferred_distributions"]:
        filename = selected_wheels[item["normalized_distribution"]]
        item["selected_wheel_filename"] = filename
        item["selected_wheel_sha256"] = hashlib.sha256(
            _WHEELHOUSE_ARTIFACTS[filename]
        ).hexdigest()
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
    build_policy = document["build_policy"]
    build_policy["canonical_wheelhouse_identity_present"] = True
    build_policy["offline_wheelhouse_staged"] = True
    build_policy["preferred_postgresql_driver_locked"] = True
    build_policy["standalone_cpython_archive_staged"] = True
    build_policy["standalone_cpython_archive_content_verified"] = True
    document["dependency_policy"][
        "current_lock_contains_preferred_postgresql_driver"
    ] = True
    wheelhouse = canonical_wheelhouse_identity(
        wheelhouse_artifacts=_wheelhouse(),
        controller_requirements_lock=_synthetic_lock(),
    )
    selected_wheelhouse = document["selected_runtime_inputs"]["wheelhouse"]
    selected_wheelhouse["canonical_member_count"] = wheelhouse.member_count
    selected_wheelhouse["canonical_total_bytes"] = wheelhouse.total_bytes
    selected_wheelhouse["canonical_tree_sha256"] = wheelhouse.tree_sha256
    selected_wheelhouse["wheelhouse_staged"] = True
    return _canonical(document)


def _package(
    *,
    contract: bytes | None = None,
    lock: bytes | None = None,
) -> tuple[bytes, dict[str, bytes]]:
    contract = contract if contract is not None else _future_ready_contract()
    lock = lock if lock is not None else _synthetic_lock()
    artifacts = {
        "ops/governed_memory/controller-requirements.lock": lock,
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
    contract = artifacts[
        "ops/governed_memory/installation/current/"
        "controller_runtime_contract.json"
    ]
    lock = artifacts["ops/governed_memory/controller-requirements.lock"]
    plan = create_controller_runtime_build_plan(
        build_nonce="c" * 64,
        standalone_cpython_substrate_json=_substrate(),
        wheelhouse_artifacts=_wheelhouse(),
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
        python_version=SELECTED_CPYTHON_VERSION,
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
            + SELECTED_CPYTHON_ARCHIVE_SHA256
            + "/"
            + SELECTED_CPYTHON_ARCHIVE_NAME,
        )
        self.assertEqual(
            plan.wheelhouse_root,
            "/var/lib/governed-memory-controller/offline/wheelhouses/"
            + plan.wheelhouse.tree_sha256,
        )
        self.assertEqual(plan.wheelhouse.schema_version, WHEELHOUSE_TREE_SCHEMA)
        self.assertEqual(plan.wheelhouse.member_count, 5)
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
                "psycopg": "3.3.4",
                "psycopg-binary": "3.3.4",
                "pycparser": "3.0",
            },
        )

    def test_substrate_requires_external_exact_selection_and_canonical_bytes(self):
        selected = parse_standalone_cpython_substrate(_substrate())
        self.assertEqual(
            selected.archive_sha256, SELECTED_CPYTHON_ARCHIVE_SHA256
        )
        refused = (
            _substrate(state="identity-not-selected"),
            _substrate(python_version="3.12.12"),
            _substrate(archive_name=SELECTED_CPYTHON_ARCHIVE_NAME[:-3]),
            _substrate(archive_sha256="a" * 64),
            _substrate(payload_root="python/install"),
            _substrate(runtime_is_not_venv=False),
            _substrate(network_calls=1),
            _substrate() + b"\n",
        )
        for raw in refused:
            with self.subTest(raw=raw), self.assertRaises(
                ControllerRuntimeBuildError
            ):
                parse_standalone_cpython_substrate(raw)

    def test_wheelhouse_identity_is_byte_derived_canonical_and_lock_closed(self):
        wheelhouse = _wheelhouse()
        selected = canonical_wheelhouse_identity(
            wheelhouse_artifacts=wheelhouse,
            controller_requirements_lock=_synthetic_lock(),
        )
        reordered = canonical_wheelhouse_identity(
            wheelhouse_artifacts=dict(reversed(tuple(wheelhouse.items()))),
            controller_requirements_lock=_synthetic_lock(),
        )
        self.assertEqual(selected, reordered)
        self.assertEqual(selected.schema_version, WHEELHOUSE_TREE_SCHEMA)
        self.assertEqual(
            tuple(member.distribution for member in selected.members),
            (
                "cffi",
                "cryptography",
                "psycopg",
                "psycopg-binary",
                "pycparser",
            ),
        )
        self.assertEqual(
            selected.total_bytes,
            sum(len(raw) for raw in wheelhouse.values()),
        )
        material = {
            "schema_version": WHEELHOUSE_TREE_SCHEMA,
            "platform": {
                "architecture": "x86_64",
                "os": "linux",
                "python_tag": "cp312",
            },
            "members": [
                {
                    "distribution": member.distribution,
                    "filename": member.filename,
                    "sha256": member.sha256,
                    "size": member.size,
                    "version": member.version,
                }
                for member in selected.members
            ],
        }
        self.assertEqual(
            selected.tree_sha256,
            hashlib.sha256(_canonical(material)).hexdigest(),
        )

    def test_wheelhouse_refuses_changed_missing_extra_duplicate_and_unsafe_members(self):
        changed = _wheelhouse()
        changed[next(iter(changed))] += b"changed"
        missing = _wheelhouse()
        missing.pop(next(iter(missing)))
        extra = _wheelhouse()
        extra["unknown-1.0-py3-none-any.whl"] = b"unknown"
        duplicate = _wheelhouse()
        duplicate["cffi-2.1.0-1-cp312-cp312-manylinux_2_17_x86_64.whl"] = (
            duplicate[
                "cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
            ]
        )
        wrong_name = _wheelhouse()
        cffi = wrong_name.pop(
            "cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        )
        wrong_name[
            "pycparser-3.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        ] = cffi
        unsafe = _wheelhouse()
        unsafe[
            "subdir/cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        ] = unsafe.pop(
            "cffi-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        )
        for candidate in (changed, missing, extra, duplicate, wrong_name, unsafe):
            with self.subTest(candidate=tuple(candidate)), self.assertRaisesRegex(
                ControllerRuntimeBuildError, "controller_wheelhouse_invalid"
            ):
                canonical_wheelhouse_identity(
                    wheelhouse_artifacts=candidate,
                    controller_requirements_lock=_synthetic_lock(),
                )

    def test_wheelhouse_refuses_one_hash_bound_to_multiple_distributions(self):
        digest = hashlib.sha256(b"shared-wheel-bytes").hexdigest()
        ambiguous = (
            "cffi==2.1.0 \\\n"
            f"    --hash=sha256:{digest}\n"
            "pycparser==3.0 \\\n"
            f"    --hash=sha256:{digest}\n"
        ).encode("ascii")
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_wheelhouse_invalid"
        ):
            canonical_wheelhouse_identity(
                wheelhouse_artifacts={
                    "cffi-2.1.0-py3-none-any.whl": b"shared-wheel-bytes",
                    "pycparser-3.0-py3-none-any.whl": b"shared-wheel-bytes",
                },
                controller_requirements_lock=ambiguous,
            )

    def test_contract_records_selected_inputs_without_claiming_staging_or_proof(self):
        contract = json.loads(
            (
                ROOT
                / "ops/governed_memory/installation/current/"
                "controller_runtime_contract.json"
            ).read_text(encoding="ascii")
        )
        selected = contract["selected_runtime_inputs"]
        cpython = selected["standalone_cpython"]
        self.assertEqual(cpython["python_version"], SELECTED_CPYTHON_VERSION)
        self.assertEqual(cpython["archive_name"], SELECTED_CPYTHON_ARCHIVE_NAME)
        self.assertEqual(
            cpython["archive_sha256"], SELECTED_CPYTHON_ARCHIVE_SHA256
        )
        self.assertEqual(cpython["payload_root"], "python")
        self.assertFalse(cpython["archive_staged"])
        self.assertFalse(cpython["archive_bytes_sha256_verified_locally"])
        self.assertIsNone(cpython["specification_sha256"])
        self.assertIsNone(cpython["payload_tree_sha256"])

        driver = selected["postgresql_driver"]
        self.assertEqual(driver["preferred_extra"], "binary")
        self.assertEqual(
            {
                item["normalized_distribution"]: (
                    item["version"], item["selected_wheel_sha256"]
                )
                for item in driver["preferred_distributions"]
            },
            {
                "psycopg": (
                    "3.3.4",
                    "b6bbc25ccf05c8fad3b061d9db2ef0909a555171b84b07f29458a447253d679a",
                ),
                "psycopg-binary": (
                    "3.3.4",
                    "e7510c37550f91a187e3660a8cc50d4b760f8c3b8b2f89ebc5698cd2c7f2c85d",
                ),
            },
        )
        self.assertEqual(
            {
                item["normalized_distribution"]: item[
                    "selected_wheel_filename"
                ]
                for item in driver["preferred_distributions"]
            },
            {
                "psycopg": "psycopg-3.3.4-py3-none-any.whl",
                "psycopg-binary": (
                    "psycopg_binary-3.3.4-cp312-cp312-"
                    "manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
                ),
            },
        )
        for key in (
            "current_controller_lock_contains_selection",
            "wheel_bytes_staged",
            "wheel_bytes_sha256_verified_locally",
            "binary_native_library_closure_inspected",
            "selection_ready_for_runtime_build",
        ):
            self.assertFalse(driver[key], key)
        self.assertTrue(
            driver["postgresql_source_closure_contract_packaged"]
        )
        self.assertFalse(driver["driver_native_postgresql_stages_packaged"])
        wheelhouse = selected["wheelhouse"]
        self.assertFalse(wheelhouse["caller_supplied_opaque_tree_sha256_accepted"])
        self.assertFalse(wheelhouse["wheelhouse_staged"])
        self.assertIsNone(wheelhouse["canonical_tree_sha256"])
        current_lock = (
            ROOT / "ops/governed_memory/controller-requirements.lock"
        ).read_text(encoding="ascii")
        self.assertNotIn("psycopg", current_lock)
        self.assertNotIn("asyncpg", current_lock)

    def test_current_repository_runtime_inputs_are_refused(self):
        contract = (
            ROOT
            / "ops/governed_memory/installation/current/"
            "controller_runtime_contract.json"
        ).read_bytes()
        lock = (
            ROOT / "ops/governed_memory/controller-requirements.lock"
        ).read_bytes()
        manifest, artifacts = _package(contract=contract, lock=lock)
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_runtime_substrate_not_ready",
        ):
            create_controller_runtime_build_plan(
                build_nonce="d" * 64,
                standalone_cpython_substrate_json=_substrate(),
                wheelhouse_artifacts=_wheelhouse(),
                controller_runtime_contract_json=contract,
                controller_requirements_lock=lock,
                package_manifest_json=manifest,
                package_artifacts=artifacts,
            )

    def test_plan_requires_ready_driver_and_exact_lock_wheel_bindings(self):
        not_ready = json.loads(_future_ready_contract().decode("ascii"))
        not_ready["selected_runtime_inputs"]["postgresql_driver"][
            "binary_native_library_closure_inspected"
        ] = False
        wrong_digest = json.loads(_future_ready_contract().decode("ascii"))
        wrong_digest["selected_runtime_inputs"]["postgresql_driver"][
            "preferred_distributions"
        ][0]["selected_wheel_sha256"] = "a" * 64
        wrong_tree = json.loads(_future_ready_contract().decode("ascii"))
        wrong_tree["selected_runtime_inputs"]["wheelhouse"][
            "canonical_tree_sha256"
        ] = "f" * 64
        for document, message in (
            (not_ready, "controller_runtime_postgresql_driver_not_ready"),
            (wrong_digest, "controller_runtime_postgresql_driver_not_ready"),
            (wrong_tree, "controller_runtime_wheelhouse_not_ready"),
        ):
            contract = _canonical(document)
            manifest, artifacts = _package(contract=contract)
            with self.subTest(message=message), self.assertRaisesRegex(
                ControllerRuntimeBuildError, message
            ):
                create_controller_runtime_build_plan(
                    build_nonce="e" * 64,
                    standalone_cpython_substrate_json=_substrate(),
                    wheelhouse_artifacts=_wheelhouse(),
                    controller_runtime_contract_json=contract,
                    controller_requirements_lock=_synthetic_lock(),
                    package_manifest_json=manifest,
                    package_artifacts=artifacts,
                )

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
                wheelhouse_artifacts=_wheelhouse(),
                controller_runtime_contract_json=artifacts[
                    "ops/governed_memory/installation/current/"
                    "controller_runtime_contract.json"
                ],
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
            standalone_cpython_substrate_json=_substrate(),
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
        self.assertEqual(receipt["build_plan_sha256"], plan.build_plan_sha256)
        self.assertEqual(
            receipt["standalone_cpython_specification_sha256"],
            plan.substrate.specification_sha256,
        )
        self.assertEqual(
            receipt["standalone_cpython_archive_sha256"],
            plan.substrate.archive_sha256,
        )
        self.assertEqual(
            receipt["standalone_cpython_payload_tree_sha256"],
            plan.substrate.payload_tree_sha256,
        )
        self.assertEqual(
            receipt["wheelhouse_tree_sha256"], plan.wheelhouse.tree_sha256
        )
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
            standalone_cpython_substrate_json=_substrate(),
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
                standalone_cpython_substrate_json=_substrate(),
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
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=untrusted_marker,
            )
        self.assertEqual(untrusted_marker.calls, ["observe_stage"])

    def test_partial_stage_creation_or_recovery_requires_manual_review(self):
        plan, manifest, artifacts = _plan()

        class MalformedCreate(_FakeTransport):
            def create_stage(self, plan):
                self.calls.append("create_stage")
                return StageObservation("absent", None)

        malformed_create = MalformedCreate(plan)
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_stage_creation_requires_review",
        ):
            execute_controller_runtime_build(
                plan,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=malformed_create,
            )
        self.assertNotIn("abandon_owned_stage", malformed_create.calls)

        class MalformedRecovery(_FakeTransport):
            def recover_owned_partial_stage(self, plan):
                self.calls.append("recover_owned_partial_stage")
                return StageObservation("owned_partial", plan.build_plan_sha256)

        malformed_recovery = MalformedRecovery(plan)
        malformed_recovery.initial_stage = StageObservation(
            "owned_partial",
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
            "controller_build_stage_recovery_requires_review",
        ):
            execute_controller_runtime_build(
                plan,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=malformed_recovery,
            )
        self.assertNotIn("abandon_owned_stage", malformed_recovery.calls)

    def test_execution_revalidates_factory_plan_before_transport(self):
        plan, manifest, artifacts = _plan()
        transport = _FakeTransport(plan)
        forged = replace(plan, stage_root="/tmp/attacker-controlled-stage")
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_plan_invalid"
        ):
            execute_controller_runtime_build(
                forged,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=transport,
            )
        self.assertEqual(transport.calls, [])

        forged_substrate = replace(
            plan.substrate,
            specification_sha256="f" * 64,
        )
        forged_material = {
            "schema_version": plan.schema_version,
            "build_nonce": plan.build_nonce,
            "package_manifest_sha256": plan.package_manifest_sha256,
            "controller_runtime_contract_sha256": (
                plan.controller_runtime_contract_sha256
            ),
            "controller_requirements_lock_sha256": (
                plan.controller_requirements_lock_sha256
            ),
            "supervisor_launcher_sha256": plan.supervisor_launcher_sha256,
            "release_closure_sha256": plan.release_closure_sha256,
            "standalone_cpython_specification_sha256": "f" * 64,
            "standalone_cpython_archive_sha256": (
                forged_substrate.archive_sha256
            ),
            "standalone_cpython_payload_tree_sha256": (
                forged_substrate.payload_tree_sha256
            ),
            "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
            "wheelhouse_member_count": plan.wheelhouse.member_count,
            "wheelhouse_total_bytes": plan.wheelhouse.total_bytes,
        }
        forged_sha = hashlib.sha256(_canonical(forged_material)).hexdigest()
        forged = replace(
            plan,
            substrate=forged_substrate,
            build_plan_sha256=forged_sha,
            stage_root=(
                "/var/lib/governed-memory-controller/build-staging/"
                + forged_sha
            ),
            staged_runtime_root=(
                "/var/lib/governed-memory-controller/build-staging/"
                + forged_sha
                + "/runtime"
            ),
            staged_release_root=(
                "/var/lib/governed-memory-controller/build-staging/"
                + forged_sha
                + "/release"
            ),
        )
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError, "controller_build_plan_invalid"
        ):
            execute_controller_runtime_build(
                forged,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=transport,
            )
        self.assertEqual(transport.calls, [])

    def test_driver_selection_refuses_renamed_selected_wheel(self):
        renamed = _wheelhouse()
        original = "psycopg-3.3.4-py3-none-any.whl"
        renamed["psycopg-3.3.4-attacker_selected_name.whl"] = renamed.pop(
            original
        )
        contract = json.loads(_future_ready_contract().decode("ascii"))
        candidate = canonical_wheelhouse_identity(
            wheelhouse_artifacts=renamed,
            controller_requirements_lock=_synthetic_lock(),
        )
        selected = contract["selected_runtime_inputs"]["wheelhouse"]
        selected["canonical_member_count"] = candidate.member_count
        selected["canonical_total_bytes"] = candidate.total_bytes
        selected["canonical_tree_sha256"] = candidate.tree_sha256
        contract_raw = _canonical(contract)
        manifest, artifacts = _package(contract=contract_raw)
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_runtime_postgresql_driver_not_ready",
        ):
            create_controller_runtime_build_plan(
                build_nonce="9" * 64,
                standalone_cpython_substrate_json=_substrate(),
                wheelhouse_artifacts=renamed,
                controller_runtime_contract_json=contract_raw,
                controller_requirements_lock=_synthetic_lock(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
            )

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
                standalone_cpython_substrate_json=_substrate(),
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
                standalone_cpython_substrate_json=_substrate(),
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
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=unsealed,
            )
        self.assertEqual(unsealed.calls[-1], "abandon_owned_stage")

        unflushed = _FakeTransport(plan)
        unflushed.publication_changes = {"receipt_parent_fsynced": False}
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_partial_publication_requires_review",
        ):
            execute_controller_runtime_build(
                plan,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=unflushed,
            )
        # Once publication begins, automatic deletion is forbidden.  A partial
        # final publication is left for explicit evidence-backed recovery.
        self.assertNotIn("abandon_owned_stage", unflushed.calls)

        malformed = _FakeTransport(plan)
        malformed.publication_changes = {"root_uid": True}
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_build_partial_publication_requires_review",
        ):
            execute_controller_runtime_build(
                plan,
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=malformed,
            )
        self.assertNotIn("abandon_owned_stage", malformed.calls)

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
                standalone_cpython_substrate_json=_substrate(),
                package_manifest_json=manifest,
                package_artifacts=artifacts,
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()
