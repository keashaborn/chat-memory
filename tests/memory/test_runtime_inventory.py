"""Phase 8A promoted proof and immutable inactive-runtime identity checks."""

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
RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
HISTORICAL_PHASE6E_RUNTIME_BUILD_RECEIPT = (
    OPS / "history" / "phase6e" / "runtime_build_receipt.json"
)
DISPOSABLE_RUNNER = VALIDATION_TOOLS / "run_disposable_successor.sh"
PHASE7C_DISPOSABLE_PROOF = OPS / "phase7c_disposable_proof_receipt.json"
PHASE8A_DISPOSABLE_PROOF = OPS / "phase8a_disposable_proof_receipt.json"
POSTGRES_BOOTSTRAP = VALIDATION_TOOLS / "postgres_bootstrap.pgsql"
SCHEMA_CONTRACT = ROOT / "governed-memory-migrations" / "schema_contract.json"
README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"

EXPECTED_SOURCE_TREE_SHA256 = (
    "610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd"
)
EXPECTED_RUNTIME_RECEIPT_SHA256 = (
    "210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2"
)
EXPECTED_DISPOSABLE_RUNNER_SHA256 = (
    "2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b"
)
EXPECTED_PROOF_EXECUTION_RUNNER_SHA256 = (
    "bd591188d0afaf4d7a65753e54648241fc1aa5ff3289f194d76d6aa07dfe449f"
)
EXPECTED_PHASE7C_PROOF_SHA256 = (
    "d4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0"
)
EXPECTED_POSTGRES_BOOTSTRAP_SHA256 = (
    "0c28d2e444cddea0b61e8ea7ac9f6084b06e2038beb06bb65e754c4712eeb857"
)
EXPECTED_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{EXPECTED_SOURCE_TREE_SHA256}/bin/python"
)

EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "admission.py",
    "api.py",
    "auth.py",
    "contracts.py",
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
    "response_defaults.py",
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
    "test_admission.py",
    "test_auth_claim_artifacts.py",
    "test_build_provenance.py",
    "test_canonical_cluster_rollback.py",
    "test_chat_memory_e2e.py",
    "test_conversation_bridge.py",
    "test_conversation_capture.py",
    "test_conversation_deletion.py",
    "test_conversation_erasure_http.py",
    "test_deletion_contracts.py",
    "test_deletion_coordinator.py",
    "test_eligibility.py",
    "test_exclusive_cutover.py",
    "test_extraction.py",
    "test_http_api.py",
    "test_http_auth.py",
    "test_http_live_auth_mapping.py",
    "test_http_runtime.py",
    "test_http_service.py",
    "test_http_store.py",
    "test_https_transport.py",
    "test_inactive_installation_package.py",
    "test_inactive_installation_controller.py",
    "test_installation_authority.py",
    "test_intake_boundary.py",
    "test_lifecycle.py",
    "test_once_worker.py",
    "test_openai_adapters.py",
    "test_pilot_marker.py",
    "test_projection.py",
    "test_prompt_and_binding.py",
    "test_qdrant_adapter.py",
    "test_qdrant_transport.py",
    "test_release_contracts.py",
    "test_response_defaults.py",
    "test_response_provenance.py",
    "test_response_provider.py",
    "test_response_runtime.py",
    "test_retrieval.py",
    "test_retrieval_calibration.py",
    "test_runtime_inventory.py",
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


