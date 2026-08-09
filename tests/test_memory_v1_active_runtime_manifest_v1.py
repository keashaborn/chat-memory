from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops/systemd/memory-v1-active-runtime-manifest-v1.json"


class ActiveRuntimeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.value = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_states_target_and_authorities_without_hiding_compatibility(self) -> None:
        self.assertEqual(
            self.value["contract_version"], "memory_v1_active_runtime_manifest_v1"
        )
        self.assertEqual(
            self.value["activation_state"],
            "installed_inactive_with_compatibility_active",
        )
        self.assertEqual(self.value["current_target_phase"], "installed_inactive")
        self.assertEqual(
            self.value["canonical_authority"]["postgresql"], "authoritative"
        )
        self.assertIn(
            "compatibility_only",
            self.value["canonical_authority"]["raw_memory_qdrant"],
        )
        self.assertFalse(self.value["first_vertical_slice"]["backlog_drain"])
        self.assertFalse(
            self.value["first_vertical_slice"]["automatic_claim_promotion"]
        )
        self.assertFalse(
            self.value["first_vertical_slice"]["recurring_openai_timers"]
        )

    def test_all_declared_source_and_unit_bytes_are_hash_bound(self) -> None:
        for component in self.value["source_components"]:
            path = ROOT / component["path"]
            self.assertTrue(path.is_file(), component["path"])
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), component["sha256"]
            )
        for unit in self.value["systemd_units"]:
            if "source_path" not in unit:
                continue
            path = ROOT / unit["source_path"]
            self.assertTrue(path.is_file(), unit["source_path"])
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(observed, unit["source_sha256"])
            self.assertEqual(observed, unit["installed_sha256"])

    def test_exact_installed_memory_inventory_is_complete_and_unique(self) -> None:
        units = self.value["systemd_units"]
        names = [item["name"] for item in units]
        self.assertEqual(len(names), len(set(names)))
        memory_names = sorted(name for name in names if name.startswith("memory-v1-"))
        self.assertEqual(
            memory_names, self.value["exact_installed_memory_unit_set"]
        )
        self.assertEqual(len(memory_names), 39)

        by_name = {item["name"]: item for item in units}
        for name in (
            "memory-v1-openai-extraction.service",
            "memory-v1-openai-extraction.timer",
            "memory-v1-openai-v5-2-packet-router.service",
            "memory-v1-openai-v5-2-packet-router.timer",
        ):
            expected = by_name[name]["expected"]["installed_inactive"]
            self.assertEqual(expected["load_state"], "loaded")
            self.assertEqual(expected["enabled_state"], "disabled")
            self.assertEqual(expected["active_state"], "inactive")
            self.assertIn("canonical_inactive", by_name[name]["classification"])

    def test_active_and_elapsed_compatibility_timers_are_explicit(self) -> None:
        by_name = {item["name"]: item for item in self.value["systemd_units"]}
        for name in (
            "memory-v1-v5-chat-capture.timer",
            "memory-v1-evidence-intake-dispatcher.timer",
            "memory-v1-v5-2-local-packet-router.timer",
            "memory-v1-v5-local-auto-stage.timer",
            "memory-v1-v5-local-auto-resolution.timer",
            "memory-v1-v5-local-claim-projection.timer",
            "memory-v1-projection.timer",
            "memory-v1-deferred-reconciliation-scan.timer",
            "memory-v1-v5-local-legacy-reintake-audit.timer",
            "memory-v1-v5-local-packet-router.timer",
        ):
            expected = by_name[name]["expected"]["installed_inactive"]
            self.assertEqual(expected["load_state"], "loaded")
            self.assertEqual(expected["enabled_state"], "enabled")
            self.assertEqual(expected["active_state"], "active")

    def test_manual_pilot_and_authenticated_validation_gates_are_first_class(self) -> None:
        blockers = {item["id"]: item for item in self.value["blockers"]}
        self.assertEqual(
            blockers["review_to_claim_admission_manual_pilot_authorization_required"]["status"],
            "exact_item_release_and_projection_available_manual_pilot_required",
        )
        self.assertEqual(
            blockers["user_claim_lifecycle_authenticated_validation_pending"]["status"],
            "installed_backend_deployed_authenticated_ui_validation_pending",
        )
        self.assertEqual(
            blockers["governed_owner_activation_refresh_required"]["status"],
            "content_free_verifier_required_before_each_pilot",
        )
        handoffs = {item["stage"]: item for item in self.value["runtime_handoffs"]}
        self.assertEqual(
            handoffs["review_to_claim_admission"]["status"],
            "installed_manual_exact_proposition_admission_available",
        )
        self.assertEqual(
            handoffs["derived_projection"]["status"],
            "active_exact_item_release_and_projection_available_manual_pilot_required",
        )
        self.assertEqual(
            handoffs["user_provenance_and_lifecycle"]["status"],
            "installed_backend_deployed_authenticated_ui_validation_pending",
        )
        self.assertEqual(
            handoffs["answer_binding"]["status"],
            "deployed_inactive_until_governed_memory_selected",
        )
        raw = {
            item["component"]: item for item in self.value["compatibility_boundaries"]
        }
        self.assertEqual(
            raw["backend_/log_raw_memory_qdrant_side_effect"]["classification"],
            "removed_from_ordinary_log_identity_bootstrap_only",
        )
        self.assertFalse(
            raw["backend_/log_raw_memory_qdrant_side_effect"][
                "may_be_present_in_governed_prompt"
            ]
        )
        self.assertFalse(
            raw["backend_/cards_raw_qdrant_api"]["may_be_present_in_governed_prompt"]
        )

    def test_package_catalog_and_binding_cover_the_combined_target(self) -> None:
        packages = {
            item["migration_id"]: item for item in self.value["database_packages"]
        }
        for migration_id in (
            "memory_openai_circuit_window_v1",
            "memory_openai_eligibility_disposition_v2",
            "memory_v1_openai_review_admission_authority_v1",
            "memory_openai_review_admission_authority_v1",
            "memory_openai_review_reference_authority_v3",
            "memory_v1_governed_claim_lifecycle_outbox_authority_v1",
            "memory_v1_governed_claim_transition_authority_v1",
            "memory_governed_claim_lifecycle_v1",
            "memory_v1_projection_outbox_release_authority_v1",
            "memory_projection_outbox_release_v1",
        ):
            self.assertEqual(
                packages[migration_id]["installation_state"],
                "installed_definition_verified",
            )
        components = {
            item["component_id"]: item for item in self.value["source_components"]
        }
        self.assertEqual(
            components["openai_review_admission"]["classification"],
            "canonical_active_manual_admission",
        )
        self.assertEqual(
            components["governed_claim_transition"]["classification"],
            "canonical_active_manual_claim_transition",
        )
        self.assertEqual(
            components["governed_claim_lifecycle_router"]["classification"],
            "canonical_active_owner_lifecycle_with_reviewed_correction",
        )
        self.assertEqual(
            components["openai_provider_adapter"]["classification"],
            "canonical_inactive_provider_contract",
        )
        self.assertEqual(
            components["openai_packet_review_builder"]["classification"],
            "canonical_inactive_exact_route_authorized_reader",
        )
        self.assertEqual(
            components["openai_packet_review_builder"]["path"],
            "scripts/memory_v1_openai_review_packet_v1.py",
        )
        self.assertEqual(
            components["governed_response_route"]["path"],
            "rag_engine/resse_response_router.py",
        )
        self.assertEqual(
            components["governed_answer_provenance"]["classification"],
            "canonical_active_bounded_model_exposure_not_semantic_use",
        )
        self.assertEqual(
            components["governed_owner_activation_verifier"]["classification"],
            "canonical_active_content_free_verifier",
        )
        self.assertEqual(
            components["active_runtime_verifier"]["classification"],
            "canonical_active_fail_closed_complete_path_verifier",
        )
        self.assertEqual(
            components["projection_core"]["classification"],
            "canonical_active_exact_item_and_bounded_backlog_projection",
        )
        functions = {
            item["signature"]: item["sha256"]
            for item in self.value["catalog_contract"]["functions"]
        }
        claim = next(
            signature
            for signature in functions
            if signature.startswith("memory.claim_owner_v5_bounded_extraction_job_v1(")
        )
        self.assertEqual(
            functions[claim],
            "f71bccae8e2496969daf06d4657bae71dae70630bcf9a302b033ddd91af2137e",
        )
        expected_review_hashes = {
            "memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb)": "8d503ad82beab0c010e96350bc88fe8b133c5ab48d0a462462bbd5be864ac770",
            "memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text)": "9eabd51c82611ec80d8a0f9732baacd7f863c4c9d54c61c0f8a51b7e0590ab0f",
            "memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text)": "7bb694266195cf56b8ba0d9bda8c9d6cfa162b700532f5130061fa68cd2de07a",
        }
        for signature, expected_hash in expected_review_hashes.items():
            self.assertEqual(functions[signature], expected_hash)
        for signature in (
            "memory.read_owner_governed_claim_lifecycle_v1(integer)",
            "memory.retract_owner_governed_claim_v1(uuid,uuid,uuid,integer,text,text)",
            "memory.release_owner_projection_outbox_v1(uuid,uuid,uuid,text,text,text)",
            "memory.release_owner_projection_outbox_write_v1(uuid,uuid,uuid,text)",
        ):
            self.assertIn(signature, functions)
        self.assertGreaterEqual(
            len(self.value["catalog_contract"]["forced_rls_relations"]), 18
        )
        binding = self.value["release_binding"]
        self.assertEqual(
            binding["contract_version"],
            "memory_v1_active_runtime_release_binding_v1",
        )
        self.assertEqual(
            set(binding["required_fields"]),
            {
                "catalog_environment_sha256",
                "catalog_function_sha256",
                "manifest_sha256",
                "openai_sdk_version",
                "phase",
                "python_executable_sha256",
                "repository_commit",
                "repository_tree",
                "runtime_config_sha256",
                "source_sha256",
                "unit_sha256",
            },
        )

    def test_verifier_has_one_current_fail_closed_phase(self) -> None:
        verification = self.value["verification"]
        self.assertEqual(
            verification["contract_version"],
            "memory_v1_active_runtime_verifier_v1",
        )
        self.assertEqual(
            set(verification["supported_phases"]), {"installed_inactive"}
        )
        self.assertTrue(verification["reject_unlisted_installed_memory_units"])
        self.assertEqual(
            verification["catalog_environment"],
            "/opt/chat-memory/.env",
        )
        self.assertEqual(
            verification["supported_phases"]["installed_inactive"][
                "catalog_environment_state"
            ],
            "root_owned_0600_regular_single_link",
        )
        self.assertTrue(
            (ROOT / "scripts/memory_v1_active_runtime_verifier_v1.py").is_file()
        )
        activation = verification["governed_owner_activation"]
        self.assertEqual(
            activation["contract_version"],
            "memory_v1_governed_activation_verifier_v1",
        )
        self.assertEqual(
            activation["output"],
            "boolean_owner_allowlisted_and_configuration_sha256_only",
        )


if __name__ == "__main__":
    unittest.main()
