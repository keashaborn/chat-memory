from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.governed_memory_install import controller, package, rollback
from tools.governed_memory_validation import (
    generate_installation_package_manifest,
    verify_store_migration_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


class DormantStoreInstallPackageTests(unittest.TestCase):
    def _verify_generated_manifest(self) -> dict[str, object]:
        manifest = generate_installation_package_manifest.generate()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "package_manifest.json"
            path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="ascii",
            )
            with patch.object(package, "MANIFEST", path):
                return package.verify()

    def test_current_members_verify_as_inactive_execution_package(self) -> None:
        receipt = self._verify_generated_manifest()
        self.assertEqual(receipt["schema_version"], (
            "governed-memory-dormant-store-install-package-verification-v6"
        ))
        self.assertEqual(receipt["artifact_count"], 76)
        self.assertEqual(len(package.EXPECTED_ARTIFACTS), 76)
        self.assertTrue(
            receipt["durable_install_and_rollback_journal_adapters_packaged"]
        )

        self.assertTrue(
            receipt["durable_resource_identity_ledger_and_anchor_packaged"]
        )
        self.assertTrue(receipt["guarded_synthetic_proof_harness_packaged"])
        self.assertFalse(receipt["synthetic_proof_executed_by_verifier"])
        self.assertFalse(
            receipt["synthetic_proof_receipt_present_at_package_sealing"]
        )
        self.assertTrue(
            receipt["authority_gated_disposable_linux_proof_runner_packaged"]
        )
        self.assertFalse(
            receipt["disposable_linux_proof_runner_executed_by_verifier"]
        )
        self.assertFalse(
            receipt[
                "disposable_linux_proof_receipt_present_at_package_sealing"
            ]
        )
        self.assertTrue(receipt["proof_receipt_is_external_to_package"])
        self.assertTrue(receipt["bounded_image_inspect_runner_primitive_packaged"])
        self.assertTrue(receipt["local_image_inspect_adapter_packaged"])
        self.assertTrue(
            receipt["controller_runtime_verification_capability_packaged"]
        )
        self.assertTrue(receipt["supervisor_launcher_source_packaged"])
        self.assertFalse(receipt["controller_runtime_built_or_installed"])
        for field in (
            "resolved_store_spec_and_exact_docker_labels_bound",
            "resource_identity_ledger_v2_packaged",
            "retained_audit_artifact_hashes_bound",
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_authority_marker_and_receipt",
        ):
            self.assertTrue(receipt[field], field)
        for field in (
            "closed_live_transport_contracts_packaged",
            "complete_closed_live_transport_substrate_set_packaged",
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory",
            "driver_native_postgresql_stage_contract_packaged",
            "driver_native_postgresql_executable_stage_machine_packaged",
            "concrete_psycopg_postgresql_transport_packaged",
            "runtime_input_selection_contract_repaired",
            "runtime_build_receipt_provenance_v4_packaged",
            "approved_terminal_postgresql_catalog_manifest_selected",
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required",
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence",
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented",
            "administrative_cooperative_writer_fence_implemented",
            "selected_live_platform_transports_packaged",
            "selected_non_postgresql_live_platform_transport_factory_packaged",
            "pinned_postgresql_driver_selected",
            "controller_runtime_build_transport_packaged",
            "controller_runtime_publication_policy_transport_packaged",
            "production_runtime_publication_primitives_packaged",
            "independent_standalone_cpython_payload_tree_proof_packaged",
        ):
            self.assertTrue(receipt[field], field)
        for field in (
            "equivalent_privileged_root_bypass_excluded",
            "stopped_store_semantic_empty_recheck_is_valid",
        ):
            self.assertFalse(receipt[field], field)
        self.assertTrue(
            receipt["claim_bound_install_controller_composition_packaged"]
        )
        self.assertTrue(
            receipt[
                "install_receipt_binds_fresh_terminal_canonical_store_readiness"
            ]
        )
        self.assertTrue(
            receipt[
                "empty_rollback_requires_opaque_verified_install_receipt_and_ledger"
            ]
        )
        self.assertTrue(
            receipt[
                "completed_install_and_empty_rollback_replay_reverification_packaged"
            ]
        )
        self.assertTrue(
            receipt["claim_bound_empty_rollback_controller_composition_packaged"]
        )
        self.assertTrue(receipt["closed_install_store_effect_adapter_packaged"])
        self.assertTrue(receipt["closed_empty_rollback_store_effect_adapter_packaged"])
        self.assertTrue(receipt["selected_live_platform_transports_packaged"])
        self.assertTrue(receipt["pinned_postgresql_driver_selected"])
        self.assertTrue(receipt["durable_create_once_receipt_store_packaged"])
        contract = json.loads(package.CONTRACT.read_text(encoding="utf-8"))
        receipts = contract["receipt_policy"]
        self.assertTrue(
            receipts[
                "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held"
            ]
        )
        self.assertTrue(
            receipts[
                "empty_rollback_receipt_persistence_create_once_while_controller_authority_marker_held_required"
            ]
        )
        self.assertTrue(
            receipt["controller_runtime_release_builder_orchestration_packaged"]
        )
        self.assertTrue(receipt["controller_runtime_build_transport_packaged"])
        self.assertTrue(
            receipt["operation_specific_install_and_empty_rollback_receipts_packaged"]
        )
        self.assertTrue(receipt["install_controller_emits_canonical_receipt"])
        self.assertTrue(receipt["empty_rollback_controller_emits_canonical_receipt"])
        self.assertFalse(receipt["activation_entrypoint_packaged"])
        self.assertTrue(receipt["stores_supervisor_cli_packaged"])
        self.assertEqual(
            receipt["stores_supervisor_cli_docker_surface"],
            ["container_inspect", "container_start", "container_stop"],
        )
        self.assertFalse(receipt["installation_performed_by_verifier"])
        self.assertFalse(receipt["images_staged_by_verifier"])
        self.assertFalse(receipt["secrets_touched_by_verifier"])
        self.assertFalse(receipt["activation_performed_by_verifier"])
        self.assertEqual(
            receipt["contract_canonical_sha256"],
            package.EXPECTED_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["plan_canonical_sha256"],
            package.EXPECTED_PLAN_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["controller_model_sha256"],
            package.EXPECTED_CONTROLLER_MODEL_SHA256,
        )
        self.assertEqual(
            receipt["controller_source_sha256"],
            package.EXPECTED_CONTROLLER_SOURCE_SHA256,
        )
        self.assertEqual(
            receipt["execution_contract_canonical_sha256"],
            package.EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["controller_runtime_contract_canonical_sha256"],
            package.EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["postgres_native_stage_contract_canonical_sha256"],
            package.EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["proof_contract_canonical_sha256"],
            package.EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["synthetic_proof_receipt_schema_canonical_sha256"],
            package.EXPECTED_SYNTHETIC_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["live_proof_receipt_schema_canonical_sha256"],
            package.EXPECTED_LIVE_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["install_receipt_schema_canonical_sha256"],
            package.EXPECTED_INSTALL_RECEIPT_SCHEMA_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["empty_rollback_receipt_schema_canonical_sha256"],
            package.EXPECTED_EMPTY_ROLLBACK_RECEIPT_SCHEMA_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["migration_verifier_source_sha256"],
            package.EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256,
        )
        artifacts = set(receipt["artifact_sha256"])
        self.assertEqual(artifacts, package.EXPECTED_ARTIFACTS)
        self.assertEqual(
            json.loads(
                package.MANIFEST.read_text(encoding="utf-8")
            )["schema_version"],
            "governed-memory-dormant-store-install-inactive-execution-"
            "package-manifest-v6",
        )
        self.assertEqual(
            receipt["state"],
            "phase9j-install-ready-closed-runtime-and-store-transports-packaged-not-installed-not-activated",
        )
        self.assertNotIn(
            "governed-memory-migrations/schema_contract.json",
            artifacts,
        )
        for forbidden in package.FORBIDDEN_ARTIFACT_MARKERS:
            self.assertFalse(any(forbidden in path for path in artifacts))

    def test_current_installation_and_controller_directories_are_closed(self) -> None:
        package._verify_current_directory_closure()

    def test_retired_disposable_proof_authority_module_is_absent(self) -> None:
        retired = (
            ROOT
            / "tools/governed_memory_validation/phase9_disposable_proof_authority.py"
        )
        self.assertFalse(retired.exists())
        self.assertNotIn(
            retired.relative_to(ROOT).as_posix(),
            package.EXPECTED_ARTIFACTS,
        )
        self.assertEqual(
            package._repository_regular_file_inventory(
                "ops/governed_memory/installation"
            ),
            package.EXPECTED_INSTALLATION_DIRECTORY_FILES,
        )
        self.assertEqual(
            package._repository_regular_file_inventory(
                "tools/governed_memory_install"
            ),
            package.EXPECTED_INSTALL_TOOL_DIRECTORY_FILES,
        )

    def test_image_authority_is_in_memory_and_capsule_embeds_trust(self) -> None:
        contract = json.loads(package.CONTRACT.read_text(encoding="ascii"))
        targets = contract["exact_targets"]
        filesystem = contract["filesystem_policy"]
        images = contract["image_policy"]
        authority = contract["authority_policy"]
        receipts = contract["receipt_policy"]

        self.assertNotIn("image_receipt", targets)
        self.assertNotIn("trust_anchor", targets)
        self.assertEqual(
            targets["recovery_capsule"],
            "/var/lib/governed-memory-controller/phase9-disposable-proof-recovery-capsule-v4.json",
        )
        self.assertEqual(
            targets["nonce_state"],
            "/var/lib/governed-memory-controller/authority-state-v3.sqlite3",
        )
        self.assertEqual(
            targets["proof_supervision_lock"],
            "/run/lock/governed-memory-controller/phase9-disposable-live-proof.lock",
        )
        self.assertNotIn("trust_anchor", filesystem)
        self.assertEqual(
            filesystem["recovery_capsule"],
            {
                "owner": "root:root",
                "mode": "0400",
                "external_or_persistent_hard_link_allowed": False,
                "fixed_publication_temp_suffix": ".publishing",
                "transient_same_inode_publication_hard_link_allowed": True,
                "terminal_link_count": 1,
                "interrupted_same_inode_link_count_two_reconciled_before_execution": True,
                "no_replace_publication_required": True,
                "partial_different_inode_or_metadata_drift_refused": True,
                "create_once": True,
                "embeds_trust_bundle": True,
                "symlink_allowed": False,
                "retained_after_terminal_observation": True,
                "file_and_parent_fsync_required": True,
            },
        )
        self.assertEqual(
            filesystem["proof_supervision_lock"],
            {
                "owner": "root:root",
                "mode": "0600",
                "hard_link_allowed": False,
                "symlink_allowed": False,
                "link_count": 1,
                "empty_file_required": True,
                "parent_owner": "root:root",
                "parent_mode": "0700",
                "issuer_creates_if_absent_or_securely_reopens": True,
                "never_unlinked": True,
                "nonblocking_exclusive_lock_required": True,
                "held_for_full_run_or_recover_lifecycle": True,
                "inherited_through_sealed_runner_process_group": True,
                "retained_after_terminal_observation": True,
            },
        )
        for retired in (
            "authority_substrate_must_preexist_execution",
            "image_staging_substrate_must_preexist_execution",
            "package_may_create_authority_substrate",
            "package_may_create_image_staging_substrate",
        ):
            self.assertNotIn(retired, filesystem)
        self.assertFalse(filesystem["persistent_authority_key_file_required"])
        self.assertFalse(filesystem["persistent_image_staging_substrate_required"])
        self.assertTrue(
            filesystem["proof_substrate_directories_must_preexist_execution"]
        )
        self.assertTrue(
            filesystem[
                "proof_runner_validates_but_never_creates_chmods_or_chowns_substrate_directories"
            ]
        )
        self.assertEqual(images["pull_policy"], "never")
        self.assertTrue(
            images["exact_local_repo_digest_and_image_id_reinspection_required"]
        )
        self.assertTrue(images["canonical_image_identity_set_constructed_in_memory"])
        self.assertFalse(images["persistent_image_staging_receipt_required"])
        self.assertTrue(authority["recovery_capsule_path_is_fixed"])
        self.assertTrue(
            authority["recovery_capsule_root_owned_mode_0400_required"]
        )
        self.assertTrue(
            authority["trust_bundle_is_embedded_in_recovery_capsule"]
        )
        self.assertFalse(authority["persistent_trusted_owner_key_file_required"])
        self.assertTrue(
            authority["signed_public_pre_effect_recovery_delegation_required"]
        )
        self.assertTrue(
            authority[
                "durable_recovery_reservation_claim_required_before_first_install_effect"
            ]
        )
        self.assertFalse(authority["ephemeral_private_signer_retained_for_execution"])
        recovery = contract["recovery_policy"]
        preauthorized_rollback = (
            "postflight_empty_rollback_requires_distinct_pre_signed_"
            "recovery_delegation_and_preclaimed_reservation"
        )
        self.assertTrue(recovery[preauthorized_rollback])
        self.assertEqual(
            authority["host_clock_synchronization_preflight_binary"],
            "/usr/bin/timedatectl",
        )
        self.assertEqual(
            authority["host_clock_synchronization_preflight_arguments"],
            ["show", "--property=NTPSynchronized", "--value"],
        )
        self.assertEqual(
            authority["host_clock_synchronization_preflight_exact_stdout"],
            "yes\n",
        )
        self.assertTrue(
            authority[
                "host_clock_synchronization_preflight_runs_before_substrate_validation"
            ]
        )
        self.assertTrue(
            receipts[
                "install_receipt_binds_canonical_in_memory_image_identity_set_sha256"
            ]
        )
        self.assertTrue(
            receipts[
                "live_proof_receipt_binds_canonical_in_memory_image_identity_set_sha256"
            ]
        )
        self.assertTrue(
            receipts[
                "live_proof_receipt_binds_host_clock_synchronization_preflight_passed"
            ]
        )
        blockers = contract["remaining_blockers"]
        self.assertNotIn("trusted_authority_substrate_not_installed", blockers)
        self.assertNotIn("exact_local_image_identity_receipt_not_published", blockers)
        self.assertIn(
            "durable_recovery_capsule_and_pre_effect_reservation_not_live_executed",
            blockers,
        )
        self.assertIn(
            "exact_local_image_digest_and_id_reinspection_pending_for_disposable_live_proof",
            blockers,
        )

    def test_directory_closure_rejects_unlisted_file_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installation = root / "ops/governed_memory/installation"
            tools = root / "tools/governed_memory_install"
            installation.mkdir(parents=True)
            tools.mkdir(parents=True)
            (installation / "stale.py").write_text("stale\n", encoding="ascii")
            (tools / "current.py").write_text("current\n", encoding="ascii")
            with (
                patch.object(package, "ROOT", root),
                patch.object(
                    package,
                    "EXPECTED_INSTALLATION_DIRECTORY_FILES",
                    frozenset(),
                ),
                patch.object(
                    package,
                    "EXPECTED_INSTALL_TOOL_DIRECTORY_FILES",
                    frozenset({"tools/governed_memory_install/current.py"}),
                ),
                self.assertRaisesRegex(
                    package.PackageError,
                    "directory_closure_invalid",
                ),
            ):
                package._verify_current_directory_closure()

            (installation / "stale.py").unlink()
            (installation / "linked.py").symlink_to(tools / "current.py")
            with (
                patch.object(package, "ROOT", root),
                self.assertRaisesRegex(
                    package.PackageError,
                    "directory_inventory_invalid",
                ),
            ):
                package._repository_regular_file_inventory(
                    "ops/governed_memory/installation"
                )

    def test_checked_in_manifest_is_exact_generated_manifest(self) -> None:
        checked_in = json.loads(
            package.MANIFEST.read_text(encoding="ascii")
        )
        self.assertEqual(checked_in, generate_installation_package_manifest.generate())
        self.assertEqual(package.verify()["artifact_count"], 76)

    def test_postgres_runtime_driver_probe_is_exact_and_cross_bound(self) -> None:
        native = json.loads(
            (ROOT / package.POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE).read_text(
                encoding="ascii"
            )
        )
        runtime = json.loads(
            (ROOT / package.CONTROLLER_RUNTIME_CONTRACT_RELATIVE).read_text(
                encoding="ascii"
            )
        )
        contract = json.loads(package.CONTRACT.read_text(encoding="ascii"))
        preferred = native["preferred_driver"]
        probe = preferred["runtime_driver_probe"]
        identity = hashlib.sha256(
            json.dumps(
                probe,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        self.assertEqual(
            identity,
            preferred["runtime_driver_identity_sha256"],
        )
        self.assertEqual(
            identity,
            runtime["selected_runtime_inputs"]["postgresql_driver"][
                "runtime_driver_identity_sha256"
            ],
        )
        self.assertEqual(
            identity,
            contract["migration_policy"][
                "runtime_postgresql_driver_identity_sha256"
            ],
        )
        for mutate in (
            lambda value: value["native_files"][0].__setitem__("size", 1),
            lambda value: value["native_files"][0].__setitem__(
                "sha256", "0" * 64
            ),
            lambda value: value["native_files"].reverse(),
            lambda value: value["native_files"].append(
                copy.deepcopy(value["native_files"][0])
            ),
        ):
            changed = copy.deepcopy(native)
            mutate(changed["preferred_driver"]["runtime_driver_probe"])
            with self.assertRaisesRegex(
                package.PackageError,
                "dormant_store_postgres_native_stage_contract_invalid",
            ):
                package._verify_postgres_native_stage_contract(changed)

    def test_verifier_generator_and_local_migration_binding_are_hash_bound(
        self,
    ) -> None:
        generated = generate_installation_package_manifest.generate()
        artifacts = generated["artifacts"]
        required = {
            "ops/governed_memory/installation/current/controller_runtime_contract.json",
            "ops/governed_memory/installation/current/disposable_proof_contract.json",
            "ops/governed_memory/installation/current/disposable_proof_receipt.schema.json",
            "ops/governed_memory/installation/current/live_proof_receipt.schema.json",
            "ops/governed_memory/installation/current/install_receipt.schema.json",
            "ops/governed_memory/installation/current/empty_rollback_receipt.schema.json",
            "ops/governed_memory/installation/current/execution_contract.json",
            "tools/governed_memory_install/package.py",
            "tools/governed_memory_install/disposable_proof_harness.py",
            "tools/governed_memory_install/journal.py",
            "tools/governed_memory_install/install_backend.py",
            "tools/governed_memory_install/install_entrypoint.py",
            "tools/governed_memory_install/execution_capability.py",
            "tools/governed_memory_install/package_capability.py",
            "tools/governed_memory_install/controller_runtime.py",
            "tools/governed_memory_install/receipts.py",
            "tools/governed_memory_install/rollback.py",
            "tools/governed_memory_install/rollback_authority.py",
            "tools/governed_memory_install/rollback_entrypoint.py",
            "tools/governed_memory_install/rollback_journal.py",
            "tools/governed_memory_install/secure_file.py",
            "tools/governed_memory_install/store_readiness.py",
            "tools/governed_memory_install/store_supervisor_launcher.py",
            "tools/governed_memory_install/synthetic_backend.py",
            "tools/governed_memory_install/authority.py",
            "tools/governed_memory_validation/generate_installation_package_manifest.py",
            "tools/governed_memory_validation/run_installation_synthetic_proof.py",
            "tools/governed_memory_validation/run_disposable_installation_live_proof.py",
            "tools/governed_memory_validation/verify_store_migration_manifest.py",
            "ops/governed_memory/installation/current/postgres/migration_bindings.json",
            "ops/governed_memory/installation/current/postgres/native_stage_contract.json",
            "tools/governed_memory_install/linux_live_adapters.py",
            "tools/governed_memory_install/live_rollback_marker.py",
            "tools/governed_memory_install/postgres_native_stages.py",
            "tools/governed_memory_install/linux_live_transports.py",
            "tools/governed_memory_install/psycopg_postgres_adapter.py",
            "tools/governed_memory_release/inspect_standalone_cpython.py",
            "tools/governed_memory_release/linux_runtime_publication_primitives.py",
            "tools/governed_memory_release/runtime_input_stager.py",
            "tools/governed_memory_release/runtime_publication_transport.py",
            "tools/governed_memory_validation/durable_live_proof_receipt.py",
            "tools/governed_memory_validation/process_death_arm_receipt.py",
        }
        self.assertTrue(required.issubset(artifacts))
        for historical in (
            "governed-memory-migrations/0001_foundation/package.json",
            "governed-memory-migrations/0003_owner_claim_detail/package.json",
            "governed-memory-migrations/0004_pilot_marker/package.json",
        ):
            self.assertNotIn(historical, artifacts)

    def test_imported_local_package_initializers_are_manifest_bound(self) -> None:
        required: set[str] = set()
        for relative in package.EXPECTED_ARTIFACTS:
            if not relative.endswith(".py"):
                continue
            parent = PurePosixPath(relative).parent
            while parent != PurePosixPath("."):
                initializer = parent / "__init__.py"
                if (ROOT / initializer.as_posix()).is_file():
                    required.add(initializer.as_posix())
                parent = parent.parent
        self.assertEqual(
            required,
            {
                "tools/governed_memory_install/__init__.py",
                "tools/governed_memory_release/__init__.py",
            },
        )
        self.assertTrue(required.issubset(package.EXPECTED_ARTIFACTS))

    def test_retired_modules_are_absent_and_historical_manifest_is_unbound(
        self,
    ) -> None:
        old_manifest = ROOT / "ops/governed_memory/installation/package_manifest.json"
        self.assertNotIn(
            str(old_manifest.relative_to(ROOT)),
            package.EXPECTED_ARTIFACTS,
        )
        retired = {
            "tools/governed_memory_install/authority_v2.py",
            "tools/governed_memory_install/controller_linux.py",
            "tools/governed_memory_install/controller_v2.py",
            "tools/governed_memory_install/durable_journal_v2.py",
            "tools/governed_memory_install/execution_capability_v2.py",
            "tools/governed_memory_install/inactive_installation.py",
            "tools/governed_memory_install/package_v3.py",
            "tools/governed_memory_install/postgres_source_closure.py",
            "tools/governed_memory_install/synthetic_backend_v2.py",
            "ops/governed_memory/installation/current/postgres/source_closure_contract.json",
        }
        canonical = {
            "tools/governed_memory_install/authority.py",
            "tools/governed_memory_install/controller.py",
            "tools/governed_memory_install/execution_capability.py",
            "tools/governed_memory_install/journal.py",
            "tools/governed_memory_install/package.py",
            "tools/governed_memory_install/psycopg_postgres_adapter.py",
            "tools/governed_memory_install/synthetic_backend.py",
            "tools/governed_memory_release/inspect_standalone_cpython.py",
            "tools/governed_memory_release/linux_runtime_publication_primitives.py",
            "tools/governed_memory_release/runtime_input_stager.py",
        }
        self.assertTrue(canonical.issubset(package.EXPECTED_ARTIFACTS))
        self.assertTrue(retired.isdisjoint(package.EXPECTED_ARTIFACTS))
        for relative in retired:
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_manifest_contains_no_versioned_successor_module_paths(self) -> None:
        self.assertFalse(
            any(
                "_v2.py" in path or "_v3.py" in path
                for path in package.EXPECTED_ARTIFACTS
            )
        )

    def test_manifest_rejects_extra_forbidden_or_hash_tampering(self) -> None:
        manifest = generate_installation_package_manifest.generate()
        cases = []
        extra = copy.deepcopy(manifest)
        extra["artifacts"][
            "governed-memory-migrations/0002_conversation_bridge/forward.pgsql"
        ] = "0" * 64
        cases.append(extra)
        historical = copy.deepcopy(manifest)
        historical["artifacts"][
            "governed-memory-migrations/0001_foundation/package.json"
        ] = "0" * 64
        cases.append(historical)
        bad_hash = copy.deepcopy(manifest)
        key = next(iter(bad_hash["artifacts"]))
        bad_hash["artifacts"][key] = "0" * 64
        cases.append(bad_hash)
        for candidate in cases:
            with self.subTest(candidate=len(candidate["artifacts"])):
                with self.assertRaises(package.PackageError):
                    package._verify_manifest(candidate)

    def test_contract_and_plan_are_exact_closed_and_controller_bound(self) -> None:
        contract = json.loads(package.CONTRACT.read_text(encoding="utf-8"))
        package._verify_contract(contract)
        contract["scope"]["current_phase_installs_or_activates"] = True
        with self.assertRaisesRegex(
            package.PackageError,
            "contract_semantics_invalid",
        ):
            package._verify_contract(contract)

        contract = json.loads(package.CONTRACT.read_text(encoding="utf-8"))
        contract["scope"]["unexpected"] = False
        with self.assertRaisesRegex(
            package.PackageError,
            "contract_semantics_invalid",
        ):
            package._verify_contract(contract)

        plan = json.loads(package.PLAN.read_text(encoding="utf-8"))
        package._verify_plan(plan)
        model_hash = package._verify_controller_binding(plan, controller)
        self.assertEqual(model_hash, package.EXPECTED_CONTROLLER_MODEL_SHA256)
        self.assertEqual(
            tuple(step["id"] for step in plan["empty_rollback_steps"]),
            tuple(step.step_id for step in rollback.EMPTY_ROLLBACK_STEPS),
        )
        plan["live_execution"]["install_command_exposed"] = True
        with self.assertRaisesRegex(
            package.PackageError,
            "plan_semantics_invalid",
        ):
            package._verify_plan(plan)

        plan = json.loads(package.PLAN.read_text(encoding="utf-8"))
        plan["install_steps"][0]["effect"] = "different_but_well_formed_effect"
        with self.assertRaisesRegex(
            package.PackageError,
            "controller_plan_binding_invalid",
        ):
            package._verify_controller_binding(plan, controller)

    def test_verified_controller_loader_is_dependency_closed(self) -> None:
        controller = package._load_verified_controller_module(
            package.CONTROLLER_MODEL_RELATIVE,
            expected_sha256=package.artifact_sha256(
                package.CONTROLLER_MODEL_RELATIVE
            ),
        )
        projection = controller.plan_install_steps_projection(
            controller.STORES_ONLY_PLAN
        )
        self.assertEqual(len(projection), 19)
        self.assertEqual(
            tuple(step["id"] for step in projection),
            package.EXPECTED_CONTROLLER_STEP_IDS,
        )
        self.assertRegex(
            controller.validate_plan(controller.STORES_ONLY_PLAN),
            r"[0-9a-f]{64}\Z",
        )
        with self.assertRaisesRegex(
            controller.ExecutionLockError,
            "verifier_lock_surface_unavailable",
        ):
            controller.validate_held_execution_lock(object())

    def test_repository_reader_rejects_symlink_file_and_directory_components(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe"
            safe.mkdir()
            member = safe / "member"
            member.write_bytes(b"member")
            (root / "directory-link").symlink_to(safe, target_is_directory=True)
            (safe / "file-link").symlink_to(member)
            with patch.object(package, "ROOT", root):
                self.assertEqual(
                    package.artifact_sha256("safe/member"),
                    "e31ab643c44f7a0ec824b59d1194d60dac334200d845e61d2d289daa0f087ea4",
                )
                for relative in (
                    "directory-link/member",
                    "safe/file-link",
                ):
                    with self.subTest(relative=relative):
                        with self.assertRaisesRegex(
                            package.PackageError,
                            "artifact_path_invalid",
                        ):
                            package.artifact_sha256(relative)

    def test_migration_verifier_binds_local_descriptor_and_excludes_history(
        self,
    ) -> None:
        receipt = verify_store_migration_manifest.verify()
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-dormant-store-install-store-migration-verification-v3",
        )
        self.assertEqual(
            receipt["state"],
            "repository-only-current-stores-only-migration-set-not-installed-not-authorized",
        )
        self.assertEqual(receipt["file_count"], 9)
        self.assertNotIn("schema_contract.json", receipt["artifact_sha256"])
        self.assertEqual(receipt["historical_package_descriptor_count"], 0)
        self.assertEqual(
            receipt["migration_bindings_canonical_sha256"],
            verify_store_migration_manifest.EXPECTED_BINDINGS_CANONICAL_SHA256,
        )
        bindings = verify_store_migration_manifest._load(
            verify_store_migration_manifest.BINDINGS_RELATIVE
        )
        tampered_observed = dict(receipt["artifact_sha256"])
        tampered_observed["0001_foundation/package.json"] = "0" * 64
        with self.assertRaisesRegex(
            verify_store_migration_manifest.StoreMigrationManifestError,
            "historical_package_descriptor",
        ):
            verify_store_migration_manifest._verify_bindings(
                bindings,
                tampered_observed,
            )

    def test_package_rejects_nested_migration_receipt_drift(self) -> None:
        observed = generate_installation_package_manifest.generate()["artifacts"]
        receipt = verify_store_migration_manifest.verify()
        accepted = package._verify_migration_binding(
            observed,
            SimpleNamespace(verify=lambda: copy.deepcopy(receipt)),
        )
        self.assertEqual(
            accepted["manifest_sha256"],
            observed[package.MIGRATION_MANIFEST_RELATIVE],
        )

        bad_manifest = copy.deepcopy(receipt)
        bad_manifest["manifest_sha256"] = "0" * 64
        bad_child = copy.deepcopy(receipt)
        child = next(iter(bad_child["artifact_sha256"]))
        bad_child["artifact_sha256"][child] = "0" * 64
        extra = copy.deepcopy(receipt)
        extra["unexpected"] = False
        for drifted in (bad_manifest, bad_child, extra):
            with self.subTest(drifted=set(drifted)):
                with self.assertRaises(package.PackageError):
                    package._verify_migration_binding(
                        observed,
                        SimpleNamespace(verify=lambda value=drifted: value),
                    )

    def test_migration_reader_rejects_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe"
            safe.mkdir()
            (safe / "member").write_bytes(b"member")
            (root / "link").symlink_to(safe, target_is_directory=True)
            with (
                patch.object(verify_store_migration_manifest, "MIGRATIONS", root),
                self.assertRaisesRegex(
                    verify_store_migration_manifest.StoreMigrationManifestError,
                    "manifest_path_invalid",
                ),
            ):
                verify_store_migration_manifest._read_checked("link/member")

    def test_migration_rejects_unapproved_paths_before_reading_them(self) -> None:
        manifest_path = ROOT / verify_store_migration_manifest.MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(
            {"path": "unapproved/repository/member", "sha256": "0" * 64}
        )
        raw = json.dumps(manifest).encode("ascii")

        def controlled_read(relative: str) -> bytes:
            if relative == verify_store_migration_manifest.MANIFEST_RELATIVE:
                return raw
            self.fail("unapproved manifest was used as a repository read list")

        with (
            patch.object(
                verify_store_migration_manifest,
                "_read_checked",
                side_effect=controlled_read,
            ),
            self.assertRaisesRegex(
                verify_store_migration_manifest.StoreMigrationManifestError,
                "manifest_file_set_invalid",
            ),
        ):
            verify_store_migration_manifest.verify()

    def test_dormant_store_install_preflight_is_fail_closed_without_numeric_quit(self) -> None:
        dormant_store_install = (
            ROOT
            / "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
        ).read_text(encoding="utf-8")
        self.assertIn("\\set ON_ERROR_STOP on", dormant_store_install)
        self.assertIn("ERRCODE = '22023'", dormant_store_install)
        self.assertNotIn("\\quit", dormant_store_install)


if __name__ == "__main__":
    unittest.main()
