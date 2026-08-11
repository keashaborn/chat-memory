"""Source-inventory and installed candidate-runtime identity checks."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import tomllib
import unicodedata
import unittest

from rag_engine.governed_memory.contracts import ContractViolation, canonical_sha256
from rag_engine.governed_memory.auth import ActorRole
from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.repository import GovernedMemoryRepository
from tools.governed_memory_release.build_candidate_runtime import (
    PROVIDER_ASSET_SOURCE_PATHS,
    _package_source_material,
    _source_tree_sha256,
)


FIXTURE_PROVENANCE = "synthetic-governed-memory-successor"
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "rag_engine" / "governed_memory"
RUNTIME_PACKAGE = PACKAGE / "runtime"
PROVIDER_ASSETS = PACKAGE / "provider_assets"
MANIFEST = ROOT / "ops" / "governed_memory" / "runtime_manifest.json"
RUNTIME_LOCK = ROOT / "ops" / "governed_memory" / "runtime-requirements.lock"
BUILD_LOCK = ROOT / "ops" / "governed_memory" / "build-requirements.lock"
SUCCESSOR_README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"
SCHEMA_CONTRACT = ROOT / "governed-memory-migrations" / "schema_contract.json"
INTEGRATION_TESTS = ROOT / "tests" / "memory_integration"
VALIDATION_TOOLS = ROOT / "tools" / "governed_memory_validation"
RELEASE_TOOLS = ROOT / "tools" / "governed_memory_release"
CLEAN_SUCCESSOR_DOCS = ROOT / "docs" / "memory" / "clean_successor"
RUNTIME_PACKAGES = VALIDATION_TOOLS / "runtime_packages.json"
RUNTIME_BUILD_RECEIPT = ROOT / "ops" / "governed_memory" / "runtime_build_receipt.json"
PHASE6B_DISPOSABLE_PROOF_RECEIPT = (
    ROOT / "ops" / "governed_memory" / "phase6b_disposable_proof_receipt.json"
)
HISTORICAL_PHASE5 = ROOT / "ops" / "governed_memory" / "history" / "phase5"
HISTORICAL_RUNTIME_BUILD_RECEIPT = HISTORICAL_PHASE5 / "runtime_build_receipt.json"
HISTORICAL_DISPOSABLE_PROOF_RECEIPT = (
    HISTORICAL_PHASE5 / "disposable_proof_receipt.json"
)
HISTORICAL_PHASE6B_RUNTIME_BUILD_RECEIPT = (
    ROOT
    / "ops"
    / "governed_memory"
    / "history"
    / "phase6b"
    / "runtime_build_receipt.json"
)

EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "admission.py",
    "api.py",
    "auth.py",
    "contracts.py",
    "conversation_deletion.py",
    "conversation_capture.py",
    "conversation_source.py",
    "eligibility.py",
    "deletion_contracts.py",
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
    "response_defaults.py",
    "response_provenance.py",
    "response_provider.py",
    "response_runtime.py",
    "retrieval.py",
    "successor_live_authority.py",
    "worker.py",
}

EXPECTED_RUNTIME_PACKAGE_FILES = {
    "__init__.py",
    "__main__.py",
    "application.py",
    "calibration.py",
    "deletion_coordinator.py",
    "deletion_postgres.py",
    "environment.py",
    "once_worker.py",
    "pilot_marker.py",
    "qdrant_adapter.py",
    "live_supabase.py",
    "https_transport.py",
    "openai_adapters.py",
    "qdrant_transport.py",
    "worker_application.py",
    "worker_bridge.py",
    "worker_postgres.py",
}

EXPECTED_PROVIDER_ASSET_FILES = {
    "__init__.py",
    "extraction_instructions.txt",
    "extraction_output.schema.json",
    "predicate_catalog.json",
}

EXPECTED_TEST_FILES = {
    "test_admission.py",
    "test_chat_memory_e2e.py",
    "test_conversation_bridge.py",
    "test_conversation_capture.py",
    "test_conversation_deletion.py",
    "test_deletion_contracts.py",
    "test_deletion_coordinator.py",
    "test_eligibility.py",
    "test_exclusive_cutover.py",
    "test_extraction.py",
    "test_https_transport.py",
    "test_http_api.py",
    "test_http_auth.py",
    "test_http_live_auth_mapping.py",
    "test_http_runtime.py",
    "test_http_service.py",
    "test_http_store.py",
    "test_intake_boundary.py",
    "test_lifecycle.py",
    "test_openai_adapters.py",
    "test_once_worker.py",
    "test_auth_claim_artifacts.py",
    "test_build_provenance.py",
    "test_pilot_marker.py",
    "test_projection.py",
    "test_prompt_and_binding.py",
    "test_qdrant_adapter.py",
    "test_qdrant_transport.py",
    "test_release_contracts.py",
    "test_response_provider.py",
    "test_response_defaults.py",
    "test_response_provenance.py",
    "test_response_runtime.py",
    "test_retrieval.py",
    "test_retrieval_calibration.py",
    "test_runtime_inventory.py",
    "test_runtime_release.py",
    "test_successor_live_authority.py",
    "test_schema_and_rls.py",
    "test_worker_recovery.py",
    "test_worker_application.py",
    "test_worker_bridge.py",
    "test_worker_postgres.py",
}

EXPECTED_INTEGRATION_FILES = {
    "__init__.py",
    "local_jwks_server.py",
    "test_conversation_deletion_disposable.py",
    "test_governed_memory_http_vertical_slice.py",
}

EXPECTED_VALIDATION_TOOL_FILES = {
    "postgres_bootstrap.pgsql",
    "run_disposable_successor.sh",
    "runtime_packages.json",
    "verify_migration_manifest.py",
}

EXPECTED_RELEASE_TOOL_FILES = {
    "__init__.py",
    "build_candidate_runtime.py",
    "release_guard.py",
}

EXPECTED_CLEAN_SUCCESSOR_DOC_FILES = {
    "ACTIVATION.md",
    "README.md",
    "VALIDATION.md",
}

EXPECTED_RUNTIME_PACKAGES = {
    "schema_version": "governed-memory-validation-runtime-v2",
    "python_implementation": "CPython",
    "python_version": "3.12.3",
    "runtime_lock_path": "ops/governed_memory/runtime-requirements.lock",
    "runtime_lock_sha256": "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365",
    "candidate_project": {
        "name": "governed-memory-successor",
        "version": "0.0.0",
    },
    "packages": {
        "annotated-doc": "0.0.5",
        "annotated-types": "0.7.0",
        "anyio": "4.11.0",
        "asyncpg": "0.30.0",
        "cffi": "2.1.0",
        "click": "8.3.0",
        "cryptography": "49.0.0",
        "fastapi": "0.120.4",
        "h11": "0.16.0",
        "idna": "3.11",
        "pycparser": "3.0",
        "pydantic": "2.12.3",
        "pydantic-core": "2.41.4",
        "PyJWT": "2.13.0",
        "sniffio": "1.3.1",
        "starlette": "0.49.2",
        "typing-extensions": "4.15.0",
        "typing-inspection": "0.4.2",
        "uvicorn": "0.38.0",
    },
}

EXPECTED_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_SOURCE_TREE_SHA256 = _source_tree_sha256()
EXPECTED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{EXPECTED_SOURCE_TREE_SHA256}/bin/python"
)
PHASE6B_SOURCE_TREE_SHA256 = (
    "d08cc71966beec1e31e107c08b71daa4e51daf3c0b3b6f5ef584ef8bae41c0e0"
)
PHASE6B_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{PHASE6B_SOURCE_TREE_SHA256}/bin/python"
)
RECORDED_SOURCE_TREE_SHA256 = (
    "af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b"
)
RECORDED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-phase5-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{RECORDED_SOURCE_TREE_SHA256}/bin/python"
)
EXPECTED_PROJECT_WHEEL_SHA256 = (
    "58146af4097400097b1312011c591d1878904f7ac5709b0fdecd57da3fc0f8e4"
)
CURRENT_PROJECT_WHEEL_SHA256 = (
    "7f086b2687d1daf8f687ef4c4b97777096d9b337045be1a8d307f2ba659af412"
)
CURRENT_RUNTIME_BUILD_RECEIPT_SHA256 = (
    "cfe7a60c2e69de5a1603f86717f72d093f6fc2e623c2cb627008dbabb97c1c86"
)
PHASE6B_RUNTIME_BUILD_RECEIPT_SHA256 = (
    "ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69"
)

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
]


class RuntimeManifestTests(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        raw = MANIFEST.read_bytes()
        self.assertLessEqual(len(raw), 64 * 1024)
        return json.loads(raw.decode("utf-8"))

    def test_candidate_is_explicitly_uninstalled_and_content_free(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["schema_version"],
            "governed-memory-successor-runtime-manifest-v1",
        )
        self.assertEqual(
            manifest["phase"],
            "phase6e_inactive_sealed_candidate_"
            "disposable_deletion_proof_pending",
        )
        self.assertFalse(manifest["production_state_changed"])
        self.assertEqual(
            manifest["authority"],
            {
                "database": "governed_memory",
                "database_target": "127.0.0.1:55432",
                "conversation_database": "memory",
                "conversation_database_target": "127.0.0.1:5432",
                "conversation_application_login": "brains_app",
                "conversation_worker_login": "governed_memory_worker",
                "conversation_bridge": "memory_ingest_private.memory_ingest_outbox",
                "worker_pilot_identity_environment": [
                    "GOVERNED_MEMORY_EXPECTED_PILOT_ID",
                    "GOVERNED_MEMORY_EXPECTED_PILOT_CONTRACT_SHA256",
                    "GOVERNED_MEMORY_EXPECTED_AUTHORIZATION_RECEIPT_SHA256",
                ],
                "qdrant_target": "127.0.0.1:6343",
                "qdrant_collection": "governed_memory_9a54cf123493_000001",
                "qdrant_alias": "governed_memory_active",
            },
        )
        self.assertEqual(
            manifest["ingestion"],
            {
                "capture_mode_default": "off",
                "capture_owner_allowlist_in_repository": False,
                "writer_membership_granted_in_production": False,
                "historical_import": False,
                "historical_backfill": False,
                "attachment_content_release_1": False,
                "requires_post_cutover_user_message": True,
                "worker_base_conversation_table_select": False,
                "phase6b_context_required_policy": (
                    "fresh_count_zero_two_exact_marks_re_leased_count_one_one_"
                    "exact_mark_then_terminal_unresolved_one_receipt_only_"
                    "successor_read_zero_successor_writes_provider_embedding_"
                    "vector_calls"
                ),
                "attachment_invariant": (
                    "enqueue_lease_and_source_read_require_no_owner_thread_"
                    "message_attachment_row"
                ),
                "source_erasure_status": (
                    "phase6d_inactive_static_candidate_"
                    "phase6e_disposable_proof_pending"
                ),
                "source_erasure_scope": (
                    "exact_materialized_chat_targets_and_matching_chat_"
                    "attachments_bridge_rows_eligible_empty_chat_threads_and_"
                    "successor_memory_derived_from_exact_targets_only"
                ),
                "source_erasure_selectors": [
                    "thread",
                    "message_tail",
                    "recent",
                    "all_conversations",
                ],
                "source_erasure_direct_delete_roots": [
                    "public.chat_log",
                    "public.chat_attachments",
                    "public.threads",
                ],
                "source_erasure_transient_target_tables": [
                    "memory_ingest_private.source_erasure_target",
                    "memory_ingest_private.source_erasure_thread_target",
                ],
                "source_erasure_permanent_tombstone_tables": [
                    "memory_ingest_private.source_erasure_message_tombstone",
                    "memory_ingest_private.source_erasure_thread_tombstone",
                ],
                "source_erasure_tombstone_identity_scope": (
                    "global_message_and_thread_uuid"
                ),
                "source_erasure_targets_retained_until": (
                    "conversation_deletion_final_receipt_acknowledged"
                ),
                "source_erasure_tombstones_immutable": True,
                "source_erasure_runtime_catalog_attestation": (
                    "exact_mutated_relation_schema_foreign_key_trigger_rule_"
                    "and_inheritance_inventory"
                ),
                "source_erasure_allowed_auxiliary_effects": [
                    "public.active_thread_selection",
                    "trusted_web.response_transcript_v1",
                ],
                "source_erasure_auxiliary_effect_authority": (
                    "exact_named_validated_on_delete_cascade_composite_owner_"
                    "thread_foreign_keys_only"
                ),
                "source_erasure_validated_auxiliary_foreign_keys": {
                    "public.active_thread_selection": [
                        {
                            "constraint_name": (
                                "active_thread_selection_owner_thread_fk"
                            ),
                            "child_columns": ["owner_user_id", "thread_id"],
                            "parent_table": "public.threads",
                            "parent_columns": ["owner_user_id", "id"],
                            "validated": True,
                            "delete_action": "cascade",
                        }
                    ],
                    "trusted_web.response_transcript_v1": [
                        {
                            "constraint_name": (
                                "response_transcript_v1_user_chat_log_id_fkey"
                            ),
                            "child_columns": [
                                "user_chat_log_id",
                                "owner_user_id",
                                "thread_id",
                            ],
                            "parent_table": "public.chat_log",
                            "parent_columns": [
                                "id",
                                "owner_user_id",
                                "thread_id",
                            ],
                            "validated": True,
                            "delete_action": "cascade",
                        },
                        {
                            "constraint_name": (
                                "response_transcript_v1_assistant_chat_log_id_fkey"
                            ),
                            "child_columns": [
                                "assistant_chat_log_id",
                                "owner_user_id",
                                "thread_id",
                            ],
                            "parent_table": "public.chat_log",
                            "parent_columns": [
                                "id",
                                "owner_user_id",
                                "thread_id",
                            ],
                            "validated": True,
                            "delete_action": "cascade",
                        },
                    ],
                },
                "source_erasure_weak_single_column_transcript_foreign_keys_allowed": False,
                "source_erasure_unknown_dependency_action": (
                    "fail_closed_before_delete_on_unknown_foreign_key_"
                    "delete_trigger_delete_rule_or_inheritance"
                ),
                "source_erasure_unclassified_side_effects_allowed": False,
                "source_erasure_legacy_capture_trigger_required": False,
                "source_erasure_legacy_capture_trigger_if_present": (
                    "exact_disabled_identity_only"
                ),
                "source_erasure_memory_only_or_account_wide_memory_selector_allowed": False,
                "source_erasure_structured_lifeswitch_data_or_accounts_deleted": False,
                "source_erasure_legacy_project_rows_deleted": False,
                "source_erasure_content_free_consent_security_audit_receipts_retained": True,
                "pilot_capture_limit": (
                    "owner_locked_twenty_rows_all_states_rolling_24h_exact_"
                    "replay_no_new_slot_typed_limit_result_focused_static_and_"
                    "adapter_tested_not_disposable_runtime_exercised"
                ),
                "qdrant_unavailable_policy": (
                    "predispatch_qdrant_unavailability_retryable_post_embedding_"
                    "dispatch_failure_terminal_without_embedding_resend_"
                    "canonical_postgresql_claim_retained_later_same_claim_"
                    "projection_blocked_explicit_projection_reconciliation_"
                    "required"
                ),
            },
        )
        self.assertEqual(
            set(manifest),
            {
                "schema_version", "phase", "python_runtime", "validation_runtime",
                "authority", "infrastructure", "http_runtime", "activation",
                "release_guard", "ingestion", "worker_adapters", "provider_policy",
                "disposable_validation", "owner_routes", "prohibited_routes",
                "calibration", "frontend_candidate", "historical_evidence",
                "legacy_imports_allowed", "production_state_changed",
            },
        )

    def test_no_service_or_timer_is_claimed_installed(self) -> None:
        activation = self.load_manifest()["activation"]
        self.assertEqual(activation["installed_services"], [])
        self.assertEqual(activation["enabled_services"], [])
        self.assertEqual(activation["running_services"], [])
        self.assertEqual(activation["installed_timers"], [])
        self.assertEqual(activation["enabled_timers"], [])
        self.assertEqual(
            activation["future_target_services"],
            [
                "governed-memory-http.service",
                "governed-memory-worker.service",
            ],
        )
        self.assertEqual(
            activation["shipped_inactive_templates"],
            [
                "ops/governed_memory/systemd/governed-memory-http.service.in",
                "ops/governed_memory/systemd/governed-memory-worker.service.in",
            ],
        )
        worker_unit = (
            ROOT / "ops" / "governed_memory" / "systemd"
            / "governed-memory-worker.service.in"
        ).read_text(encoding="utf-8")
        self.assertIn("GOVERNED_MEMORY_WORKER_MODE=off", worker_unit)
        self.assertIn("Type=oneshot", worker_unit)
        self.assertNotIn("[Install]", worker_unit)
        self.assertNotIn("WantedBy=", worker_unit)

    def test_worker_identity_is_bound_to_three_explicit_environment_values(self) -> None:
        authority = self.load_manifest()["authority"]
        self.assertEqual(
            authority["worker_pilot_identity_environment"],
            [
                "GOVERNED_MEMORY_EXPECTED_PILOT_ID",
                "GOVERNED_MEMORY_EXPECTED_PILOT_CONTRACT_SHA256",
                "GOVERNED_MEMORY_EXPECTED_AUTHORIZATION_RECEIPT_SHA256",
            ],
        )

    def test_provider_policy_records_zero_disposable_external_calls(self) -> None:
        policy = self.load_manifest()["provider_policy"]
        self.assertFalse(policy["import_time_calls"])
        self.assertFalse(policy["production_calls_authorized"])
        self.assertEqual(policy["disposable_external_calls"], 0)
        self.assertEqual(policy["generation_calls_per_exact_attempt"], 1)
        adapters = self.load_manifest()["worker_adapters"]
        self.assertEqual(adapters["provider"], "strict_fake_tested_zero_real_calls")
        self.assertEqual(
            adapters["embedding"],
            "strict_3072_fake_tested_durable_request_dispatch_marker_"
            "zero_real_calls_current_disposable_proof_pending",
        )
        self.assertEqual(
            adapters["qdrant"],
            "exact_fake_tested_phase6b_real_disposable_proof_historical_"
            "current_phase6e_proof_pending",
        )
        self.assertEqual(adapters["algorithm"], "implemented_fake_tested")
        self.assertEqual(
            adapters["cli_composition"],
            "implemented_inactive_static_candidate_"
            "phase6e_disposable_proof_pending",
        )
        self.assertEqual(
            adapters["cross_process_singleton"],
            "postgresql_session_advisory_lock_static_candidate_"
            "phase6e_proof_pending",
        )
        self.assertEqual(
            adapters["conversation_bridge"],
            "two_database_rpc_only_inactive_static_candidate_"
            "phase6e_disposable_proof_pending",
        )
        self.assertEqual(
            adapters["scheduler"],
            "private_content_free_postgresql_sequence_cyclic_three_lane_"
            "static_candidate_phase6e_proof_pending",
        )

    def test_successor_validation_is_disposable_and_not_activation_proof(self) -> None:
        manifest = self.load_manifest()
        validation = manifest["disposable_validation"]
        self.assertEqual(validation["scope"], "successor_disposable_only")
        self.assertFalse(validation["production_data_read"])
        self.assertEqual(validation["provider_external_calls"], 0)
        self.assertEqual(
            validation["evidence_status"],
            "phase6b_proof_historical_noncurrent_"
            "phase6e_deletion_proof_pending",
        )
        self.assertFalse(validation["current_full_proof_complete"])
        self.assertIsNone(validation["current_candidate_python"])
        self.assertIsNone(validation["current_proof_receipt"])
        self.assertIsNone(validation["current_proof_receipt_sha256"])
        self.assertTrue(validation["final_resources_absent"])
        self.assertFalse(validation["resource_cleanup_complete"])
        self.assertFalse(validation["all_owner_routes_invoked"])
        self.assertFalse(validation["alternating_owner_pool_isolation"])
        self.assertIsNone(validation["owner_pool_max_size"])
        self.assertFalse(
            validation["qdrant_v1_19_0_real_disposable_compatibility_verified"]
        )
        self.assertFalse(validation["pilot_marker_disposable_proof_complete"])
        self.assertFalse(validation["worker_runtime_composition_validated"])
        self.assertEqual(
            validation["worker_runtime_composition_status"],
            "phase6d_inactive_static_candidate_"
            "phase6e_disposable_proof_pending",
        )
        self.assertFalse(validation["worker_cross_process_singleton_validated"])
        self.assertEqual(
            validation["worker_cross_process_singleton_status"],
            "phase6d_inactive_static_candidate_"
            "phase6e_disposable_proof_pending",
        )
        self.assertFalse(validation["deletion_coordination_disposable_proof_complete"])
        self.assertFalse(validation["semantic_threshold_calibrated"])
        self.assertFalse(validation["persistent_resources_created"])
        self.assertEqual(
            manifest["historical_evidence"],
            {
                "phase5_runtime_build_receipt": (
                    "ops/governed_memory/history/phase5/runtime_build_receipt.json"
                ),
                "phase5_disposable_proof_receipt": (
                    "ops/governed_memory/history/phase5/disposable_proof_receipt.json"
                ),
                "phase6b_runtime_build_receipt": (
                    "ops/governed_memory/history/phase6b/"
                    "runtime_build_receipt.json"
                ),
                "phase6b_runtime_build_receipt_sha256": (
                    PHASE6B_RUNTIME_BUILD_RECEIPT_SHA256
                ),
                "phase6b_disposable_proof_receipt": (
                    "ops/governed_memory/phase6b_disposable_proof_receipt.json"
                ),
                "phase6b_disposable_proof_receipt_sha256": hashlib.sha256(
                    PHASE6B_DISPOSABLE_PROOF_RECEIPT.read_bytes()
                ).hexdigest(),
                "phase6b_proof_status": (
                    "historical_noncurrent_after_phase6d_source_and_"
                    "migration_changes"
                ),
                "reusable_for_current_candidate": False,
            },
        )
        self.assertFalse(manifest["activation"]["production_authorized"])
        self.assertFalse(
            manifest["infrastructure"][
                "qdrant_real_disposable_compatibility_verified"
            ]
        )
        self.assertIsNone(
            manifest["infrastructure"]["qdrant_persistent_pilot_image"]
        )
        self.assertFalse(
            manifest["infrastructure"]["qdrant_persistent_resource_created"]
        )
        http_runtime = manifest["http_runtime"]
        self.assertTrue(http_runtime["session_id_required"])
        self.assertEqual(
            http_runtime["supabase_auth_sessions_rpc_status"],
            "staged_candidate_not_installed",
        )
        self.assertFalse(http_runtime["supabase_auth_sessions_rpc_live_verified"])
        self.assertTrue(http_runtime["owner_claim_fact_detail_implemented"])
        self.assertFalse(
            http_runtime["owner_claim_fact_detail_disposable_proof_complete"]
        )
        self.assertIn(
            "semantic_calibration_artifact_unapproved_retrieval_off",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "supabase_auth_sessions_rpc_not_installed_or_live_verified",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "qdrant_v1_19_0_real_disposable_compatibility_pending",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "durable_pilot_marker_candidate_not_applied_or_disposable_proved",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "owner_claim_fact_detail_api_not_implemented",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "phase6b_migration_contract_disposable_proof_pending",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "worker_cross_process_singleton_not_implemented",
            manifest["activation"]["blockers"],
        )
        self.assertEqual(
            manifest["calibration"],
            {
                "approval_binding": (
                    "independent_expected_artifact_and_approval_receipt_sha256"
                ),
                "artifact_status": "unapproved",
                "retrieval_enabled": False,
            },
        )
        self.assertEqual(
            manifest["frontend_candidate"],
            {
                "git_commit": "6d80ba00c93bfa74c4e44604a41ac36e779e93a9",
                "git_commit_short": "6d80ba",
                "git_tree": "f5e72973cff3e9dc28ecb9555c974ae93672955b",
                "built": True,
                "deployed": False,
                "authenticated_visual_qa_complete": False,
            },
        )
        self.assertIn(
            "legacy_memory_owner_scoped_read_write_shadow_quiescence_not_proved",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "legacy_project_memory_thread_dependencies_not_separated",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "trusted_web_transcript_composite_owner_thread_lineage_not_installed",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "legacy_chat_owner_thread_lineage_not_remediated",
            manifest["activation"]["blockers"],
        )
        self.assertEqual(
            manifest["release_guard"],
            {
                "create_allowed": False,
                "create_refusal_code": "activation_blockers_open",
                "cleanup_allowed": False,
                "cleanup_refusal_code": "authorization_missing",
                "commands_executed": 0,
            },
        )

    def test_python_runtime_is_exact_candidate_contract(self) -> None:
        manifest = self.load_manifest()
        runtime = manifest["python_runtime"]
        self.assertEqual(
            runtime,
            {
                "implementation": "CPython",
                "required": "3.12.*",
                "validated_candidate": "3.12.3",
                "platform": "linux_x86_64",
            },
        )
        readme = SUCCESSOR_README.read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        self.assertIn("requires CPython 3.12.x", normalized_readme)
        self.assertIn("CPython 3.12.3", normalized_readme)

    def test_validation_runtime_is_candidate_owned_and_hash_locked(self) -> None:
        runtime_packages = json.loads(RUNTIME_PACKAGES.read_text(encoding="utf-8"))
        self.assertEqual(runtime_packages, EXPECTED_RUNTIME_PACKAGES)
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["validation_runtime"],
            {
                "manifest": "tools/governed_memory_validation/runtime_packages.json",
                "runtime_lock": "ops/governed_memory/runtime-requirements.lock",
                "runtime_lock_sha256": EXPECTED_RUNTIME_LOCK_SHA256,
                "build_lock": "ops/governed_memory/build-requirements.lock",
                "build_lock_sha256": EXPECTED_BUILD_LOCK_SHA256,
                "current_source_tree_sha256": EXPECTED_SOURCE_TREE_SHA256,
                "current_candidate_python": EXPECTED_CANDIDATE_PYTHON,
                "current_candidate_python_sha256": (
                    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
                ),
                "current_project_wheel_sha256": CURRENT_PROJECT_WHEEL_SHA256,
                "current_source_bound": True,
                "final_phase6d_runtime_rebuild_pending": False,
                "current_build_receipt": (
                    "ops/governed_memory/runtime_build_receipt.json"
                ),
                "current_build_receipt_sha256": (
                    CURRENT_RUNTIME_BUILD_RECEIPT_SHA256
                ),
                "current_build_receipt_present": True,
            },
        )
        self.assertNotIn(
            "candidate_owned_runtime_environment_not_built",
            manifest["activation"]["blockers"],
        )
        self.assertTrue(manifest["validation_runtime"]["current_source_bound"])
        self.assertEqual(hashlib.sha256(RUNTIME_LOCK.read_bytes()).hexdigest(), EXPECTED_RUNTIME_LOCK_SHA256)
        self.assertEqual(hashlib.sha256(BUILD_LOCK.read_bytes()).hexdigest(), EXPECTED_BUILD_LOCK_SHA256)
        self.assertTrue(RUNTIME_BUILD_RECEIPT.is_file())
        self.assertFalse(RUNTIME_BUILD_RECEIPT.is_symlink())
        self.assertEqual(
            hashlib.sha256(RUNTIME_BUILD_RECEIPT.read_bytes()).hexdigest(),
            CURRENT_RUNTIME_BUILD_RECEIPT_SHA256,
        )
        current_build_receipt = json.loads(
            RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii")
        )
        self.assertEqual(
            current_build_receipt["schema_version"],
            "governed-memory-runtime-build-receipt-v1",
        )
        self.assertEqual(
            current_build_receipt["candidate_python"],
            EXPECTED_CANDIDATE_PYTHON,
        )
        self.assertEqual(
            current_build_receipt["source_tree_sha256"],
            EXPECTED_SOURCE_TREE_SHA256,
        )
        self.assertEqual(
            current_build_receipt["project_wheel_sha256"],
            CURRENT_PROJECT_WHEEL_SHA256,
        )
        self.assertEqual(current_build_receipt["network_calls"], 0)
        self.assertEqual(current_build_receipt["provider_calls"], 0)
        self.assertFalse(current_build_receipt["production_state_changed"])
        self.assertTrue(HISTORICAL_RUNTIME_BUILD_RECEIPT.is_file())
        self.assertFalse(HISTORICAL_RUNTIME_BUILD_RECEIPT.is_symlink())
        historical_build_receipt = json.loads(
            HISTORICAL_RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii")
        )
        self.assertNotEqual(
            RUNTIME_BUILD_RECEIPT.read_bytes(),
            HISTORICAL_RUNTIME_BUILD_RECEIPT.read_bytes(),
        )
        self.assertNotIn("phase5", current_build_receipt["candidate_python"])
        self.assertNotIn("phase5", current_build_receipt["project_wheel"])
        self.assertEqual(
            historical_build_receipt["candidate_python"],
            RECORDED_CANDIDATE_PYTHON,
        )
        self.assertEqual(
            historical_build_receipt["source_tree_sha256"],
            RECORDED_SOURCE_TREE_SHA256,
        )
        self.assertEqual(
            historical_build_receipt["project_wheel_sha256"],
            EXPECTED_PROJECT_WHEEL_SHA256,
        )
        self.assertNotEqual(
            PHASE6B_SOURCE_TREE_SHA256,
            EXPECTED_SOURCE_TREE_SHA256,
        )
        historical_proof = json.loads(
            HISTORICAL_DISPOSABLE_PROOF_RECEIPT.read_text(encoding="ascii")
        )
        self.assertEqual(historical_proof["phase"], "phase5")
        self.assertFalse(historical_proof["reusable_for_current_candidate"])
        self.assertEqual(historical_proof["proof_receipt"]["result"], "passed")
        self.assertEqual(
            historical_proof["proof_receipt"]["provider_external_calls"],
            0,
        )
        phase6b_proof = json.loads(
            PHASE6B_DISPOSABLE_PROOF_RECEIPT.read_text(encoding="ascii")
        )
        self.assertEqual(
            hashlib.sha256(PHASE6B_DISPOSABLE_PROOF_RECEIPT.read_bytes()).hexdigest(),
            "55e3993e5f403b095a804bcd9b0a40e52c06f295586a5aa4288d9aa75e9df7e8",
        )
        self.assertEqual(phase6b_proof["phase"], "phase6b")
        receipt = phase6b_proof["proof_receipt"]
        canonical = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            phase6b_proof["proof_receipt_canonical_sha256"],
        )
        self.assertEqual(
            receipt["candidate_head"],
            "7693d9db459f81f4d89e680108867ce31dd4c7ed",
        )
        self.assertEqual(
            receipt["candidate_tree"],
            "f62ad8e9cccffe715927fa825f24cb4312a27934",
        )
        self.assertEqual(
            receipt["manifest_sha256"],
            "3bfe6ce5f2514f642dee58416d12f0bfa938646897b70b3e3294f8e1639b2c66",
        )
        self.assertEqual(receipt["result"], "passed")
        self.assertEqual(receipt["provider_external_calls"], 0)
        self.assertFalse(receipt["production_data_read"])
        self.assertTrue(receipt["resources_removed"])

    def test_schema_validation_scope_is_versionless_and_exact(self) -> None:
        contract = json.loads(SCHEMA_CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            contract["validation_scope"],
            {
                "scope": "successor_disposable_only",
                "environment": "disposable_only",
                "production_data_read": False,
                "provider_external_calls": 0,
                "production_state_changed": False,
            },
        )
        self.assertEqual(
            contract["hard_requirements"]["production_activation_blockers"],
            self.load_manifest()["activation"]["blockers"],
        )
        bridge = contract["bridge"]
        self.assertEqual(
            bridge["source_erasure_selectors"],
            ["thread", "message_tail", "recent", "all_conversations"],
        )
        self.assertEqual(
            bridge["source_erasure_direct_delete_roots"],
            ["public.chat_log", "public.chat_attachments", "public.threads"],
        )
        self.assertEqual(
            bridge["source_erasure_allowed_auxiliary_effects"],
            [
                "public.active_thread_selection",
                "trusted_web.response_transcript_v1",
            ],
        )
        self.assertEqual(
            bridge["source_erasure_auxiliary_effect_authority"],
            "exact_named_validated_on_delete_cascade_composite_owner_thread_"
            "foreign_keys_only",
        )
        self.assertEqual(
            bridge["source_erasure_validated_auxiliary_foreign_keys"],
            {
                "public.active_thread_selection": [
                    {
                        "constraint_name": (
                            "active_thread_selection_owner_thread_fk"
                        ),
                        "child_columns": ["owner_user_id", "thread_id"],
                        "parent_table": "public.threads",
                        "parent_columns": ["owner_user_id", "id"],
                        "validated": True,
                        "delete_action": "cascade",
                    }
                ],
                "trusted_web.response_transcript_v1": [
                    {
                        "constraint_name": (
                            "response_transcript_v1_user_chat_log_id_fkey"
                        ),
                        "child_columns": [
                            "user_chat_log_id",
                            "owner_user_id",
                            "thread_id",
                        ],
                        "parent_table": "public.chat_log",
                        "parent_columns": [
                            "id",
                            "owner_user_id",
                            "thread_id",
                        ],
                        "validated": True,
                        "delete_action": "cascade",
                    },
                    {
                        "constraint_name": (
                            "response_transcript_v1_assistant_chat_log_id_fkey"
                        ),
                        "child_columns": [
                            "assistant_chat_log_id",
                            "owner_user_id",
                            "thread_id",
                        ],
                        "parent_table": "public.chat_log",
                        "parent_columns": [
                            "id",
                            "owner_user_id",
                            "thread_id",
                        ],
                        "validated": True,
                        "delete_action": "cascade",
                    },
                ],
            },
        )
        self.assertFalse(
            bridge[
                "source_erasure_weak_single_column_transcript_foreign_keys_allowed"
            ]
        )
        self.assertEqual(
            bridge["source_erasure_unknown_dependency_action"],
            "fail_closed_before_delete_on_unknown_foreign_key_"
            "delete_trigger_delete_rule_or_inheritance",
        )
        self.assertEqual(
            bridge["source_erasure_transient_target_tables"],
            [
                "memory_ingest_private.source_erasure_target",
                "memory_ingest_private.source_erasure_thread_target",
            ],
        )
        self.assertEqual(
            bridge["source_erasure_permanent_tombstone_tables"],
            [
                "memory_ingest_private.source_erasure_message_tombstone",
                "memory_ingest_private.source_erasure_thread_tombstone",
            ],
        )
        self.assertEqual(
            bridge["source_erasure_tombstone_identity_scope"],
            "global_message_and_thread_uuid",
        )
        self.assertEqual(
            bridge["source_erasure_targets_retained_until"],
            "conversation_deletion_final_receipt_acknowledged",
        )
        self.assertTrue(bridge["source_erasure_tombstones_immutable"])
        self.assertEqual(
            bridge["source_erasure_runtime_catalog_attestation"],
            "exact_mutated_relation_schema_foreign_key_trigger_rule_"
            "and_inheritance_inventory",
        )
        self.assertFalse(
            bridge["source_erasure_unclassified_side_effects_allowed"]
        )
        self.assertFalse(
            bridge["source_erasure_legacy_capture_trigger_required"]
        )
        self.assertEqual(
            bridge["source_erasure_legacy_capture_trigger_if_present"],
            "exact_disabled_identity_only",
        )
        self.assertFalse(bridge["source_erasure_legacy_project_rows_deleted"])
        self.assertFalse(bridge["source_erasure_accounts_deleted"])
        self.assertFalse(
            bridge["source_erasure_structured_lifeswitch_tables_allowed"]
        )
        self.assertEqual(
            bridge["source_erasure_runtime_status"],
            "phase6d_inactive_static_candidate_"
            "phase6e_disposable_proof_pending",
        )
        self.assertIn(
            "memory_ingest_private.assert_chat_deletion_catalog()",
            contract["internal_functions"],
        )
        self.assertIn(
            "memory_ingest_private.assert_chat_deletion_catalog()",
            bridge["internal_functions"],
        )
        self.assertIn(
            "memory_ingest_private.serialize_response_transcript_source_erasure()",
            contract["internal_functions"],
        )
        self.assertIn(
            "memory_ingest_private.serialize_response_transcript_source_erasure()",
            bridge["internal_functions"],
        )

    def test_route_surface_is_exact_and_owner_is_not_a_path_parameter(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(manifest["owner_routes"], EXPECTED_ROUTES)
        self.assertEqual(
            manifest["prohibited_routes"],
            ["/cards", "/vantage", "/memory/evidence", "/memory/retrieve"],
        )
        self.assertTrue(manifest["legacy_imports_allowed"] is False)
        self.assertNotIn("user_id", "\n".join(manifest["owner_routes"]))


class SourceInventoryTests(unittest.TestCase):
    def test_release_one_authority_has_only_owner_and_worker_roles(self) -> None:
        self.assertEqual(
            tuple((role.name, role.value) for role in ActorRole),
            (("OWNER", "owner"), ("WORKER", "worker")),
        )
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("ActorRole.ADMIN", source)
                self.assertNotIn("explicit_admin_review", source)

    def test_repository_surface_is_closed_and_has_no_generic_plan_escape_hatch(self) -> None:
        expected_methods = {
            "persist_ingest_decision",
            "read_ingest_receipt",
            "fail_ingest",
            "mark_provider_dispatched",
            "complete_provider_call",
            "fail_provider_call",
            "apply_proposal_review",
            "correct_claim",
            "retract_claim",
            "request_claim_deletion",
            "finalize_claim_deletion",
            "finish_projection",
            "persist_answer_binding",
            "read_claims",
            "read_operation",
        }
        observed = {
            name
            for name, value in vars(GovernedMemoryRepository).items()
            if inspect.isfunction(value) and not name.startswith("_")
        }
        self.assertEqual(observed, expected_methods)
        source = (PACKAGE / "repository.py").read_text(encoding="utf-8")
        for forbidden in ("apply_plan", "effect_names", "free_form_sql"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

        for name in expected_methods:
            signature = inspect.signature(getattr(GovernedMemoryRepository, name))
            parameters = signature.parameters
            self.assertEqual(next(iter(parameters)), "self")
            for parameter in tuple(parameters.values())[1:]:
                with self.subTest(method=name, parameter=parameter.name):
                    self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertNotIn(
                        parameter.name,
                        {
                            "now",
                            "occurred_at",
                            "reviewed_at",
                            "transaction_time",
                            "sql",
                            "effects",
                            "plan",
                        },
                    )

    def test_repository_reason_and_answer_binding_signatures_are_exact(self) -> None:
        review = inspect.signature(
            GovernedMemoryRepository.apply_proposal_review
        ).parameters
        self.assertIn("reason_codes", review)
        self.assertNotIn("reason_code", review)
        for method_name in (
            "correct_claim",
            "retract_claim",
            "request_claim_deletion",
        ):
            with self.subTest(method=method_name):
                parameters = inspect.signature(
                    getattr(GovernedMemoryRepository, method_name)
                ).parameters
                self.assertNotIn("reason_code", parameters)
                self.assertNotIn("reason_codes", parameters)

        answer = tuple(
            inspect.signature(
                GovernedMemoryRepository.persist_answer_binding
            ).parameters
        )
        self.assertEqual(
            answer,
            (
                "self",
                "operation_id",
                "owner_user_id",
                "response_id",
                "thread_id",
                "query_sha256",
                "policy_sha256",
                "allowed_predicates",
                "domains",
                "intents",
                "max_records",
                "policy_revision",
                "renderer_sha256",
                "prompt_sha256",
                "explicit_recall",
                "selected_claim_ids",
                "injected_claim_ids",
                "outcome",
                "expected_selection_manifest_sha256",
                "expected_injection_manifest_sha256",
                "memory_block",
                "outbound_request",
            ),
        )

    def test_retract_and_delete_routes_require_state_and_revision_compare_and_swap(self) -> None:
        by_operation = {
            specification.operation: specification
            for specification in OWNER_ROUTE_SPECIFICATIONS
        }
        for operation in ("retract_claim", "delete_claim"):
            specification = by_operation[operation]
            with self.subTest(operation=operation):
                self.assertIn(
                    "expected_state_sha256", specification.required_body_fields
                )
                self.assertIn(
                    "expected_revision_sha256", specification.required_body_fields
                )
                self.assertEqual(specification.optional_body_fields, ())
                self.assertNotIn(
                    "pending_correction_override",
                    specification.required_body_fields
                    + specification.optional_body_fields,
                )
                self.assertTrue(specification.server_time_owned)

    def test_every_module_all_symbol_exists_and_wildcard_import_succeeds(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            module_name = (
                "rag_engine.governed_memory"
                if path.name == "__init__.py"
                else f"rag_engine.governed_memory.{path.stem}"
            )
            module = importlib.import_module(module_name)
            exports = getattr(module, "__all__", None)
            with self.subTest(module=module_name):
                self.assertIsInstance(exports, (list, tuple))
                self.assertEqual(len(exports), len(set(exports)))
                missing = [name for name in exports if not hasattr(module, name)]
                self.assertEqual(missing, [])
                namespace: dict[str, object] = {}
                exec(f"from {module_name} import *", namespace, namespace)
                self.assertEqual(
                    {name for name in exports if name in namespace}, set(exports)
                )

    def test_contract_hash_normalizes_nfc_recursively(self) -> None:
        composed = {
            "outer": ["caf\u00e9", {"label": "r\u00e9sum\u00e9"}],
        }
        decomposed = {
            "outer": [
                unicodedata.normalize("NFD", "caf\u00e9"),
                {"label": unicodedata.normalize("NFD", "r\u00e9sum\u00e9")},
            ],
        }
        self.assertNotEqual(composed, decomposed)
        composed_hash = canonical_sha256("governed_memory.nfc_probe", composed)
        self.assertRegex(composed_hash, r"^[0-9a-f]{64}$")
        with self.assertRaises(ContractViolation):
            canonical_sha256("governed_memory.nfc_probe", decomposed)

    def test_versionless_package_file_set_is_exact(self) -> None:
        observed = {path.name for path in PACKAGE.glob("*.py")}
        self.assertEqual(observed, EXPECTED_PACKAGE_FILES)

    def test_runtime_adapter_file_set_and_exports_are_exact(self) -> None:
        observed = {path.name for path in RUNTIME_PACKAGE.glob("*.py")}
        self.assertEqual(observed, EXPECTED_RUNTIME_PACKAGE_FILES)
        for path in sorted(RUNTIME_PACKAGE.glob("*.py")):
            module_name = f"rag_engine.governed_memory.runtime.{path.stem}"
            module = importlib.import_module(module_name)
            exports = getattr(module, "__all__", None)
            with self.subTest(module=module_name):
                self.assertIsInstance(exports, (list, tuple))
                self.assertEqual(len(exports), len(set(exports)))
                self.assertEqual(
                    [name for name in exports if not hasattr(module, name)],
                    [],
                )

    def test_provider_asset_file_set_and_non_python_allowlist_are_exact(self) -> None:
        observed = {
            path.name for path in PROVIDER_ASSETS.iterdir() if path.is_file()
        }
        self.assertEqual(observed, EXPECTED_PROVIDER_ASSET_FILES)
        observed_non_python = {
            relative
            for relative, _digest in _package_source_material(PACKAGE)
            if Path(relative).suffix != ".py"
        }
        self.assertEqual(observed_non_python, PROVIDER_ASSET_SOURCE_PATHS)

    def test_test_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in Path(__file__).parent.glob("test_*.py")}
        self.assertEqual(observed, EXPECTED_TEST_FILES)

    def test_integration_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in INTEGRATION_TESTS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_INTEGRATION_FILES)

    def test_validation_tool_file_set_is_exact(self) -> None:
        observed = {path.name for path in VALIDATION_TOOLS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_VALIDATION_TOOL_FILES)

    def test_disposable_runner_binds_exact_migration_manifest(self) -> None:
        migration_manifest = ROOT / "governed-memory-migrations" / "manifest.json"
        manifest_sha256 = hashlib.sha256(migration_manifest.read_bytes()).hexdigest()
        runner = (VALIDATION_TOOLS / "run_disposable_successor.sh").read_text(
            encoding="utf-8"
        )
        binding = f"readonly EXPECTED_MANIFEST_SHA256='{manifest_sha256}'"
        self.assertEqual(runner.count("readonly EXPECTED_MANIFEST_SHA256="), 1)
        self.assertIn(binding, runner)

    def test_disposable_runner_receipt_binds_source_and_runtime_build(self) -> None:
        runner = (VALIDATION_TOOLS / "run_disposable_successor.sh").read_text(
            encoding="utf-8"
        )
        self.assertEqual(runner.count("RUNTIME_BUILD_RECEIPT_SHA256=''"), 1)
        self.assertEqual(
            runner.count('RUNTIME_BUILD_RECEIPT_SHA256="${receipt_sha}"'),
            1,
        )
        self.assertIn('"source_tree_sha256":"%s"', runner)
        self.assertIn('"runtime_build_receipt_sha256":"%s"', runner)
        self.assertIn('"deletion_receipt_sha256":"%s"', runner)
        self.assertIn(
            '"deletion_resilience_receipt_sha256":"%s"', runner
        )
        self.assertIn('"${SOURCE_TREE_SHA256}" "${RUNTIME_BUILD_RECEIPT_SHA256}"', runner)
        self.assertIn(
            '"${INTEGRATION_RECEIPT_SHA256}" "${DELETION_RECEIPT_SHA256}"',
            runner,
        )
        self.assertIn(
            '"${DELETION_RESILIENCE_RECEIPT_SHA256}"', runner
        )
        self.assertEqual(
            runner.count("validate_deletion_resilience_receipt() {"), 1
        )
        self.assertEqual(
            runner.count("test_deletion_resilience_boundaries"), 1
        )
        self.assertIn(
            "readonly PHASE6E_DELETION_INTEGRATION_READY='true'", runner
        )
        self.assertIn(
            "phase6e_migration_proof_authorization_missing", runner
        )
        self.assertEqual(
            runner.count("governed-memory-successor-disposable-run-v6"), 1
        )
        self.assertNotIn("governed-memory-successor-disposable-run-v5", runner)
        self.assertNotIn("governed-memory-successor-disposable-run-v4", runner)

    def test_disposable_runner_uses_successor_runtime_and_exact_provider_asset_allowlist(self) -> None:
        runner = (VALIDATION_TOOLS / "run_disposable_successor.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("governed-memory-phase4-", runner)
        self.assertNotIn("governed-memory-phase5-runtime-", runner)
        self.assertNotIn("governed-memory-phase5-build-", runner)
        self.assertIn("governed-memory-successor-runtime-", runner)
        self.assertIn("governed-memory-successor-build-", runner)
        for relative in sorted(PROVIDER_ASSET_SOURCE_PATHS):
            with self.subTest(relative=relative):
                self.assertEqual(runner.count(f'"{relative}"'), 2)

    def test_release_tool_file_set_is_exact(self) -> None:
        observed = {path.name for path in RELEASE_TOOLS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_RELEASE_TOOL_FILES)

    def test_clean_successor_document_file_set_is_exact(self) -> None:
        observed = {
            path.name for path in CLEAN_SUCCESSOR_DOCS.iterdir() if path.is_file()
        }
        self.assertEqual(observed, EXPECTED_CLEAN_SUCCESSOR_DOC_FILES)

    def test_successor_release_docs_and_contracts_have_no_stale_active_claims(
        self,
    ) -> None:
        paths = [
            *sorted(CLEAN_SUCCESSOR_DOCS.iterdir()),
            MANIFEST,
            ROOT / "ops" / "governed_memory" / "bootstrap_contract.json",
            ROOT / "ops" / "governed_memory" / "pilot_contract.json",
            ROOT / "ops" / "governed_memory" / "compose.candidate.yaml",
            RELEASE_TOOLS / "release_guard.py",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("Phase 4", text)
                self.assertNotIn("phase4", text)
                self.assertNotIn("current_phase5", text)
                self.assertNotIn("v1.11.0", text)
                self.assertNotIn("owner_claim_fact_detail_api_not_implemented", text)
                self.assertNotIn(
                    "durable_pilot_ever_started_marker_not_implemented",
                    text,
                )

    def test_successor_has_no_legacy_names_or_imports(self) -> None:
        prohibited_text = (
            "memory_" + "v1",
            "memory_" + "raw",
            "vantage",
            "persona_loader",
            "filesystem review",
            "assistant_response_preferences",
        )
        prohibited_import_roots = {"openai", "asyncpg", "qdrant_client"}
        allowed_outer_import_roots = {
            "http_service.py": {"asyncpg"},
        }
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            lowered = source.lower().replace(
                "chat_source_and_derived_governed_conversational_memory_v1",
                "current_chat_source_erasure_contract",
            )
            for token in prohibited_text:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, lowered)
            tree = ast.parse(source, filename=str(path))
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".", 1)[0])
            self.assertEqual(
                roots & prohibited_import_roots,
                allowed_outer_import_roots.get(path.name, set()),
                f"{path.name} has an unclassified outer-adapter import",
            )
        runtime_source = (RUNTIME_PACKAGE / "live_supabase.py").read_text(encoding="utf-8")
        self.assertIn('"User-Agent": "governed-memory-live-user"', runtime_source)
        self.assertNotIn("governed-memory-live-user-v", runtime_source)

    def test_successor_package_imports_no_outer_rag_engine_modules(self) -> None:
        allowed_prefix = "rag_engine.governed_memory"
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[tuple[int, str]] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(
                        (node.lineno, alias.name) for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append((node.lineno, node.module))
            for line, module_name in imported:
                if module_name != "rag_engine" and not module_name.startswith(
                    "rag_engine."
                ):
                    continue
                with self.subTest(
                    path=path.relative_to(ROOT).as_posix(),
                    line=line,
                    module=module_name,
                ):
                    self.assertTrue(
                        module_name == allowed_prefix
                        or module_name.startswith(f"{allowed_prefix}."),
                        "the installable successor imports an outer rag_engine module",
                    )

    def test_response_tests_import_only_successor_and_shared_fixtures(self) -> None:
        allowed_rag_engine_prefix = "rag_engine.governed_memory"
        allowed_test_module = "tests.memory._fixtures"
        paths = (
            Path(__file__).parent / "test_response_provider.py",
            Path(__file__).parent / "test_response_runtime.py",
        )
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[tuple[int, str]] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(
                        (node.lineno, alias.name) for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append((node.lineno, node.module))
            for line, module_name in imported:
                if module_name == "rag_engine" or module_name.startswith(
                    "rag_engine."
                ):
                    allowed = (
                        module_name == allowed_rag_engine_prefix
                        or module_name.startswith(f"{allowed_rag_engine_prefix}.")
                    )
                elif module_name == "tests" or module_name.startswith("tests."):
                    allowed = module_name == allowed_test_module
                else:
                    continue
                with self.subTest(path=path.name, line=line, module=module_name):
                    self.assertTrue(
                        allowed,
                        "response tests import an outer application or another test module",
                    )

    def test_successor_tests_do_not_import_old_helpers(self) -> None:
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            self.assertFalse(
                any(("memory_" + "v1") in name for name in imported),
                f"{path.name} imports a retired Memory test or module",
            )

    def test_object_kind_has_no_compatibility_aliases(self) -> None:
        compatibility_name = "Value" + "Kind"
        paths = [
            *sorted(PACKAGE.glob("*.py")),
            *sorted(Path(__file__).parent.glob("test_*.py")),
        ]
        definitions: list[tuple[str, int]] = []
        aliases: list[tuple[str, int, str]] = []
        for path in paths:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn(
                    compatibility_name,
                    source,
                    "the clean successor must not retain retired enum names",
                )
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "ObjectKind":
                    definitions.append((path.name, node.lineno))
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if not isinstance(value, ast.Name) or value.id != "ObjectKind":
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id != "ObjectKind":
                            aliases.append((path.name, node.lineno, target.id))
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for imported in node.names:
                        if imported.name.rsplit(".", 1)[-1] != "ObjectKind":
                            continue
                        if imported.asname not in (None, "ObjectKind"):
                            aliases.append((path.name, node.lineno, imported.asname))

        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0][0], "contracts.py")
        self.assertEqual(aliases, [])

    def test_readme_records_phase6e_proof_pending_without_activation(self) -> None:
        readme = SUCCESSOR_README.read_text(encoding="utf-8")
        normalized = " ".join(readme.split())
        for required in (
            "Phase 6E",
            "historical evidence",
            "not production activated",
            "structured LifeSwitch",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

    def test_pyproject_packages_only_the_successor_and_has_exact_entrypoint(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"], {
            "requires": ["setuptools==84.0.0"],
            "build-backend": "setuptools.build_meta",
        })
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
        self.assertEqual(
            project["tool"]["setuptools"]["package-data"],
            {
                "rag_engine.governed_memory.provider_assets": [
                    "*.json",
                    "*.txt",
                ]
            },
        )

    def test_phase6e_runtime_is_current_and_phase6b_receipt_is_historical(
        self,
    ) -> None:
        runtime = json.loads(MANIFEST.read_text(encoding="utf-8"))[
            "validation_runtime"
        ]
        build_receipt = json.loads(
            RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii")
        )
        phase6b_receipt = json.loads(
            HISTORICAL_PHASE6B_RUNTIME_BUILD_RECEIPT.read_text(
                encoding="ascii"
            )
        )
        self.assertFalse(runtime["final_phase6d_runtime_rebuild_pending"])
        self.assertTrue(runtime["current_source_bound"])
        self.assertEqual(
            runtime["current_source_tree_sha256"], EXPECTED_SOURCE_TREE_SHA256
        )
        self.assertEqual(
            runtime["current_candidate_python"], EXPECTED_CANDIDATE_PYTHON
        )
        self.assertEqual(
            build_receipt["source_tree_sha256"], EXPECTED_SOURCE_TREE_SHA256
        )
        self.assertEqual(
            build_receipt["candidate_python"], EXPECTED_CANDIDATE_PYTHON
        )
        self.assertEqual(
            phase6b_receipt["source_tree_sha256"], PHASE6B_SOURCE_TREE_SHA256
        )
        self.assertEqual(
            phase6b_receipt["candidate_python"], PHASE6B_CANDIDATE_PYTHON
        )
        self.assertEqual(
            hashlib.sha256(
                HISTORICAL_PHASE6B_RUNTIME_BUILD_RECEIPT.read_bytes()
            ).hexdigest(),
            PHASE6B_RUNTIME_BUILD_RECEIPT_SHA256,
        )
        self.assertNotEqual(
            EXPECTED_SOURCE_TREE_SHA256, PHASE6B_SOURCE_TREE_SHA256
        )


if __name__ == "__main__":
    unittest.main()