class Phase8ARuntimeInventoryTests(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        return json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))

    def test_source_inventory_and_runtime_identity_are_exact(self) -> None:
        self.assertEqual(_source_tree_sha256(ROOT), EXPECTED_SOURCE_TREE_SHA256)
        receipt = json.loads(RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii"))
        self.assertEqual(
            hashlib.sha256(RUNTIME_BUILD_RECEIPT.read_bytes()).hexdigest(),
            EXPECTED_RUNTIME_RECEIPT_SHA256,
        )
        self.assertEqual(
            receipt["schema_version"], "governed-memory-runtime-build-receipt-v1"
        )
        self.assertEqual(receipt["source_tree_sha256"], EXPECTED_SOURCE_TREE_SHA256)
        self.assertEqual(receipt["candidate_python"], EXPECTED_CANDIDATE_PYTHON)
        self.assertFalse(receipt["candidate_python_is_symlink"])
        self.assertEqual(receipt["network_calls"], 0)
        self.assertEqual(receipt["provider_calls"], 0)
        self.assertFalse(receipt["persistent_resources_created"])
        self.assertFalse(receipt["production_state_changed"])
        self.assertFalse(receipt["legacy_environment_imported"])

    def test_runtime_manifest_preserves_phase8a_at_execution_snapshot(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["phase"],
            "phase8a_inactive_installation_controller_packaged_proof_pending_"
            "activation_blocked",
        )
        self.assertFalse(manifest["production_state_changed"])
        self.assertFalse(manifest["legacy_imports_allowed"])
        activation = manifest["activation"]
        self.assertFalse(activation["production_authorized"])
        for key in (
            "installed_services",
            "running_services",
            "enabled_services",
            "installed_timers",
            "enabled_timers",
        ):
            self.assertEqual(activation[key], [])
        validation = manifest["disposable_validation"]
        self.assertEqual(
            validation["scope"], "successor_disposable_only"
        )
        self.assertTrue(validation["current_full_proof_complete"])
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
        self.assertFalse(
            validation["current_runner_execution_attested_by_phase7c_proof"]
        )
        self.assertEqual(
            validation["current_runner_post_proof_change_scope"],
            "metadata_only_expected_manifest_sha256_and_validation_state_rebind",
        )
        self.assertFalse(
            validation["current_runner_proof_execution_semantics_changed"]
        )
        self.assertEqual(
            validation["current_proof_receipt"],
            "ops/governed_memory/phase7c_disposable_proof_receipt.json",
        )
        self.assertEqual(
            validation["current_proof_receipt_sha256"],
            EXPECTED_PHASE7C_PROOF_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(PHASE7C_DISPOSABLE_PROOF.read_bytes()).hexdigest(),
            EXPECTED_PHASE7C_PROOF_SHA256,
        )
        self.assertTrue(PHASE7C_DISPOSABLE_PROOF.is_file())
        self.assertFalse(PHASE7C_DISPOSABLE_PROOF.is_symlink())
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
        controller = manifest["installation_controller_validation"]
        self.assertEqual(controller["scope"], "phase8a_synthetic_controller_only")
        self.assertEqual(
            controller["state"],
            "packaged_proof_pending_not_installed_not_authorized",
        )
        self.assertEqual(controller["package_artifact_count"], 59)
        self.assertEqual(controller["required_disposable_scenario_count"], 336)
        self.assertEqual(
            controller["installation_decision_receipt_schema_version"],
            "governed-memory-installation-decision-receipt-v2",
        )
        self.assertEqual(
            controller["phase8b_migration_execution_contract"],
            {
                "psql_variable_name": "governed_memory_inactive_installation",
                "psql_variable_value": "on",
                "canonical_roles_preflight_requires_variable": True,
                "canonical_migrations": [
                    "governed_memory_foundation_0001",
                    "governed_memory_owner_claim_detail_0003",
                    "governed_memory_pilot_marker_0004",
                ],
                "source_postgresql_steps": [],
                "source_conversation_bridge_included": False,
                "omission_defaults_to_active_mode_and_invalidates_inactive_installation": (
                    True
                ),
                "evaluator_verifies_execution": False,
            },
        )
        for source_count_key in (
            "phase8b_source_connection_count_required",
            "phase8b_source_catalog_read_count_required",
            "phase8b_source_application_row_read_count_required",
            "phase8b_source_write_count_required",
        ):
            self.assertEqual(controller[source_count_key], 0, source_count_key)
        self.assertTrue(
            controller[
                "phase8b_all_exact_targets_required_in_every_observation_stage"
            ]
        )
        self.assertEqual(
            controller["phase8b_required_empty_rollback_retained_targets"],
            [
                "install_root",
                "environment_root",
                "runtime_environment_root",
                "state_root",
                "backup_root",
                "legacy_secret_quarantine_path_template",
            ],
        )
        self.assertEqual(
            controller["phase8b_required_application_unit_postflight_state"],
            "installed_disabled_inactive",
        )
        self.assertEqual(
            controller["phase8b_required_store_supervisor_postflight_state"],
            "installed_enabled_active_store_only",
        )
        self.assertFalse(controller["phase8b_store_supervisor_is_application_runtime"])
        self.assertEqual(
            controller["phase8b_installation_blockers"],
            [
                "separate_phase8b_dormant_installation_approval_required",
                (
                    "final_candidate_commit_tree_package_controller_and_plan_not_"
                    "externally_signed"
                ),
                "external_owner_public_key_trust_anchor_not_installed",
                "legacy_secret_exact_transition_receipt_absent",
                "fresh_store_secret_generation_receipt_absent",
                "persistent_qdrant_digest_not_authorized",
                "runtime_wheel_and_offline_dependency_wheelhouse_not_packaged",
                "store_supervisor_artifact_not_packaged",
                "encrypted_backup_restore_adapter_artifact_not_packaged",
                "trusted_clock_and_atomic_single_use_nonce_claim_not_packaged",
                "canonical_global_execution_lock_not_packaged",
                "external_journal_seal_anchor_not_packaged",
                "exact_live_probe_adapter_not_packaged",
                "same_filesystem_quarantine_preflight_adapter_not_packaged",
                "canonical_cluster_rollback_not_disposable_postgresql_executed",
                "linux_execution_backend_hard_disabled",
                "exact_live_preflight_receipt_absent",
            ],
        )
        self.assertFalse(controller["current_disposable_proof_executed"])
        self.assertFalse(controller["current_disposable_proof_complete"])
        self.assertIsNone(controller["current_disposable_proof_result"])
        self.assertIsNone(controller["current_proof_receipt"])
        self.assertIsNone(controller["current_proof_receipt_sha256"])
        self.assertFalse(controller["live_backend_packaged"])
        self.assertFalse(controller["live_execution_surface_exposed"])
        self.assertFalse(controller["installation_authorized"])
        self.assertFalse(controller["activation_authorized"])

    def test_external_phase8a_proof_is_promoted_without_package_rewrite(self) -> None:
        wrapper = json.loads(PHASE8A_DISPOSABLE_PROOF.read_text(encoding="utf-8"))
        self.assertEqual(
            wrapper["state"],
            "disposable_proof_passed_metadata_promoted_inactive_not_"
            "installed_not_authorized",
        )
        self.assertEqual(
            wrapper["immutable_package_snapshot"]["package_artifact_count"], 59
        )
        self.assertTrue(
            wrapper["immutable_package_snapshot"][
                "embedded_proof_pending_labels_are_frozen_execution_snapshot"
            ]
        )
        self.assertEqual(wrapper["proof_summary"]["controller_scenario_count"], 336)
        self.assertEqual(
            wrapper["proof_summary"]["postgresql16_positive_scenario_count"], 21
        )
        self.assertEqual(
            wrapper["proof_summary"]["postgresql16_refusal_scenario_count"], 5
        )
        self.assertFalse(wrapper["authority"]["installation_authorized"])
        self.assertFalse(wrapper["authority"]["activation_authorized"])
        self.assertTrue(
            wrapper["deferred_revalidation"][
                "phase7c_failure_path_revalidation_required"
            ]
        )

    def test_current_validation_runtime_is_exact(self) -> None:
        runtime = self.load_manifest()["validation_runtime"]
        self.assertEqual(runtime["current_source_tree_sha256"], EXPECTED_SOURCE_TREE_SHA256)
        self.assertEqual(runtime["current_candidate_python"], EXPECTED_CANDIDATE_PYTHON)
        self.assertEqual(
            runtime["current_build_receipt_sha256"],
            EXPECTED_RUNTIME_RECEIPT_SHA256,
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
        self.assertFalse(ingestion["source_erasure_legacy_project_rows_deleted"])
        self.assertFalse(ingestion["source_erasure_unclassified_side_effects_allowed"])

    def test_logging_and_frontend_blockers_are_truthful(self) -> None:
        manifest = self.load_manifest()
        http = manifest["http_runtime"]
        self.assertEqual(http["default_mode"], "off")
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
            HISTORICAL_PHASE6E_RUNTIME_BUILD_RECEIPT.read_bytes()
        ).hexdigest()
        runner = DISPOSABLE_RUNNER.read_text(encoding="utf-8")
        self.assertEqual(current_sha256, EXPECTED_RUNTIME_RECEIPT_SHA256)
        self.assertEqual(
            historical_sha256,
            "cfe7a60c2e69de5a1603f86717f72d093f6fc2e623c2cb627008dbabb97c1c86",
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

    def test_schema_scope_records_current_disposable_validation(self) -> None:
        schema = json.loads(SCHEMA_CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["validation_scope"],
            {
                "scope": "successor_disposable_only",
                "environment": "disposable_only",
                "production_data_read": False,
                "provider_external_calls": 0,
                "production_state_changed": False,
            },
        )
        requirements = schema["hard_requirements"]
        self.assertTrue(requirements["current_disposable_successor_validation_complete"])
        self.assertNotIn("phase7b_disposable_revalidation_required", requirements)
        self.assertTrue(schema["claim_detail"]["disposable_validated"])
        self.assertTrue(schema["pilot_marker"]["disposable_validated"])
        self.assertEqual(
            requirements["production_activation_blockers"],
            self.load_manifest()["activation"]["blockers"],
        )
        erasure_contract = schema["interface_contracts"]["chat_source_erasure"]
        self.assertIn("Phase 7C disposable PostgreSQL and Qdrant deletion proof passed", erasure_contract)
        self.assertIn("no production route, membership, service, or store", erasure_contract)

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
        self.assertEqual(_file_names(TESTS), EXPECTED_TEST_FILES)
        self.assertEqual(
            _file_names(INTEGRATION_TESTS),
            {
                "__init__.py",
                "local_jwks_server.py",
                "test_conversation_deletion_disposable.py",
                "test_governed_memory_http_vertical_slice.py",
            },
        )
        self.assertEqual(
            _file_names(VALIDATION_TOOLS),
            {
                "postgres_bootstrap.pgsql",
                "run_disposable_installation_controller.py",
                "run_disposable_successor.sh",
                "runtime_packages.json",
                "verify_migration_manifest.py",
            },
        )
        self.assertEqual(
            _file_names(RELEASE_TOOLS),
            {"__init__.py", "build_candidate_runtime.py", "release_guard.py"},
        )
        self.assertEqual(
            _file_names(INSTALL_TOOLS),
            {
                "__init__.py",
                "authority.py",
                "controller.py",
                "controller_linux.py",
                "inactive_installation.py",
                "journal.py",
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

    def test_docs_describe_phase8a_promoted_and_phase7c_retained_proof(self) -> None:
        normalized = " ".join(README.read_text(encoding="utf-8").split())
        for required in (
            "Phase 8A promoted proof metadata",
            "Retained Phase 7C successor proof",
            "CPython 3.12.3 Linux runtime",
            "Attempt 4 passed against pre-promotion candidate commit",
            "structured LifeSwitch data",
            "do not authorize installation, rollback, activation, or cleanup",
            "zero source PostgreSQL connections",
            "All 336 closed controller scenarios passed",
            "21 positive and 5 refusal scenarios passed",
            "roles_preflight.pgsql:39",
            "0002_conversation_bridge/forward.pgsql:20",
        ):
            self.assertIn(required, normalized)
        runner = (
            VALIDATION_TOOLS / "run_disposable_successor.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "readonly EXPECTED_MANIFEST_SHA256='"
            "57ea2a0b151b0ac4a84f0df86041e418d1b1a7843cbfd9281175e34500e15150'",
            runner,
        )
        self.assertIn("[[ \"${fields[5]}\" == 'disposable_validated' ]]", runner)
        self.assertNotIn(
            "phase7b_static_unit_validated_disposable_revalidation_required",
            runner,
        )
        self.assertIn("retired_phase6e_preliminary_proof_mode_refused", runner)
        self.assertNotIn("phase6e_disposable_deletion_validated_inactive_activation_blocked", README.read_text(encoding="utf-8"))

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
