from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
PHASE8B = ROOT / "ops/governed_memory/installation/phase8b"


class Phase8BExecutionContractTests(unittest.TestCase):
    def test_execution_contract_is_inactive_closed_and_truthful(self) -> None:
        contract = json.loads(
            (PHASE8B / "execution_contract.json").read_text(encoding="ascii")
        )
        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "state",
                "server",
                "base_package_manifest_sha256",
                "authority_sequence",
                "execution_boundary",
                "binding_policy",
                "durability_policy",
                "host_action_policy",
                "proof_boundary",
                "remaining_blockers",
            },
        )
        self.assertEqual(contract["server"], "seebx")
        self.assertEqual(
            contract["base_package_manifest_sha256"],
            "907bab6516029418314611556e37423ef65c77e52afc48199dce43903bfb1607",
        )
        boundary = contract["execution_boundary"]
        self.assertFalse(boundary["approval_authorizes_execution"])
        self.assertFalse(boundary["live_entrypoint_callable_but_not_cli_exposed"])
        self.assertFalse(boundary["installation_executor_implemented"])
        self.assertFalse(boundary["rollback_executor_implemented"])
        self.assertFalse(boundary["activation_executor_implemented"])
        for key, value in boundary.items():
            if key.endswith("_in_this_phase"):
                self.assertEqual(value, 0)
        durability = contract["durability_policy"]
        self.assertTrue(durability["compensation_effect_before_receipt_resume_supported"])
        self.assertTrue(
            durability["same_open_instance_inode_and_directory_replacement_refused"]
        )
        self.assertFalse(
            durability[
                "cross_process_same_content_inode_or_directory_replacement_refused"
            ]
        )
        self.assertFalse(durability["terminal_seal_recovery_implemented"])
        self.assertFalse(durability["composite_step_crash_recovery_implemented"])
        self.assertFalse(durability["end_to_end_crash_recovery_claimed"])
        binding = contract["binding_policy"]
        self.assertFalse(binding["opaque_verified_package_capability_required"])
        self.assertTrue(binding["opaque_claimed_execution_capability_required"])
        self.assertFalse(binding["resource_identity_ledger_content_or_head_bound"])
        self.assertIn(
            "opaque_verified_package_capability_and_claim_boundary_not_implemented",
            contract["remaining_blockers"],
        )
        self.assertIn(
            "cross_process_durable_file_identity_or_equivalent_seal_not_implemented",
            contract["remaining_blockers"],
        )
        host = contract["host_action_policy"]
        self.assertFalse(host["typed_operation_specific_boundaries_only"])
        proof = contract["proof_boundary"]
        self.assertEqual(proof["harness_type"], "guarded_synthetic_only")
        for key in (
            "live_linux_proof",
            "docker_or_systemd_proof",
            "postgresql_or_qdrant_execution_proof",
            "kernel_confinement_proof",
        ):
            self.assertFalse(proof[key])
        self.assertTrue(proof["no_argument_runner_uses_private_temporary_directory"])
        self.assertTrue(
            proof["direct_test_helper_accepts_caller_owned_disposable_root"]
        )
        self.assertFalse(
            proof["synthetic_unit_exercise_requires_signed_authority"]
        )
        self.assertFalse(proof["synthetic_unit_receipt_is_promotable_live_proof"])

    def test_controller_runtime_is_separate_locked_and_unbuilt(self) -> None:
        runtime = json.loads(
            (PHASE8B / "controller_runtime_contract.json").read_text(
                encoding="ascii"
            )
        )
        dependency = runtime["dependency_policy"]
        build = runtime["build_policy"]
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
        self.assertFalse(entrypoint["repository_source_execution_allowed"])
        self.assertFalse(entrypoint["unisolated_module_search_path_allowed"])
        self.assertFalse(entrypoint["activation_entrypoint_included"])
        self.assertTrue(entrypoint["exact_controller_process_is_trusted"])
        self.assertFalse(
            entrypoint[
                "opaque_python_capabilities_resist_hostile_same_process_code"
            ]
        )

        lock = ROOT / dependency["lock_file"]
        raw = lock.read_bytes()
        self.assertRegex(hashlib.sha256(raw).hexdigest(), r"^[0-9a-f]{64}$")
        text = raw.decode("ascii")
        self.assertIn("cryptography==49.0.0", text)
        self.assertNotIn("fastapi", text)
        self.assertNotIn("asyncpg", text)

    def test_current_inactive_package_doc_is_canonical_and_truthful(self) -> None:
        current = (
            ROOT / "docs/memory/clean_successor/PHASE8B_INACTIVE_REMEDIATION.md"
        ).read_text(encoding="utf-8")
        self.assertIn("tools/governed_memory_install/package.py", current)
        self.assertIn(
            "does not contain a complete live installation, rollback, or",
            current,
        )
        self.assertIn(
            "prior root-level Phase 8A successor-install package and executable stack were",
            current,
        )
        self.assertIn("separate Memory v1/v5 repository runtime", current)


if __name__ == "__main__":
    unittest.main()
