"""Current inactive-package and immutable application-runtime identity checks."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import tomllib
import unicodedata
import unittest

from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.auth import ActorRole
from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_sha256,
)
from rag_engine.governed_memory.repository import GovernedMemoryRepository
from tools.governed_memory_release.build_candidate_runtime import (
    PROVIDER_ASSET_SOURCE_PATHS,
    _source_tree_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "rag_engine" / "governed_memory"
RUNTIME_PACKAGE = PACKAGE / "runtime"
PROVIDER_ASSETS = PACKAGE / "provider_assets"
TESTS = ROOT / "tests" / "memory"
INTEGRATION_TESTS = ROOT / "tests" / "memory_integration"
VALIDATION_TOOLS = ROOT / "tools" / "governed_memory_validation"
RELEASE_TOOLS = ROOT / "tools" / "governed_memory_release"
INSTALL_TOOLS = ROOT / "tools" / "governed_memory_install"
OPS = ROOT / "ops" / "governed_memory"
RUNTIME_MANIFEST = OPS / "runtime_manifest.json"
CURRENT_COMPONENT_DISPOSITION = OPS / "current_component_disposition.json"
PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT = (
    OPS / "phase9j_controller_runtime_release_receipt.json"
)
HISTORICAL_PHASE6B_COMPONENT_DISPOSITION = (
    OPS / "history" / "phase6b" / "component_disposition.json"
)
RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT = (
    OPS / "history" / "phase7c" / "runtime_build_receipt.json"
)
DISPOSABLE_RUNNER = VALIDATION_TOOLS / "run_disposable_successor.sh"
CURRENT_PHASE8G_DISPOSABLE_PROOF = OPS / "phase8g_disposable_proof_receipt.json"
HISTORICAL_PHASE7C_DISPOSABLE_PROOF = (
    OPS / "history" / "phase7c" / "disposable_proof_receipt.json"
)
POSTGRES_BOOTSTRAP = VALIDATION_TOOLS / "postgres_bootstrap.pgsql"
SCHEMA_CONTRACT = ROOT / "governed-memory-migrations" / "schema_contract.json"
README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"

EXPECTED_CURRENT_SOURCE_TREE_SHA256 = (
    "b52b753dc7974ee120e4abe264bb36f16b53341fa86b2ea8e0ab5bf86c735c20"
)
EXPECTED_HISTORICAL_PHASE7C_SOURCE_TREE_SHA256 = (
    "610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd"
)
EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256 = (
    "25ca53e683e53f79b726909ef64bc8afad30269cce3f59804cf66335667a8108"
)
EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256 = (
    "9884fa9db3be81039b691a25866994d6033c5cf9175030ea82de401a2bbe6334"
)
EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256 = (
    "c4f6e657d864a3fc60271dc8e875c12c193b91bb6fc5815314c7dd0182c4af8e"
)
EXPECTED_PHASE9J_RUNTIME_TREE_SHA256 = (
    "61d979628f05b789bc74c6d5ad8b755fa73172492c2a6c425cbca5923237672d"
)
EXPECTED_PHASE9J_RELEASE_TREE_SHA256 = (
    "d71014855b9b59c3fad3dabda2e72827a21686621ef6ccc1ca2b36669f295e36"
)
EXPECTED_HISTORICAL_PHASE7C_RUNTIME_RECEIPT_SHA256 = (
    "210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2"
)
EXPECTED_CURRENT_PROJECT_WHEEL_SHA256 = (
    "1a13ebf4d686e5d3ddb7a04979042f0751f78cc97722a733a193bb83bcef23d2"
)
EXPECTED_CURRENT_CANDIDATE_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
EXPECTED_DISPOSABLE_RUNNER_SHA256 = (
    "e98a3214e20be1e2cdea4fed1160e50a5dce10af140dbacfa2bc2a7f38c19df0"
)
EXPECTED_PROOF_EXECUTION_RUNNER_SHA256 = (
    "72006c10f9176d80fbf4c22bc567b31a363785156751118a736a59aa8d4389ee"
)
EXPECTED_PHASE8G_PROOF_SHA256 = (
    "9ce44ac7d3ed08970d88f9ad40df14f82c9ad677af2629a9ff2f7d2aa9385e76"
)
EXPECTED_PHASE7C_PROOF_SHA256 = (
    "d4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0"
)
EXPECTED_PROMOTED_MIGRATION_MANIFEST_SHA256 = (
    "831962c268fc0f0be96d19d3186f0a26e7c60cf8f49bf28e80aa5e9d63a1bf99"
)
EXPECTED_POSTGRES_BOOTSTRAP_SHA256 = (
    "9cdda41a1056bec45409e13002bcdd5a234b13d6a4cc18306668bd38085ca5eb"
)
EXPECTED_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{EXPECTED_CURRENT_SOURCE_TREE_SHA256}/bin/python"
)

EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "admission.py",
    "api.py",
    "auth.py",
    "contracts.py",
    "chat_commands.py",
    "conversation_capture.py",
    "conversation_deletion.py",
    "conversation_erasure_http.py",
    "conversation_source.py",
    "deletion_contracts.py",
    "eligibility.py",
    "exclusive_cutover.py",
    "extraction.py",
    "http_api.py",
    "http_auth.py",
    "http_runtime.py",
    "http_service.py",
    "http_store.py",
    "lifecycle.py",
    "postgres_adapter.py",
    "projection.py",
    "repository.py",
    "response_contracts.py",
    "response_postgres.py",
    "response_provenance.py",
    "response_provider.py",
    "response_runtime.py",
    "retrieval.py",
    "successor_live_authority.py",
    "worker.py",
}
EXPECTED_RUNTIME_FILES = {
    "__init__.py",
    "__main__.py",
    "application.py",
    "calibration.py",
    "deletion_coordinator.py",
    "deletion_postgres.py",
    "environment.py",
    "https_transport.py",
    "live_supabase.py",
    "once_worker.py",
    "openai_adapters.py",
    "pilot_marker.py",
    "qdrant_adapter.py",
    "qdrant_transport.py",
    "worker_application.py",
    "worker_bridge.py",
    "worker_postgres.py",
}
EXPECTED_PROVIDER_ASSETS = {
    "__init__.py",
    "extraction_instructions.txt",
    "extraction_output.schema.json",
    "predicate_catalog.json",
}
EXPECTED_TEST_FILES = {
    "__init__.py",
    "_fixtures.py",
    "resource_identity_test_support.py",
    "test_admission.py",
    "test_auth_claim_artifacts.py",
    "test_build_provenance.py",
    "test_bootstrap_phase9_disposable_store_substrate.py",
    "test_canonical_cluster_rollback.py",
    "test_chat_memory_e2e.py",
    "test_conversation_bridge.py",
    "test_conversation_capture.py",
    "test_conversation_deletion.py",
    "test_conversation_erasure_http.py",
    "test_controller_runtime_builder.py",
    "test_deletion_contracts.py",
    "test_deletion_coordinator.py",
    "test_installation_synthetic_proof.py",
    "test_installation_durable_journal.py",
    "test_installation_execution_contracts.py",
    "test_eligibility.py",
    "test_execute_phase9_disposable_live_proof_controller.py",
    "test_exclusive_cutover.py",
    "test_extraction.py",
    "test_governed_memory_erasure_proxy_v1.py",
    "test_http_api.py",
    "test_http_auth.py",
    "test_http_live_auth_mapping.py",
    "test_http_runtime.py",
    "test_http_service.py",
    "test_http_store.py",
    "test_https_transport.py",
    "test_installation_authority.py",
    "test_intake_boundary.py",
    "test_lifecycle.py",
    "test_linux_store_effects.py",
    "test_linux_live_adapters.py",
    "test_linux_live_transports.py",
    "test_linux_store_readiness.py",
    "test_once_worker.py",
    "test_phase9_permitted_candidate.py",
    "test_openai_adapters.py",
    "test_pilot_marker.py",
    "test_disposable_installation_live_proof.py",
    "test_linux_runtime_publication_primitives.py",
    "test_postgres_native_stages.py",
    "test_process_death_arm_receipt.py",
    "test_psycopg_postgres_adapter.py",
    "test_publish_phase9_controller_runtime.py",
    "test_publish_phase9_staged_prefix_permit.py",
    "test_runtime_input_stager.py",
    "test_standalone_cpython_inspector.py",
    "test_execution_authority_state.py",
    "test_dormant_store_install_controller.py",
    "test_installation_package.py",
    "test_installation_resource_identity_security.py",
    "test_installation_store_contracts.py",
    "test_installation_composition.py",
    "test_installation_durability_anchors.py",
    "test_installation_receipts.py",
    "test_installation_runtime_capability.py",
    "test_durable_receipts.py",
    "test_durable_live_proof_receipt_publication.py",
    "test_empty_rollback_authority.py",
    "test_empty_rollback_execution.py",
    "test_empty_rollback_plan.py",
    "test_execute_phase9_staged_prefix_disposition.py",
    "test_staged_prefix_disposition.py",
    "test_projection.py",
    "test_prompt_and_binding.py",
    "test_qdrant_adapter.py",
    "test_qdrant_transport.py",
    "test_release_contracts.py",
    "test_response_provenance.py",
    "test_response_provider.py",
    "test_response_runtime.py",
    "test_retrieval.py",
    "test_retrieval_calibration.py",
    "test_rollback_live_adapter.py",
    "test_live_rollback_marker.py",
    "test_runtime_inventory.py",
    "test_runtime_publication_transport.py",
    "test_runtime_release.py",
    "test_schema_and_rls.py",
    "test_successor_live_authority.py",
    "test_worker_application.py",
    "test_worker_bridge.py",
    "test_worker_postgres.py",
    "test_worker_recovery.py",
}

EXPECTED_ROUTES = [
    "GET /memory/status",
    "GET /memory/claims",
    "GET /memory/claims/{claim_id}",
    "GET /memory/proposals",
    "POST /memory/proposals/{proposal_id}/review",
    "POST /memory/claims/{claim_id}/correct",
    "POST /memory/claims/{claim_id}/retract",
    "DELETE /memory/claims/{claim_id}",
    "GET /memory/operations/{operation_id}",
    "POST /memory/conversations/erasure-requests",
    "GET /memory/conversations/erasure-requests/{operation_id}",
]


def _file_names(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir() if entry.is_file()}


def _literal_exports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: list[tuple[str, ...]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise AssertionError(f"nonliteral __all__: {path}")
        values.append(tuple(value))
    if len(values) != 1:
        raise AssertionError(f"expected one __all__: {path}")
    return values[0]


class CurrentRuntimeInventoryTests(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        return json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))

    def test_current_component_disposition_and_legacy_quarantine_are_exact(self) -> None:
        self.assertFalse((OPS / "phase6b_component_disposition.json").exists())
        self.assertTrue(HISTORICAL_PHASE6B_COMPONENT_DISPOSITION.is_file())
        disposition = json.loads(
            CURRENT_COMPONENT_DISPOSITION.read_text(encoding="utf-8")
        )
        self.assertEqual(
            disposition["schema_version"],
            "governed-memory-current-component-disposition-v3",
        )
        active = disposition["current_successor"]
        self.assertFalse(active["legacy_memory_fallback_allowed"])
        self.assertFalse(active["stored_assistant_preferences_allowed"])
        self.assertFalse(active["legacy_memory_prompt_object_allowed"])
        active_files = {
            active["application_root"],
            active["chat_integrity"],
            active["chat_erasure_proxy"],
            active["package_manifest"],
            active["package_verifier"],
            active["install_entrypoint"],
            active["empty_rollback_entrypoint"],
            active["durable_receipt_store"],
            active["closed_linux_install_adapter"],
            active["fixed_loopback_readiness_adapter"],
            active["physical_empty_rollback_adapter"],
            active["controller_runtime_release_builder"],
            active["synthetic_proof_entrypoint"],
            active["disposable_linux_proof_manager_entrypoint"],
            active["disposable_linux_proof_private_issuer"],
            active["disposable_linux_proof_sealed_runner"],
            active["closed_live_transport_contracts"],
            active["closed_non_postgresql_live_linux_adapters"],
            active["controller_rollback_marker_transport"],
            active["postgresql_driver_adapter"],
            active["postgresql_native_stage_machine"],
            active["postgresql_native_stage_contract"],
            active["controller_runtime_publication_policy_transport"],
            active["controller_runtime_publication_primitives"],
            active["controller_runtime_input_stager"],
            active["standalone_cpython_inspector"],
        }
        for relative in active_files:
            self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertTrue((ROOT / active["memory_runtime"]).is_dir())
        self.assertEqual(
            {item["component"] for item in disposition["quarantined_legacy"]},
            {
                "memory_v1_v5_runtime",
                "legacy_preferences_identity_admin_and_vantage",
                "legacy_live_services_and_store_state",
            },
        )
        self.assertIn(
            "separate_exact_deletion_batch_authorized",
            disposition["future_deletion_gates"],
        )
        controller = disposition["sealed_dormant_store_package"]
        self.assertTrue(
            controller["claim_bound_install_controller_composition_packaged"]
        )
        self.assertTrue(
            controller[
                "install_receipt_binds_fresh_terminal_canonical_store_readiness"
            ]
        )
        self.assertTrue(
            controller[
                "empty_rollback_requires_opaque_verified_install_receipt_and_ledger"
            ]
        )
        self.assertTrue(
            controller[
                "completed_install_and_empty_rollback_replay_reverification_packaged"
            ]
        )
        self.assertTrue(
            controller[
                "claim_bound_empty_rollback_controller_composition_packaged"
            ]
        )
        for field in (
            "controller_runtime_verification_capability_packaged",
            "full_controller_release_tree_verification_packaged",
            "exact_locked_controller_distribution_set_verification_packaged",
            "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt",
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_marker_and_receipt",
            "controller_runtime_and_release_required_separate_publication_authority",
            "supervisor_launcher_source_packaged",
            "resolved_store_spec_and_exact_docker_labels_bound",
            "resource_identity_ledger_v2_packaged",
            "retained_audit_artifact_hashes_bound",
        ):
            self.assertTrue(controller[field], field)
        self.assertFalse(
            controller["controller_runtime_built_or_installed_at_package_sealing"]
        )
        self.assertFalse(controller["controller_release_staged_at_package_sealing"])
        self.assertFalse(controller["stores_install_owns_or_removes_controller_substrate"])
        self.assertFalse(
            controller["physical_postgresql_qdrant_writer_exclusion_packaged"]
        )
        for field in (
            "empty_rollback_controller_authority_marker_packaged",
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence",
            "closed_live_transport_contracts_packaged",
            "complete_closed_live_transport_substrate_set_packaged",
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory",
            "driver_native_postgresql_stage_contract_packaged",
            "driver_native_postgresql_executable_stage_machine_packaged",
            "concrete_psycopg_postgresql_transport_packaged",
            "runtime_input_selection_contract_repaired",
            "runtime_build_receipt_provenance_v4_packaged",
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required",
            "approved_terminal_postgresql_catalog_manifest_selected",
            "durable_controller_rollback_marker_transport_packaged",
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented",
            "selected_non_postgresql_live_linux_platform_transport_factory_packaged",
            "selected_live_linux_platform_transports_packaged",
            "pinned_postgresql_driver_selected_or_packaged",
            "controller_runtime_publication_policy_transport_packaged",
            "runtime_publication_durable_intent_before_first_rename_packaged",
            "runtime_publication_post_intent_generic_cleanup_forbidden",
            "runtime_publication_crash_prefix_manual_review_fence_packaged",
            "runtime_publication_same_device_rename_precondition_packaged",
            "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged",
            "production_runtime_publication_primitives_packaged",
            "approved_standalone_cpython_substrate_digest_bound",
            "independent_standalone_cpython_payload_tree_proof_packaged",
        ):
            self.assertTrue(controller[field], field)
        for field in (
            "physical_postgresql_qdrant_writer_exclusion_transport_packaged",
            "external_direct_writer_exclusion_implemented",
            "stopped_store_semantic_empty_recheck_is_valid",
            "destructive_rollback_steps_atomically_recheck_empty_under_fence",
        ):
            self.assertFalse(controller[field], field)
        self.assertTrue(controller["concrete_install_store_effect_adapters_packaged"])
        self.assertTrue(
            controller["concrete_empty_rollback_store_effect_adapters_packaged"]
        )
        self.assertFalse(controller["activation_entrypoint_packaged"])
        publication = disposition["published_controller_substrate"]
        self.assertEqual(
            publication["receipt_sha256"],
            EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256,
        )
        self.assertEqual(
            publication["package_manifest_sha256"],
            EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256,
        )
        self.assertEqual(
            publication["runtime_root"],
            "/opt/governed-memory-controller/runtimes/"
            + EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256,
        )
        self.assertEqual(
            publication["runtime_tree_sha256"],
            EXPECTED_PHASE9J_RUNTIME_TREE_SHA256,
        )
        self.assertEqual(
            publication["release_root"],
            "/opt/governed-memory-controller/releases/"
            + EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256,
        )
        self.assertEqual(
            publication["release_tree_sha256"],
            EXPECTED_PHASE9J_RELEASE_TREE_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(
                PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT.read_bytes()
            ).hexdigest(),
            EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256,
        )
        self.assertTrue(publication["persistent_controller_substrate_created"])
        for field in (
            "persistent_store_resources_created",
            "disposable_live_proof_complete",
            "installation_performed",
            "empty_rollback_performed",
            "production_state_changed",
            "activation_authorized",
        ):
            self.assertFalse(publication[field], field)
        safety = disposition["safety"]
        for field in (
            "store_installation_or_rollback_performed",
            "disposable_live_proof_performed",
            "production_services_changed",
            "production_secrets_read_or_changed",
            "production_postgresql_read_or_changed",
            "production_qdrant_read_or_changed",
            "production_data_read",
            "structured_lifeswitch_data_in_scope",
            "accounts_in_scope",
        ):
            self.assertFalse(safety[field], field)
        self.assertTrue(safety["controller_substrate_publication_completed"])
        self.assertTrue(safety["controller_substrate_publication_receipt_promoted"])

    def test_current_source_and_rebuilt_runtime_are_exactly_bound(self) -> None:
        current_source_sha256 = _source_tree_sha256(ROOT)
        self.assertEqual(current_source_sha256, EXPECTED_CURRENT_SOURCE_TREE_SHA256)
        receipt = json.loads(RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii"))
        self.assertEqual(
            hashlib.sha256(RUNTIME_BUILD_RECEIPT.read_bytes()).hexdigest(),
            EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256,
        )
        self.assertEqual(
            receipt["schema_version"], "governed-memory-runtime-build-receipt-v1"
        )
        self.assertEqual(
            receipt["source_tree_sha256"], EXPECTED_CURRENT_SOURCE_TREE_SHA256
        )
        self.assertEqual(receipt["candidate_python"], EXPECTED_CANDIDATE_PYTHON)
        self.assertEqual(
            receipt["project_wheel_sha256"],
            EXPECTED_CURRENT_PROJECT_WHEEL_SHA256,
        )
        self.assertFalse(receipt["candidate_python_is_symlink"])
        self.assertEqual(receipt["network_calls"], 0)
        self.assertEqual(receipt["provider_calls"], 0)
        self.assertFalse(receipt["persistent_resources_created"])
        self.assertFalse(receipt["production_state_changed"])
        self.assertFalse(receipt["legacy_environment_imported"])

    def test_runtime_manifest_separates_current_and_historical_evidence(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["phase"],
            "phase9j_install_ready_closed_runtime_and_store_transports_"
            "packaged_inactive_activation_blocked",
        )
        self.assertFalse(manifest["production_state_changed"])
        self.assertFalse(manifest["legacy_imports_allowed"])
        authority = manifest["authority"]
        self.assertEqual(
            set(authority),
            {
                "phase8g_application_validation_snapshot",
                "phase9j_inactive_store_target",
            },
        )
        phase8g_authority = authority["phase8g_application_validation_snapshot"]
        self.assertEqual(
            phase8g_authority["evidence_role"],
            "historical_disposable_application_validation_not_current_store_"
            "or_routing_authority",
        )
        self.assertEqual(phase8g_authority["database_target"], "127.0.0.1:55432")
        self.assertEqual(phase8g_authority["qdrant_target"], "127.0.0.1:6343")
        self.assertEqual(
            phase8g_authority["qdrant_collection"],
            "governed_memory_9a54cf123493_000001",
        )
        phase9j_target = authority["phase9j_inactive_store_target"]
        self.assertEqual(
            phase9j_target["candidate_id"],
            "governed_memory_9a54cf123493_000006",
        )
        self.assertEqual(phase9j_target["database_target"], "127.0.0.1:55437")
        self.assertEqual(phase9j_target["qdrant_target"], "127.0.0.1:6348")
        self.assertEqual(
            phase9j_target["qdrant_collection"],
            "governed_memory_9a54cf123493_000006",
        )
        self.assertFalse(phase9j_target["store_installation_performed"])
        self.assertFalse(phase9j_target["current_route_installed"])
        self.assertFalse(phase9j_target["activation_authorized"])
        publication = manifest["controller_runtime_publication"]
        self.assertEqual(
            publication["receipt_sha256"],
            EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256,
        )
        self.assertTrue(publication["persistent_controller_substrate_created"])
        self.assertFalse(publication["persistent_store_resources_created"])
        self.assertFalse(publication["active_production_state_changed"])
        self.assertFalse(publication["disposable_live_proof_complete"])
        self.assertFalse(publication["production_installation_authorized"])
        self.assertFalse(publication["activation_authorized"])
        activation = manifest["activation"]
        self.assertFalse(activation["production_authorized"])
        for key in (
            "retained_phase7a_snapshot_installed_services",
            "retained_phase7a_snapshot_running_services",
            "retained_phase7a_snapshot_enabled_services",
            "retained_phase7a_snapshot_installed_timers",
            "retained_phase7a_snapshot_enabled_timers",
        ):
            self.assertEqual(activation[key], [])
        self.assertFalse(
            activation["phase8d_live_successor_service_state_reverified"]
        )
        for stale_live_key in (
            "installed_services",
            "running_services",
            "enabled_services",
            "installed_timers",
            "enabled_timers",
        ):
            self.assertNotIn(stale_live_key, activation)
        validation = manifest["disposable_validation"]
        self.assertEqual(
            validation["scope"],
            "phase8g_current_candidate_successor_disposable_only",
        )
        self.assertEqual(
            validation["evidence_role"],
            "current_candidate_application_and_deletion_proof_not_installation_"
            "or_live_proof",
        )
        self.assertTrue(validation["current_proof_complete"])
        self.assertFalse(validation["reusable_as_current_store_installation_proof"])
        self.assertFalse(validation["reusable_as_live_proof"])
        self.assertFalse(validation["disposable_revalidation_required"])
        self.assertEqual(
            validation["runner_path"],
            "tools/governed_memory_validation/run_disposable_successor.sh",
        )
        self.assertEqual(
            validation["runner_sha256"], EXPECTED_DISPOSABLE_RUNNER_SHA256
        )
        self.assertTrue(validation["runner_sealed"])
        self.assertEqual(
            validation["proof_execution_runner_sha256"],
            EXPECTED_PROOF_EXECUTION_RUNNER_SHA256,
        )
        self.assertTrue(validation["current_runner_execution_attested_by_phase8g_proof"])
        self.assertEqual(
            validation["current_runner_post_proof_change_scope"],
            "none",
        )
        self.assertFalse(
            validation["current_runner_proof_execution_semantics_changed"]
        )
        self.assertEqual(
            validation["current_proof_receipt"],
            "ops/governed_memory/phase8g_disposable_proof_receipt.json",
        )
        self.assertEqual(
            validation["current_proof_receipt_sha256"],
            EXPECTED_PHASE8G_PROOF_SHA256,
        )
        self.assertEqual(
            validation["current_promoted_migration_manifest_sha256"],
            EXPECTED_PROMOTED_MIGRATION_MANIFEST_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(CURRENT_PHASE8G_DISPOSABLE_PROOF.read_bytes()).hexdigest(),
            EXPECTED_PHASE8G_PROOF_SHA256,
        )
        self.assertTrue(CURRENT_PHASE8G_DISPOSABLE_PROOF.is_file())
        self.assertFalse(CURRENT_PHASE8G_DISPOSABLE_PROOF.is_symlink())
        history = manifest["historical_evidence"]
        self.assertEqual(
            history["phase7c_disposable_proof_receipt"],
            "ops/governed_memory/history/phase7c/disposable_proof_receipt.json",
        )
        self.assertEqual(
            history["phase7c_disposable_proof_receipt_sha256"],
            EXPECTED_PHASE7C_PROOF_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(
                HISTORICAL_PHASE7C_DISPOSABLE_PROOF.read_bytes()
            ).hexdigest(),
            EXPECTED_PHASE7C_PROOF_SHA256,
        )
        self.assertNotEqual(EXPECTED_PHASE8G_PROOF_SHA256, EXPECTED_PHASE7C_PROOF_SHA256)
        self.assertFalse((OPS / "phase7c_disposable_proof_receipt.json").exists())
        for key in (
            "postgresql_fresh_empty",
            "qdrant_fresh_empty",
            "migration_forward_rollback_reapply",
            "normalized_catalog_equivalent_after_reapply",
            "forced_rls_owner_isolation_and_direct_dml_denial",
            "asymmetric_jwt_and_jwks_boundary_invoked",
            "owner_http_lifecycle_invoked",
            "all_owner_routes_invoked",
            "alternating_owner_pool_isolation",
            "distinct_chat_a_chat_b",
            "cold_extraction_reconstruction",
            "cold_postgresql_projection_rebuild",
            "correction_retraction_hard_delete_and_retention",
            "qdrant_v1_19_0_real_disposable_compatibility_verified",
            "pilot_marker_disposable_proof_complete",
            "worker_runtime_composition_validated",
            "worker_cross_process_singleton_validated",
            "deletion_coordination_disposable_proof_complete",
            "final_resources_absent",
            "resource_cleanup_complete",
        ):
            self.assertTrue(validation[key], key)
        self.assertFalse(validation["production_routes_installed"])
        self.assertFalse(validation["authenticated_frontend_verified"])
        self.assertFalse(validation["semantic_threshold_calibrated"])
        self.assertFalse(validation["production_data_read"])
        self.assertEqual(validation["production_endpoint_calls"], 0)
        self.assertEqual(validation["provider_external_calls"], 0)
        self.assertFalse(validation["persistent_resources_created"])
        current = manifest["inactive_store_package"]
        self.assertEqual(current["scope"], "current_inactive_stores_only_package")
        self.assertEqual(
            current["state"],
            "phase9j_install_ready_closed_runtime_and_store_transports_"
            "packaged_not_installed_not_activated",
        )
        self.assertEqual(current["package_artifact_count"], 76)
        self.assertEqual(
            current["package_manifest_sha256"],
            EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256,
        )
        self.assertEqual(
            current["contract_canonical_sha256"],
            "a290fccd427e77d6790cf412712ef77b9f199612b4f9914d3bd4275146691031",
        )
        self.assertEqual(
            current["controller_plan_canonical_sha256"],
            "0712ecd82bcb20c561ee94ff72ab28a4264651bfe0398ecce9fd23c318ed8fc2",
        )
        self.assertEqual(
            current["execution_contract_canonical_sha256"],
            "247466888415743fe4a0ef3c677f5cb8deb7b41fe116acd78a5c7647f8ca98d6",
        )
        self.assertEqual(
            current["controller_runtime_contract_canonical_sha256"],
            "60580592361d52b6af3d57d023d7bdd3f0876d677f736e6ee00aeb85841d79ac",
        )
        self.assertEqual(
            current["postgres_native_stage_contract"],
            "ops/governed_memory/installation/current/postgres/native_stage_contract.json",
        )
        self.assertEqual(
            current["postgres_native_stage_contract_canonical_sha256"],
            "2e58aa4513f0e78d2cce62a4ebd5f179a38ef8fcc5ce166c87bfb9cbfcb900ca",
        )
        self.assertEqual(
            current["store_migration_manifest_sha256"],
            "5a216df5ff299e4b3a50b301b09d7eb8bfa7bcedd17ea624833816c93899ab2d",
        )
        self.assertEqual(current["store_migration_file_count"], 9)
        self.assertTrue(current["static_package_verification_complete"])
        self.assertTrue(
            current["historical_dormant_store_install_static_package_verification_complete"]
        )
        self.assertTrue(current["synthetic_proof_harness_packaged"])
        self.assertFalse(current["synthetic_proof_executed_for_current_package"])
        self.assertTrue(current["historical_dormant_store_install_synthetic_proof_executed"])
        self.assertFalse(current["synthetic_proof_executed_by_release_guard"])
        self.assertEqual(current["synthetic_proof_scenario_count"], 131)
        self.assertEqual(
            current["synthetic_proof_outcome"],
            "in_process_test_evidence_only_not_promoted_current_package_proof",
        )
        self.assertEqual(
            current["synthetic_proof_receipt_sha256"],
            "ce9a57f6f01c670dfbc030bff812b99bfb13e9782f406d7e576f0d89af1f33c6",
        )
        self.assertEqual(
            current["synthetic_proof_execution_record"],
            "ops/governed_memory/history/phase8d/"
            "phase8a_successor_installation_stack_retirement.json",
        )
        self.assertFalse(current["synthetic_proof_full_receipt_persisted"])
        self.assertFalse(current["synthetic_proof_receipt_promoted"])
        self.assertFalse(current["live_installation_proof_complete"])
        self.assertTrue(current["installation_executor_packaged"])
        self.assertTrue(current["rollback_executor_packaged"])
        for field in (
            "controller_runtime_verification_capability_packaged",
            "full_controller_release_tree_verification_packaged",
            "exact_locked_controller_distribution_set_verification_packaged",
            "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt",
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_marker_and_receipt",
            "controller_runtime_and_release_require_separate_future_build_and_install_authority",
            "supervisor_launcher_source_packaged",
            "resolved_store_spec_and_exact_docker_labels_bound",
            "resource_identity_ledger_v2_packaged",
            "retained_audit_artifact_hashes_bound",
        ):
            self.assertTrue(current[field], field)
        self.assertFalse(current["controller_runtime_built_or_installed"])
        self.assertFalse(current["controller_release_staged"])
        self.assertFalse(current["stores_install_owns_or_removes_controller_substrate"])
        self.assertFalse(
            current["physical_postgresql_qdrant_writer_exclusion_packaged"]
        )
        for field in (
            "empty_rollback_controller_authority_marker_packaged",
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence",
            "closed_live_transport_contracts_packaged",
            "complete_closed_live_transport_substrate_set_packaged",
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory",
            "driver_native_postgresql_stage_contract_packaged",
            "driver_native_postgresql_executable_stage_machine_packaged",
            "concrete_psycopg_postgresql_transport_packaged",
            "runtime_input_selection_contract_repaired",
            "runtime_build_receipt_provenance_v4_packaged",
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required",
            "approved_terminal_postgresql_catalog_manifest_selected",
            "durable_controller_rollback_marker_transport_packaged",
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented",
            "selected_non_postgresql_live_linux_platform_transport_factory_packaged",
            "selected_live_linux_platform_transports_packaged",
            "pinned_postgresql_driver_selected_or_packaged",
            "controller_runtime_publication_policy_transport_packaged",
            "runtime_publication_durable_intent_before_first_rename_packaged",
            "runtime_publication_post_intent_generic_cleanup_forbidden",
            "runtime_publication_crash_prefix_manual_review_fence_packaged",
            "runtime_publication_same_device_rename_precondition_packaged",
            "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged",
            "production_runtime_publication_primitives_packaged",
            "approved_standalone_cpython_substrate_digest_bound",
            "independent_standalone_cpython_payload_tree_proof_packaged",
        ):
            self.assertTrue(current[field], field)
        for field in (
            "physical_postgresql_qdrant_writer_exclusion_transport_packaged",
            "external_direct_writer_exclusion_implemented",
            "stopped_store_semantic_empty_recheck_is_valid",
            "destructive_rollback_steps_atomically_recheck_empty_under_fence",
        ):
            self.assertFalse(current[field], field)
        self.assertTrue(current["concrete_install_store_effect_adapters_packaged"])
        self.assertTrue(
            current["concrete_empty_rollback_store_effect_adapters_packaged"]
        )
        self.assertTrue(current["install_controller_emits_canonical_receipt"])
        self.assertTrue(current["empty_rollback_controller_emits_canonical_receipt"])
        self.assertFalse(current["activation_executor_packaged"])
        self.assertFalse(current["installation_performed"])
        self.assertFalse(
            current["live_installation_state_reverified_for_current_candidate"]
        )
        self.assertFalse(current["installation_authorized"])
        self.assertFalse(current["activation_authorized"])
        self.assertFalse(current["production_state_changed"])
        infrastructure = manifest["infrastructure"]
        self.assertEqual(
            infrastructure["phase7c_disposable_application_compose"],
            "ops/governed_memory/compose.candidate.yaml",
        )
        self.assertEqual(
            infrastructure["phase7c_disposable_application_compose_role"],
            "retained_application_validation_input_not_current_inactive_store_"
            "composition",
        )
        self.assertFalse(infrastructure["persistent_composition_packaged"])
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
            "durable_controller_rollback_marker_transport_packaged",
            "selected_live_linux_platform_transports_packaged",
            "selected_non_postgresql_live_linux_platform_transport_factory_packaged",
            "pinned_postgresql_driver_selected_or_packaged",
        ):
            self.assertTrue(infrastructure[field], field)
        for field in (
            "physical_postgresql_qdrant_writer_exclusion_transport_packaged",
            "external_direct_writer_exclusion_implemented",
        ):
            self.assertFalse(infrastructure[field], field)
        self.assertFalse(infrastructure["phase8d_live_store_state_reverified"])
        self.assertFalse(
            infrastructure[
                "retained_phase7a_snapshot_postgresql_persistent_resource_created"
            ]
        )
        self.assertFalse(
            infrastructure[
                "retained_phase7a_snapshot_qdrant_persistent_resource_created"
            ]
        )
        for retired_key in (
            "compose_candidate",
            "persistent_compose_candidate",
            "compose_scope",
        ):
            self.assertNotIn(retired_key, infrastructure)

    def test_current_validation_runtime_is_exact(self) -> None:
        runtime = self.load_manifest()["validation_runtime"]
        self.assertEqual(
            runtime["current_source_tree_sha256"],
            EXPECTED_CURRENT_SOURCE_TREE_SHA256,
        )
        self.assertEqual(runtime["current_candidate_python"], EXPECTED_CANDIDATE_PYTHON)
        self.assertEqual(
            runtime["current_candidate_python_sha256"],
            EXPECTED_CURRENT_CANDIDATE_PYTHON_SHA256,
        )
        self.assertEqual(
            runtime["current_project_wheel_sha256"],
            EXPECTED_CURRENT_PROJECT_WHEEL_SHA256,
        )
        self.assertEqual(
            runtime["current_build_receipt"],
            "ops/governed_memory/runtime_build_receipt.json",
        )
        self.assertEqual(
            runtime["current_build_receipt_sha256"],
            EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256,
        )
        self.assertTrue(runtime["current_build_receipt_present"])
        self.assertEqual(
            runtime["historical_phase7c_build_receipt"],
            "ops/governed_memory/history/phase7c/runtime_build_receipt.json",
        )
        self.assertEqual(
            runtime["historical_phase7c_build_receipt_sha256"],
            EXPECTED_HISTORICAL_PHASE7C_RUNTIME_RECEIPT_SHA256,
        )
        self.assertTrue(runtime["historical_phase7c_build_receipt_present"])
        self.assertFalse(
            runtime[
                "historical_phase7c_build_receipt_reusable_for_current_candidate"
            ]
        )
        self.assertEqual(
            runtime["runtime_lock_sha256"], EXPECTED_RUNTIME_LOCK_SHA256
        )
        self.assertEqual(runtime["build_lock_sha256"], EXPECTED_BUILD_LOCK_SHA256)
        self.assertTrue(runtime["current_source_bound"])
        self.assertFalse(runtime["current_runtime_rebuild_pending"])

    def test_chat_only_deletion_scope_is_exact(self) -> None:
        ingestion = self.load_manifest()["ingestion"]
        self.assertEqual(
            ingestion["source_erasure_selectors"],
            ["thread", "message_tail", "recent", "all_conversations"],
        )
        self.assertEqual(
            ingestion["source_erasure_direct_delete_roots"],
            ["public.chat_log", "public.chat_attachments", "public.threads"],
        )
        self.assertFalse(
            ingestion[
                "source_erasure_memory_only_or_account_wide_memory_selector_allowed"
            ]
        )
        self.assertFalse(
            ingestion[
                "source_erasure_structured_lifeswitch_data_or_accounts_deleted"
            ]
        )
        self.assertTrue(
            ingestion["source_erasure_lifeswitch_usage_and_audit_records_retained"]
        )
        self.assertEqual(
            ingestion["source_erasure_vantage_answer_trace_disposition"],
            "undecided_activation_blocker",
        )
        self.assertEqual(
            ingestion["source_erasure_telemetry_payload_disposition"],
            "undecided_activation_blocker",
        )
        self.assertFalse(ingestion["source_erasure_legacy_project_rows_deleted"])
        self.assertFalse(ingestion["source_erasure_unclassified_side_effects_allowed"])

    def test_logging_and_frontend_blockers_are_truthful(self) -> None:
        manifest = self.load_manifest()
        http = manifest["http_runtime"]
        self.assertEqual(http["default_mode"], "off")
        self.assertEqual(http["transport"], "permissioned_unix_socket")
        self.assertEqual(
            http["unix_socket_path"], "/run/governed-memory/http.sock"
        )
        self.assertEqual(http["unix_socket_group"], "governed-memory-proxy")
        self.assertFalse(http["unix_socket_group_provisioned"])
        self.assertFalse(http["same_host_brains_proxy_group_member_provisioned"])
        self.assertFalse(http["tcp_listener_allowed"])
        self.assertFalse(http["source_logging_policy_live_verified"])
        self.assertEqual(http["source_log_duration_observed"], "off")
        self.assertEqual(
            http["source_log_parameter_max_length_observed"],
            "-1_full_bind_values_unsafe",
        )
        self.assertFalse(http["source_logging_parameter_remediation_authorized"])
        self.assertFalse(http["source_logging_parameter_remediation_applied"])
        frontend = manifest["frontend_candidate"]
        self.assertEqual(
            frontend["git_commit"], "71377a838058d75320b55817fc8c9656d404f955"
        )
        self.assertFalse(frontend["deployed"])
        self.assertFalse(frontend["authenticated_visual_qa_complete"])
        self.assertFalse(
            frontend["successor_operation_id_and_confirmation_semantics_verified"]
        )

    def test_phase6e_receipts_are_historical_only(self) -> None:
        history = self.load_manifest()["historical_evidence"]
        self.assertFalse(history["reusable_for_current_candidate"])
        self.assertEqual(
            history["phase6e_runtime_build_receipt"],
            "ops/governed_memory/history/phase6e/runtime_build_receipt.json",
        )
        self.assertEqual(
            history["phase6e_disposable_proof_receipt"],
            "ops/governed_memory/history/phase6e/disposable_proof_receipt.json",
        )
        self.assertFalse((OPS / "phase6e_disposable_proof_receipt.json").exists())
        self.assertTrue(
            (OPS / "history" / "phase6e" / "disposable_proof_receipt.json").is_file()
        )

    def test_disposable_runner_uses_current_not_historical_runtime_receipt(
        self,
    ) -> None:
        current_sha256 = hashlib.sha256(RUNTIME_BUILD_RECEIPT.read_bytes()).hexdigest()
        historical_sha256 = hashlib.sha256(
            HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT.read_bytes()
        ).hexdigest()
        historical_receipt = json.loads(
            HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii")
        )
        runner = DISPOSABLE_RUNNER.read_text(encoding="utf-8")
        self.assertEqual(current_sha256, EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256)
        self.assertEqual(
            historical_sha256,
            EXPECTED_HISTORICAL_PHASE7C_RUNTIME_RECEIPT_SHA256,
        )
        self.assertEqual(
            historical_receipt["source_tree_sha256"],
            EXPECTED_HISTORICAL_PHASE7C_SOURCE_TREE_SHA256,
        )
        self.assertNotEqual(current_sha256, historical_sha256)
        self.assertIn(
            f"readonly EXPECTED_RUNTIME_BUILD_RECEIPT_SHA256='{current_sha256}'",
            runner,
        )
        self.assertNotIn(
            f"readonly EXPECTED_RUNTIME_BUILD_RECEIPT_SHA256='{historical_sha256}'",
            runner,
        )
        self.assertEqual(
            hashlib.sha256(DISPOSABLE_RUNNER.read_bytes()).hexdigest(),
            EXPECTED_DISPOSABLE_RUNNER_SHA256,
        )

    def test_disposable_postgres_bootstrap_fixture_is_exact_and_sealed(
        self,
    ) -> None:
        bootstrap = POSTGRES_BOOTSTRAP.read_text(encoding="utf-8")
        self.assertEqual(
            hashlib.sha256(POSTGRES_BOOTSTRAP.read_bytes()).hexdigest(),
            EXPECTED_POSTGRES_BOOTSTRAP_SHA256,
        )
        self.assertEqual(
            bootstrap.count("CREATE ROLE governed_memory_bootstrap"),
            1,
        )
        self.assertIn(
            "CREATE ROLE governed_memory_bootstrap\n"
            "  LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS INHERIT\n"
            "  PASSWORD NULL;",
            bootstrap,
        )
        self.assertEqual(
            bootstrap.count(
                "GRANT governed_memory_owner TO governed_memory_bootstrap;"
            ),
            1,
        )
        self.assertNotIn(
            "GRANT governed_memory_bootstrap TO governed_memory_owner;",
            bootstrap,
        )
        self.assertEqual(
            bootstrap.count(
                "GRANT CONNECT ON DATABASE memory\n"
                "  TO brains_app, governed_memory_api, governed_memory_worker;"
            ),
            1,
        )

    def test_disposable_bootstrap_preserves_legacy_delete_baseline(self) -> None:
        bootstrap = POSTGRES_BOOTSTRAP.read_text(encoding="utf-8")
        self.assertEqual(
            bootstrap.count(
                "GRANT SELECT, INSERT, UPDATE, DELETE\n"
                "  ON public.chat_log, public.threads, public.chat_attachments "
                "TO brains_app;"
            ),
            1,
            "the disposable fixture must preserve the exact legacy DELETE "
            "baseline that migration 0002 revokes and empty-only rollback "
            "restores",
        )

    def test_schema_scope_records_current_phase8g_disposable_validation(self) -> None:
        schema = json.loads(SCHEMA_CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["validation_scope"],
            {
                "scope": "phase8g_current_candidate",
                "environment": "disposable_postgresql_qdrant_synthetic_only",
                "current_candidate_disposable_validated": True,
                "historical_phase7c_disposable_proof_retained": True,
                "disposable_revalidation_required": False,
                "production_data_read": False,
                "provider_external_calls": 0,
                "production_state_changed": False,
            },
        )
        requirements = schema["hard_requirements"]
        self.assertTrue(requirements["current_disposable_successor_validation_complete"])
        self.assertTrue(schema["claim_detail"]["disposable_validated"])
        self.assertTrue(
            schema["claim_detail"]["historical_phase7c_disposable_validated"]
        )
        self.assertTrue(schema["pilot_marker"]["disposable_validated"])
        self.assertTrue(
            schema["pilot_marker"]["historical_phase7c_disposable_validated"]
        )
        self.assertEqual(schema["schemas"], ["memory", "memory_private"])
        self.assertEqual(
            schema["conversation_database"],
            {
                "database": "memory",
                "schemas": ["memory_ingest_private", "chat_integrity"],
                "authority": "separate_existing_conversation_database_only",
            },
        )
        self.assertTrue(
            all(
                name.startswith("memory_private.")
                for name in schema["internal_functions"]
            )
        )
        self.assertEqual(
            requirements["production_activation_blockers"],
            self.load_manifest()["activation"]["blockers"],
        )
        erasure_contract = schema["interface_contracts"]["chat_source_erasure"]
        self.assertIn("Phase 8G validated the current candidate", erasure_contract)
        self.assertIn("did not install or activate production resources", erasure_contract)

    def test_route_surface_is_exact_and_owner_free(self) -> None:
        observed = [
            f"{specification.method.value} {specification.path}"
            for specification in OWNER_ROUTE_SPECIFICATIONS
        ]
        self.assertEqual(observed, EXPECTED_ROUTES)
        self.assertEqual(self.load_manifest()["owner_routes"], EXPECTED_ROUTES)
        self.assertNotIn("user_id", "\n".join(EXPECTED_ROUTES))
        erasure = next(
            value
            for value in OWNER_ROUTE_SPECIFICATIONS
            if value.operation == "request_conversation_erasure"
        )
        self.assertEqual(
            erasure.required_body_fields,
            (
                "confirmation_sha256",
                "contract_version",
                "data_domain",
                "operation_id",
                "selector_kind",
            ),
        )
        self.assertTrue(erasure.server_time_owned)

    def test_actor_and_repository_surfaces_are_closed(self) -> None:
        self.assertEqual(
            tuple((role.name, role.value) for role in ActorRole),
            (("OWNER", "owner"), ("WORKER", "worker")),
        )
        expected_methods = {
            "apply_proposal_review",
            "complete_provider_call",
            "correct_claim",
            "fail_ingest",
            "fail_provider_call",
            "finalize_claim_deletion",
            "finish_projection",
            "mark_provider_dispatched",
            "persist_answer_binding",
            "persist_ingest_decision",
            "read_claims",
            "read_ingest_receipt",
            "read_operation",
            "request_claim_deletion",
            "retract_claim",
        }
        observed = {
            name
            for name, value in vars(GovernedMemoryRepository).items()
            if inspect.isfunction(value) and not name.startswith("_")
        }
        self.assertEqual(observed, expected_methods)
        source = (PACKAGE / "repository.py").read_text(encoding="utf-8")
        for forbidden in ("apply_plan", "effect_names", "free_form_sql"):
            self.assertNotIn(forbidden, source)
        for name in expected_methods:
            signature = inspect.signature(getattr(GovernedMemoryRepository, name))
            for parameter in tuple(signature.parameters.values())[1:]:
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_contract_hash_requires_recursive_nfc(self) -> None:
        composed = {"outer": ["caf\u00e9", {"label": "r\u00e9sum\u00e9"}]}
        decomposed = {
            "outer": [
                unicodedata.normalize("NFD", "caf\u00e9"),
                {"label": unicodedata.normalize("NFD", "r\u00e9sum\u00e9")},
            ]
        }
        self.assertRegex(
            canonical_sha256("governed_memory.nfc_probe", composed),
            r"^[0-9a-f]{64}$",
        )
        with self.assertRaises(ContractViolation):
            canonical_sha256("governed_memory.nfc_probe", decomposed)

    def test_file_inventories_are_exact(self) -> None:
        self.assertEqual(_file_names(PACKAGE) & {p.name for p in PACKAGE.glob("*.py")}, EXPECTED_PACKAGE_FILES)
        self.assertEqual(_file_names(RUNTIME_PACKAGE), EXPECTED_RUNTIME_FILES)
        self.assertEqual(_file_names(PROVIDER_ASSETS), EXPECTED_PROVIDER_ASSETS)
        test_files = _file_names(TESTS)
        self.assertEqual(test_files, EXPECTED_TEST_FILES)
        self.assertTrue(
            {
                "test_execute_phase9_staged_prefix_disposition.py",
                "test_publish_phase9_staged_prefix_permit.py",
                "test_staged_prefix_disposition.py",
            }
            <= test_files
        )
        self.assertTrue(
            {
                "test_execute_phase9_pre_effect_disposition.py",
                "test_pre_effect_disposition.py",
                "test_publish_phase9_pre_effect_permit.py",
            }.isdisjoint(test_files)
        )
        self.assertEqual(
            _file_names(INTEGRATION_TESTS),
            {
                "__init__.py",
                "local_jwks_server.py",
                "test_conversation_deletion_disposable.py",
                "test_governed_memory_http_vertical_slice.py",
            },
        )
        validation_tool_files = _file_names(VALIDATION_TOOLS)
        self.assertEqual(
            validation_tool_files,
            {
                "bootstrap_phase9_disposable_store_substrate.py",
                "durable_live_proof_receipt.py",
                "execute_phase9_disposable_live_proof_controller.py",
                "postgres_bootstrap.pgsql",
                "execute_phase9_staged_prefix_disposition.py",
                "generate_staged_prefix_disposition_contract.py",
                "run_disposable_successor.sh",
                "issue_disposable_installation_live_proof.py",
                "publish_phase9_controller_runtime.py",
                "publish_phase9_staged_prefix_permit.py",
                "phase9_permitted_candidate.py",
                "staged_prefix_disposition.py",
                "process_death_arm_receipt.py",
                "run_disposable_installation_live_proof.py",
                "run_memory_unit_tests.py",
                "runtime_packages.json",
                "generate_installation_package_manifest.py",
                "run_installation_synthetic_proof.py",
                "verify_store_migration_manifest.py",
                "verify_migration_manifest.py",
            },
        )
        self.assertTrue(
            {
                "execute_phase9_staged_prefix_disposition.py",
                "generate_staged_prefix_disposition_contract.py",
                "publish_phase9_staged_prefix_permit.py",
                "staged_prefix_disposition.py",
            }
            <= validation_tool_files
        )
        self.assertTrue(
            {
                "execute_phase9_pre_effect_disposition.py",
                "pre_effect_disposition.py",
                "publish_phase9_pre_effect_permit.py",
            }.isdisjoint(validation_tool_files)
        )
        self.assertEqual(
            _file_names(RELEASE_TOOLS),
            {
                "__init__.py",
                "build_candidate_runtime.py",
                "controller_runtime_builder.py",
                "inspect_standalone_cpython.py",
                "linux_runtime_publication_primitives.py",
                "release_guard.py",
                "runtime_input_stager.py",
                "runtime_publication_transport.py",
            },
        )
        self.assertEqual(
            _file_names(INSTALL_TOOLS),
            {
                "__init__.py",
                "authority.py",
                "authority_state.py",
                "controller.py",
                "controller_runtime.py",
                "disposable_proof_harness.py",
                "durable_receipts.py",
                "execution_authority.py",
                "execution_capability.py",
                "execution_lock.py",
                "host_boundary.py",
                "image_preflight.py",
                "install_backend.py",
                "install_entrypoint.py",
                "journal.py",
                "linux_plan.py",
                "linux_live_adapters.py",
                "linux_live_transports.py",
                "linux_store_effects.py",
                "linux_store_readiness.py",
                "package.py",
                "package_capability.py",
                "postgres_native_stages.py",
                "psycopg_postgres_adapter.py",
                "receipts.py",
                "resource_identity.py",
                "rollback.py",
                "rollback_authority.py",
                "rollback_entrypoint.py",
                "rollback_journal.py",
                "rollback_live_adapter.py",
                "live_rollback_marker.py",
                "secure_file.py",
                "store_readiness.py",
                "store_supervisor.py",
                "store_supervisor_launcher.py",
                "synthetic_backend.py",
            },
        )

    def test_all_exports_are_literal_unique_and_versionless(self) -> None:
        for path in sorted(
            [*PACKAGE.glob("*.py"), *RUNTIME_PACKAGE.glob("*.py")]
        ):
            exports = _literal_exports(path)
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertEqual(len(exports), len(set(exports)))
                self.assertNotRegex(path.stem, r"(?:^|_)(?:v|phase)\d")

    def test_pyproject_packages_only_successor_and_exact_entrypoint(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["project"]["scripts"],
            {
                "governed-memory-http": (
                    "rag_engine.governed_memory.runtime.application:main"
                )
            },
        )
        self.assertEqual(
            project["tool"]["setuptools"]["packages"]["find"],
            {
                "include": [
                    "rag_engine.governed_memory",
                    "rag_engine.governed_memory.*",
                ],
                "namespaces": False,
            },
        )

    def test_docs_describe_current_inactive_boundary(self) -> None:
        normalized = " ".join(README.read_text(encoding="utf-8").split())
        for required in (
            "Phase 9J",
            "Phase 8G",
            "Phase 7C",
            "installation/current/package_manifest.json",
            "exact 76-artifact stores/controller package",
            "claim-bound install and empty-rollback controllers",
            "concrete Psycopg adapter",
            "standalone CPython inspector",
            "external Phase 9J disposable Linux proof receipt is not yet present",
            "structured LifeSwitch data",
            "Chat erasure is limited to chat threads, transcripts, attachments",
            "does not delete accounts",
            "does not substitute for the Phase 9J installation/rollback proof",
            "not reusable as current evidence",
        ):
            self.assertIn(required, normalized)
        for forbidden in (
            "Phase 9D",
            "Phase 9F",
            "postgresql_source_closure",
            "successor remains inactive, uninstalled",
        ):
            self.assertNotIn(forbidden, normalized)
        runner = (
            VALIDATION_TOOLS / "run_disposable_successor.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "readonly EXPECTED_MANIFEST_SHA256='"
            "f1be143940c3d40197a9f959e5b6d476ae70763972baa9e769419bba7c2e5a70'",
            runner,
        )
        self.assertIn(
            "[[ \"${fields[5]}\" == "
            "'phase8g_current_candidate_disposable_validated' ]]",
            runner,
        )
        self.assertNotIn(
            "phase7b_static_unit_validated_disposable_revalidation_required",
            runner,
        )
        self.assertIn(
            '"schema_version":"governed-memory-successor-disposable-run-v7"',
            runner,
        )
        self.assertIn("retired_phase6e_preliminary_proof_mode_refused", runner)
        self.assertNotIn("Phase 8A promoted proof metadata", normalized)

    def test_provider_asset_allowlist_is_exact(self) -> None:
        self.assertEqual(
            set(PROVIDER_ASSET_SOURCE_PATHS),
            {
                "provider_assets/extraction_instructions.txt",
                "provider_assets/extraction_output.schema.json",
                "provider_assets/predicate_catalog.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
