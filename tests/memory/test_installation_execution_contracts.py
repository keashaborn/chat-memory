from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CURRENT = ROOT / "ops/governed_memory/installation/current"


class DormantStoreInstallExecutionContractTests(unittest.TestCase):
    def test_execution_contract_is_repository_only_closed_and_truthful(self) -> None:
        contract = json.loads(
            (CURRENT / "execution_contract.json").read_text(encoding="ascii")
        )
        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "state",
                "server",
                "package_manifest_relationship",
                "authority_sequences",
                "execution_boundary",
                "binding_policy",
                "durability_policy",
                "host_action_policy",
                "receipt_policy",
                "proof_boundary",
                "remaining_blockers",
            },
        )
        self.assertEqual(
            contract["schema_version"],
            "governed-memory-dormant-store-install-inactive-execution-package-v2",
        )
        self.assertEqual(contract["server"], "seebx")
        relationship = contract["package_manifest_relationship"]
        self.assertTrue(
            relationship["this_contract_is_hash_bound_by_package_manifest"]
        )
        self.assertFalse(relationship["contract_embeds_package_manifest_hash"])
        self.assertFalse(relationship["circular_self_hash_claimed"])

        boundary = contract["execution_boundary"]
        self.assertFalse(boundary["approval_authorizes_execution"])
        self.assertTrue(
            boundary["claim_bound_install_controller_composition_callable_non_cli"]
        )
        self.assertTrue(
            boundary[
                "claim_bound_empty_rollback_controller_composition_callable_non_cli"
            ]
        )
        self.assertFalse(
            boundary["concrete_live_install_store_effect_adapter_implemented"]
        )
        self.assertFalse(
            boundary[
                "concrete_live_empty_rollback_store_effect_adapter_implemented"
            ]
        )
        self.assertFalse(boundary["activation_executor_implemented"])
        for key, value in boundary.items():
            if key.endswith("_in_this_repository_phase"):
                self.assertEqual(value, 0)

        binding = contract["binding_policy"]
        self.assertTrue(binding["opaque_verified_package_capability_required"])
        self.assertTrue(
            binding["opaque_claimed_install_execution_capability_required"]
        )
        self.assertTrue(
            binding["opaque_claimed_empty_rollback_capability_required"]
        )
        self.assertTrue(
            binding["opaque_verified_controller_runtime_capability_required"]
        )
        self.assertTrue(
            binding["resolved_store_spec_hash_and_exact_docker_label_hashes_bound"]
        )
        self.assertTrue(binding["exact_controller_process_is_trusted"])
        self.assertFalse(
            binding["hostile_same_process_capability_forgery_resisted"]
        )

        durability = contract["durability_policy"]
        for key in (
            "install_and_rollback_journal_anchors_packaged",
            "per_execution_resource_ledger_anchor_packaged",
            "filesystem_identity_guards_packaged",
            "one_final_partial_json_line_beyond_exact_anchor_recoverable",
            "same_open_instance_inode_and_directory_replacement_refused",
            "cross_process_same_content_inode_or_directory_replacement_refused",
            "compensation_effect_before_receipt_resume_supported",
            "terminal_postflight_effect_present_blocks_compensation_and_exact_resume_completes",
            "composite_step_recoverable_state_protocol_implemented",
            "empty_rollback_writer_fence_held_from_r06_through_receipt",
            "destructive_rollback_steps_atomically_recheck_empty_under_fence",
            "retained_audit_artifact_hashes_bound_to_rollback_receipt",
        ):
            self.assertTrue(durability[key], key)
        self.assertFalse(durability["end_to_end_live_process_crash_recovery_claimed"])

        host = contract["host_action_policy"]
        self.assertTrue(host["typed_operation_specific_boundaries_only"])
        self.assertFalse(host["arbitrary_store_mutation_argv_surface"])
        self.assertFalse(host["concrete_store_effect_adapters_packaged"])
        self.assertTrue(host["controller_runtime_secure_verifier_packaged"])
        self.assertTrue(host["exact_release_path_supervisor_launcher_packaged"])
        receipts = contract["receipt_policy"]
        self.assertTrue(receipts["install_and_empty_rollback_use_distinct_schemas"])
        self.assertTrue(receipts["closed_content_free_builders_and_verifiers_packaged"])
        self.assertTrue(receipts["empty_rollback_controller_emits_canonical_receipt"])
        self.assertTrue(receipts["install_controller_emits_canonical_receipt"])
        self.assertNotIn(
            "canonical_install_receipt_emission_not_integrated",
            contract["remaining_blockers"],
        )

        proof = contract["proof_boundary"]
        self.assertEqual(proof["harness_type"], "guarded-synthetic-only")
        for key in (
            "live_linux_proof",
            "docker_or_systemd_proof",
            "postgresql_or_qdrant_execution_proof",
            "kernel_confinement_proof",
        ):
            self.assertFalse(proof[key])

    def test_controller_runtime_is_separate_locked_and_unbuilt(self) -> None:
        runtime = json.loads(
            (CURRENT / "controller_runtime_contract.json").read_text(
                encoding="ascii"
            )
        )
        self.assertEqual(
            runtime["schema_version"],
            "governed-memory-dormant-store-install-controller-runtime-contract-v2",
        )
        dependency = runtime["dependency_policy"]
        build = runtime["build_policy"]
        verification = runtime["verification_policy"]
        entrypoint = runtime["entrypoint_policy"]
        self.assertEqual(
            dependency["lock_file"],
            "ops/governed_memory/controller-requirements.lock",
        )
        self.assertTrue(dependency["require_hashes"])
        self.assertFalse(dependency["network_install_allowed"])
        self.assertFalse(dependency["reuse_successor_application_runtime"])
        self.assertFalse(dependency["reuse_system_python_site_packages"])
        self.assertFalse(build["current_runtime_built"])
        self.assertFalse(build["current_runtime_installed"])
        self.assertFalse(build["runtime_build_or_install_performed_by_repository_phase"])
        for key in (
            "secure_receipt_bound_no_follow_verifier_packaged",
            "runtime_and_release_tree_hash_verification_required",
            "interpreter_prefix_import_stdlib_and_site_paths_confined_to_runtime_root",
            "interpreter_path_facts_sha256_required",
        ):
            self.assertTrue(verification[key], key)
        self.assertFalse(
            verification["controller_runtime_verifier_executed_in_current_phase"]
        )
        self.assertFalse(verification["current_runtime_build_receipt_present"])
        self.assertFalse(entrypoint["repository_source_execution_allowed"])
        self.assertFalse(entrypoint["unisolated_module_search_path_allowed"])
        self.assertFalse(entrypoint["module_search_launcher_allowed"])
        self.assertTrue(entrypoint["exact_release_path_supervisor_launcher_packaged"])
        self.assertTrue(entrypoint["systemd_execstart_must_bind_runtime_receipt"])
        self.assertTrue(entrypoint["claim_bound_install_composition_module_packaged"])
        self.assertTrue(
            entrypoint["claim_bound_empty_rollback_composition_module_packaged"]
        )
        self.assertFalse(entrypoint["install_or_empty_rollback_cli_included"])
        self.assertFalse(entrypoint["activation_entrypoint_included"])
        self.assertTrue(entrypoint["exact_controller_process_is_trusted"])
        self.assertFalse(
            entrypoint["opaque_python_capabilities_resist_hostile_same_process_code"]
        )

        lock = ROOT / dependency["lock_file"]
        raw = lock.read_bytes()
        self.assertRegex(hashlib.sha256(raw).hexdigest(), r"^[0-9a-f]{64}$")
        text = raw.decode("ascii")
        self.assertIn("cryptography==49.0.0", text)
        self.assertNotIn("fastapi", text)
        self.assertNotIn("asyncpg", text)

    def test_operation_specific_receipt_schemas_are_closed_and_distinct(self) -> None:
        install = json.loads(
            (CURRENT / "install_receipt.schema.json").read_text(encoding="ascii")
        )
        rollback = json.loads(
            (CURRENT / "empty_rollback_receipt.schema.json").read_text(
                encoding="ascii"
            )
        )
        for schema in (install, rollback):
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["type"], "object")
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertNotEqual(install["$id"], rollback["$id"])
        self.assertEqual(
            install["properties"]["operation"]["const"],
            "dormant_store_install",
        )
        self.assertEqual(
            rollback["properties"]["operation"]["const"],
            "empty_store_rollback",
        )
        self.assertEqual(rollback["properties"]["removed_resource_count"]["const"], 15)


if __name__ == "__main__":
    unittest.main()
