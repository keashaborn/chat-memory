#!/usr/bin/env python3
from __future__ import annotations

"""Offline verifier for the Phase 7C disposable-revalidated inactive package.

The guard reads repository artifacts only. It cannot install, authorize, start,
route, migrate, delete, or call a provider. Observation evaluation is retained
only as a content-free refusal surface for future separately authorized work.
"""

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.governed_memory_install.inactive_installation import (
    InstallationPackageError,
    verify_package as verify_installation_package,
)
from tools.governed_memory_validation.verify_migration_manifest import (
    verify as verify_migration_manifest,
)


OPS = ROOT / "ops" / "governed_memory"
MIGRATION_ROOT = ROOT / "governed-memory-migrations"
MIGRATION_MANIFEST = MIGRATION_ROOT / "manifest.json"
PACKAGE_MANIFEST = OPS / "installation" / "package_manifest.json"
RUNTIME_MANIFEST = OPS / "runtime_manifest.json"
RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
BOOTSTRAP = OPS / "bootstrap_contract.json"
PILOT = OPS / "pilot_contract.json"
RECEIPT_SCHEMA = OPS / "release_receipt.schema.json"
RUNTIME_LOCK = OPS / "runtime-requirements.lock"
BUILD_LOCK = OPS / "build-requirements.lock"
RUNTIME_PACKAGES = (
    ROOT / "tools" / "governed_memory_validation" / "runtime_packages.json"
)
INSTALLER = ROOT / "tools" / "governed_memory_install" / "inactive_installation.py"
DISPOSABLE_RUNNER = (
    ROOT / "tools" / "governed_memory_validation" / "run_disposable_successor.sh"
)
RELEASE_GUARD = Path(__file__).resolve()
FINALIZER = (
    OPS
    / "installation"
    / "postgres"
    / "canonical_bootstrap_finalize.pgsql"
)
RETIRED_CURRENT_PHASE6E_PROOF = OPS / "phase6e_disposable_proof_receipt.json"
PHASE7C_DISPOSABLE_PROOF = OPS / "phase7c_disposable_proof_receipt.json"
HISTORICAL_PHASE6B_RUNTIME = (
    OPS / "history" / "phase6b" / "runtime_build_receipt.json"
)
HISTORICAL_PHASE6B_PROOF = (
    OPS / "history" / "phase6b" / "disposable_proof_receipt.json"
)
HISTORICAL_PHASE6E_RUNTIME = (
    OPS / "history" / "phase6e" / "runtime_build_receipt.json"
)
HISTORICAL_PHASE6E_PROOF = (
    OPS / "history" / "phase6e" / "disposable_proof_receipt.json"
)
README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"
VALIDATION = ROOT / "docs" / "memory" / "clean_successor" / "VALIDATION.md"
ACTIVATION = ROOT / "docs" / "memory" / "clean_successor" / "ACTIVATION.md"

HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)

EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256 = (
    "d4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0"
)
EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256 = (
    "43f12d40f2bc44cc4bdf4413aa1fec65cbcb06d0dd491f6f173914a055e9d28a"
)
EXPECTED_PHASE7C_HTTP_RECEIPT_SHA256 = (
    "297c6c9ae6c6ce164480772e8b87ea765cff89e7386f860c29950b72223e0079"
)
EXPECTED_PHASE7C_DELETION_RECEIPT_SHA256 = (
    "c387e947ce8758e3d396fe95385a4e3e1651cb10b3a5220c329b2250d20cc637"
)
EXPECTED_PHASE7C_RESILIENCE_RECEIPT_SHA256 = (
    "3614697a7b6394a595ddd922481aed763e21a0f08f70e96aad2df14e40557351"
)
EXPECTED_PHASE7C_PROOF_LOG_SHA256 = (
    "75fb33b38574f62a781151487cfce37cfa3c1e343ec2cca27403c583ce953136"
)
EXPECTED_PHASE7C_ATTESTED_RUNTIME_MANIFEST_SHA256 = (
    "b1918d08a0338a05310db2c95b92f3e3393a5be936e17d7a235ddc0c0bc30562"
)
EXPECTED_PHASE7C_ATTESTED_MIGRATION_MANIFEST_SHA256 = (
    "3dc839db27f1b0d4ac68260fd7bff9f77321e6a10159a299c8335eb36086bcc4"
)

EXPECTED_PHASE7C_PROOF_WRAPPER_KEYS = {
    'attested_pre_promotion_runtime_manifest_sha256',
    'deletion_receipt',
    'deletion_resilience_receipt',
    'http_vertical_slice_receipt',
    'phase',
    'proof_log_sha256',
    'proof_receipt',
    'proof_receipt_canonical_sha256',
    'proof_receipt_canonicalization',
    'schema_version',
}
EXPECTED_PHASE7C_PROOF_KEYS = {
    'branch',
    'bridge_logical_dump_sha256',
    'candidate_head',
    'candidate_tree',
    'candidate_unchanged',
    'connect_trace_sha256',
    'deletion_receipt_sha256',
    'deletion_resilience_receipt_sha256',
    'docker_persistent_mounts',
    'external_network_calls',
    'foundation_logical_dump_sha256',
    'integration_receipt_sha256',
    'invocation_id',
    'loopback_application_endpoints',
    'manifest_sha256',
    'network_id',
    'ports_released',
    'postgres_container_id',
    'postgres_image_id',
    'postgres_server_version',
    'production_data_read',
    'production_endpoint_calls',
    'production_service_invoked',
    'provider_external_calls',
    'published_container_ports',
    'qdrant_container_id',
    'qdrant_image_digest',
    'qdrant_image_id',
    'qdrant_server_version',
    'resources_removed',
    'result',
    'rollback_reapply',
    'run_id',
    'runtime_build_receipt_sha256',
    'runtime_lock_sha256',
    'runtime_packages_sha256',
    'schema_version',
    'semantic_threshold_calibrated',
    'source_tree_sha256',
    'traced_internal_bridge_connects',
}
EXPECTED_PHASE7C_HTTP_RECEIPT_KEYS = {
    'answer_binding_sha256',
    'auth_negative_matrix_sha256',
    'chat_a_thread_id',
    'chat_b_thread_id',
    'claim_id',
    'cold_extraction_lease',
    'cold_projection_rebuild',
    'corrected_revision_id',
    'deletion_receipt_sha256',
    'embedding_dispatch_adversarial',
    'embedding_dispatch_marker_count',
    'embedding_dispatch_marker_sha256',
    'http_lifecycle_sha256',
    'http_owner_lifecycle',
    'inactive_worker_runtime_sha256',
    'initial_revision_id',
    'jwks_fetch_count',
    'jwks_manifest_sha256',
    'owner_isolation_sha256',
    'production_data_read',
    'production_endpoint_calls',
    'production_service_invoked',
    'projection_lease_version_fenced',
    'provider_external_calls',
    'rebuild_manifest_sha256',
    'review_surface_sha256',
    'route_manifest_sha256',
    'schema',
    'semantic_threshold_calibrated',
    'synthetic_jwt_only',
}
EXPECTED_PHASE7C_DELETION_RECEIPT_KEYS = {
    'attachment_thread_bridge_absence',
    'claim_deletion_receipt_sha256',
    'clean_composite_auxiliary_fk_count',
    'coordinator_final_receipt_sha256',
    'coordinator_no_work_after_completion',
    'crash_injection_tested',
    'deleted_attachment_count',
    'deleted_bridge_row_count',
    'deleted_claim_count',
    'deleted_message_count',
    'deleted_thread_count',
    'exact_completed_request_replay',
    'governed_final_receipt_sha256',
    'governed_message_tombstone_count',
    'lifeswitch_snapshot_bytes',
    'lifeswitch_snapshot_sha256',
    'live_runtime_catalog_refusal_count',
    'other_owner_qdrant_snapshot_bytes',
    'other_owner_qdrant_snapshot_sha256',
    'other_owner_snapshot_bytes',
    'other_owner_snapshot_sha256',
    'page_boundary_tested',
    'production_data_read',
    'production_endpoint_calls',
    'production_service_invoked',
    'provider_external_calls',
    'qdrant_delete_outcome_unknown_resolved_by_readback',
    'qdrant_deletion_receipt_sha256',
    'qdrant_target_absent_alias_and_physical',
    'schema',
    'source_conversation_final_receipt_sha256',
    'source_message_tombstone_count',
    'source_thread_tombstone_count',
    'stale_lease_injection_tested',
    'synthetic_provider_only',
    'target_message_count',
    'target_thread_count',
    'transient_targets_purged',
    'typed_project_conflict_code',
    'typed_project_conflict_retryable',
    'typed_project_conflict_status',
}
EXPECTED_PHASE7C_RESILIENCE_RECEIPT_KEYS = {
    'attachment_identity_reuse_refused',
    'attachment_movement_refused',
    'conversation_final_receipt_sha256',
    'crash_after_conversation_completion_ack',
    'crash_after_conversation_finalize',
    'crash_after_governed_receipt_handoff',
    'crash_after_source_claim_step',
    'crash_after_source_memory_finalize',
    'crash_after_source_register',
    'crash_after_successor_completion_ack',
    'crash_after_target_append',
    'crash_after_target_seal',
    'crash_boundary_count',
    'crash_boundary_manifest_sha256',
    'deleted_attachment_count',
    'deleted_message_count',
    'deleted_thread_count',
    'exhausted_attempts_manual_review',
    'final_absence_manifest_sha256',
    'final_absence_verified',
    'future_dated_chat_refused',
    'pending_ack_attempt_cap_recovered',
    'post_completion_no_work',
    'production_data_read',
    'production_endpoint_calls',
    'provider_external_calls',
    'schema',
    'source_page_count',
    'source_page_sizes_sha256',
    'source_target_manifest_sha256',
    'stale_lease_read_refused',
    'stale_lease_release_refused',
    'stale_lease_replaced',
    'successor_final_receipt_sha256',
    'successor_page_count',
    'successor_page_replay_conflict_refused',
    'successor_page_replay_exact',
    'successor_page_sizes_sha256',
    'target_count',
}

EXPECTED_DISPOSABLE_VALIDATION = {'scope': 'successor_disposable_only',
 'evidence_status': 'phase7c_disposable_installation_revalidation_passed_not_production_activation',
 'current_full_proof_complete': True,
 'disposable_revalidation_required': False,
 'validation_document': 'docs/memory/clean_successor/VALIDATION.md',
 'runner_path': 'tools/governed_memory_validation/run_disposable_successor.sh',
 'runner_sha256': '2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b',
 'runner_sealed': True,
 'proof_execution_runner_sha256': 'bd591188d0afaf4d7a65753e54648241fc1aa5ff3289f194d76d6aa07dfe449f',
 'current_runner_execution_attested_by_phase7c_proof': False,
 'current_runner_post_proof_change_scope': 'metadata_only_expected_manifest_sha256_and_validation_state_rebind',
 'current_runner_proof_execution_semantics_changed': False,
 'runner_receipt': 'stdout:SUCCESSOR_DISPOSABLE_RECEIPT',
 'current_candidate_python': '/tmp/governed-memory-successor-runtime-94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365-610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd/bin/python',
 'postgresql_fresh_empty': True,
 'qdrant_fresh_empty': True,
 'migration_forward_rollback_reapply': True,
 'normalized_catalog_equivalent_after_reapply': True,
 'forced_rls_owner_isolation_and_direct_dml_denial': True,
 'asymmetric_jwt_and_jwks_boundary_invoked': True,
 'owner_http_lifecycle_invoked': True,
 'all_owner_routes_invoked': True,
 'alternating_owner_pool_isolation': True,
 'owner_pool_max_size': 1,
 'distinct_chat_a_chat_b': True,
 'cold_extraction_reconstruction': True,
 'cold_postgresql_projection_rebuild': True,
 'correction_retraction_hard_delete_and_retention': True,
 'live_supabase_user_adapter_unit_validated': True,
 'live_supabase_session_freshness_verified': False,
 'qdrant_v1_19_0_real_disposable_compatibility_verified': True,
 'pilot_marker_disposable_proof_complete': True,
 'worker_runtime_composition_validated': True,
 'worker_runtime_composition_status': 'implemented_inactive_persistently_fair_three_lane_real_two_database_disposable_validated',
 'worker_cross_process_singleton_validated': True,
 'worker_cross_process_singleton_status': 'implemented_inactive_real_concurrent_lock_disposable_validated',
 'deletion_coordination_disposable_proof_complete': True,
 'deletion_coordination_status': 'phase7c_exact_chat_targets_real_disposable_validated_inactive_not_routed_not_production_applied',
 'production_routes_installed': False,
 'authenticated_frontend_verified': False,
 'semantic_threshold_calibrated': False,
 'production_data_read': False,
 'production_endpoint_calls': 0,
 'provider_external_calls': 0,
 'persistent_resources_created': False,
 'final_resources_absent': True,
 'resource_cleanup_complete': True,
 'current_proof_receipt': 'ops/governed_memory/phase7c_disposable_proof_receipt.json',
 'current_proof_receipt_sha256': 'd4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0'}
EXPECTED_BOOTSTRAP_IMPLEMENTATION_STATUS = {'runtime': 'phase7c_source_bound_offline_build_sealed_disposable_validated_inactive',
 'session_id_required': True,
 'supabase_auth_sessions_rpc': 'staged_candidate_not_installed_or_live_verified',
 'owner_claim_fact_detail': 'implemented_candidate_disposable_validated_not_production_applied',
 'provider_adapter': 'strict_fake_tested_zero_real_calls',
 'embedding_adapter': 'strict_3072_fake_unit_validated_durable_request_dispatch_marker_disposable_validated_zero_real_calls',
 'qdrant_adapter': 'exact_fake_unit_and_real_disposable_v1_19_0_validated_not_persistent_approved',
 'pilot_marker': 'implemented_disposable_validated_not_production_applied',
 'worker_algorithm': 'implemented_fake_tested',
 'worker_runtime_composition': 'implemented_inactive_persistently_fair_three_lane_real_two_database_disposable_validated',
 'deletion_coordinator': 'phase7c_exact_chat_targets_real_disposable_validated_inactive_not_routed_not_production_applied',
 'worker_context_required_policy': 'fresh_count_zero_double_mark_re_leased_count_one_single_mark_terminal_unresolved_one_receipt_only_successor_read_zero_successor_writes_provider_embedding_vector_calls',
 'worker_scheduler': 'private_content_free_postgresql_sequence_cyclic_three_lane_disposable_validated_inactive',
 'worker_cross_process_singleton': 'postgresql_session_advisory_lock_real_concurrent_disposable_validated_inactive',
 'calibration': 'independently_bound_unapproved_retrieval_off',
 'frontend_candidate': '71377a_built_undeployed_visual_qa_pending_operation_id_confirmation_semantics_unverified'}
EXPECTED_PILOT_PROVIDER_POLICY = {'provider_calls_before_pilot_authorization': 0,
 'provider_adapter_status': 'strict_fake_tested_zero_real_calls',
 'embedding_adapter_status': 'strict_3072_fake_unit_validated_durable_request_dispatch_marker_disposable_validated_zero_real_calls',
 'qdrant_adapter_status': 'exact_fake_unit_and_real_disposable_v1_19_0_validated_not_persistent_approved',
 'worker_algorithm_status': 'implemented_fake_tested',
 'worker_runtime_composition_status': 'implemented_inactive_persistently_fair_three_lane_real_two_database_disposable_validated',
 'deletion_coordinator_status': 'phase7c_exact_chat_targets_real_disposable_validated_inactive_not_routed_not_production_applied',
 'context_required_runtime_policy': 'fresh_count_zero_double_mark_re_leased_count_one_single_mark_terminal_unresolved_one_receipt_only_successor_read_zero_successor_writes_provider_embedding_vector_calls',
 'qdrant_unavailable_policy': 'predispatch_qdrant_unavailability_retryable_post_embedding_dispatch_failure_terminal_without_embedding_resend_canonical_postgresql_claim_retained_later_same_claim_projection_blocked_explicit_projection_reconciliation_required',
 'calibration_status': 'independently_bound_unapproved_retrieval_off',
 'automatic_retry_after_unknown_dispatch': False}
EXPECTED_PILOT_CANDIDATE_SURFACES = {'runtime': 'phase7c_source_bound_offline_build_sealed_disposable_validated_inactive',
 'owner_claim_fact_detail': 'implemented_candidate_disposable_validated_not_production_applied',
 'pilot_marker': 'implemented_disposable_validated_not_production_applied',
 'deletion_coordinator': 'phase7c_exact_chat_targets_real_disposable_validated_inactive_not_routed_not_production_applied',
 'frontend': '71377a_built_undeployed_visual_qa_pending_operation_id_confirmation_semantics_unverified'}
EXPECTED_MIGRATION_MANIFEST_SHA256 = (
    "57ea2a0b151b0ac4a84f0df86041e418d1b1a7843cbfd9281175e34500e15150"
)
EXPECTED_MIGRATION_PACKAGE_ID_SHA256 = (
    "949cfa26bdcdaae11fbc582664e38295c585229be88c06d42e356745b1e97d8d"
)
EXPECTED_PACKAGE_MANIFEST_SHA256 = (
    "d851ee1749e90b403139b8b6376c8d71f219bd4d52b868062efac3936d71c3af"
)
EXPECTED_RUNTIME_MANIFEST_SHA256 = (
    "fa71a22afa3ead0879324c4e3179165f3ab02fbf9de4445f564fa213eeee7327"
)
EXPECTED_RUNTIME_RECEIPT_SHA256 = (
    "210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2"
)
EXPECTED_RUNTIME_SOURCE_SHA256 = (
    "610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd"
)
EXPECTED_RUNTIME_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
EXPECTED_RUNTIME_WHEEL_SHA256 = (
    "2cd060454039e8e16149cd670bf0f8dba70d6d93cc749c542fa5cc86b938c05e"
)
EXPECTED_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_RUNTIME_PACKAGES_SHA256 = (
    "ed9273d6bd6dad6cf5680c478dff1beab453f66ab607914994fe8dc2b9d4e882"
)
EXPECTED_RUNTIME_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{EXPECTED_RUNTIME_SOURCE_SHA256}/bin/python"
)
EXPECTED_RUNTIME_WHEEL = (
    "/tmp/governed-memory-successor-build-"
    f"{EXPECTED_BUILD_LOCK_SHA256}-{EXPECTED_RUNTIME_SOURCE_SHA256}/dist/"
    "governed_memory_successor-0.0.0-py3-none-any.whl"
)

EXPECTED_FIXED_ARTIFACT_HASHES = {
    "docs/memory/clean_successor/ACTIVATION.md": (
        "92f1106dd9e37f4e5ecb5ff4bc856578fd14057caafdd1a480a165857fdd495a"
    ),
    "docs/memory/clean_successor/README.md": (
        "03e7e02409dc04e2ec21931ce9e45bfba13f7a51d593e65c7ba359f96b8704e3"
    ),
    "docs/memory/clean_successor/VALIDATION.md": (
        "17abb30f9c79bd02fd9814941c3256953816cbeb5bdfa39e3c939145112c564a"
    ),
    "governed-memory-migrations/manifest.json": (
        EXPECTED_MIGRATION_MANIFEST_SHA256
    ),
    "ops/governed_memory/phase7c_disposable_proof_receipt.json": (
        EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
    ),
    "ops/governed_memory/bootstrap_contract.json": (
        "3cdeaf1b90b253d2f07244de432104181c7368c0728bf4504f18a353c106682c"
    ),
    "ops/governed_memory/history/phase6b/disposable_proof_receipt.json": (
        "55e3993e5f403b095a804bcd9b0a40e52c06f295586a5aa4288d9aa75e9df7e8"
    ),
    "ops/governed_memory/history/phase6b/runtime_build_receipt.json": (
        "ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69"
    ),
    "ops/governed_memory/history/phase6e/disposable_proof_receipt.json": (
        "79ed0cf79299f856b8a0ab77d420386adf3705f286f6c78b21225cba8eed1097"
    ),
    "ops/governed_memory/history/phase6e/runtime_build_receipt.json": (
        "cfe7a60c2e69de5a1603f86717f72d093f6fc2e623c2cb627008dbabb97c1c86"
    ),
    "ops/governed_memory/installation/package_manifest.json": (
        EXPECTED_PACKAGE_MANIFEST_SHA256
    ),
    "ops/governed_memory/pilot_contract.json": (
        "3c5105024637f7c9c9f46e1131918d8e253040eee14bc2a35f2618fd198d889f"
    ),
    "ops/governed_memory/release_receipt.schema.json": (
        "900af74bc4bf40028b4db0a43d9d315230003439bcfb00798dde4f903e3974f0"
    ),
    "ops/governed_memory/runtime_build_receipt.json": (
        EXPECTED_RUNTIME_RECEIPT_SHA256
    ),
    "ops/governed_memory/runtime_manifest.json": (
        EXPECTED_RUNTIME_MANIFEST_SHA256
    ),
    "tools/governed_memory_validation/run_disposable_successor.sh": (
        "2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b"
    ),
    "tools/governed_memory_validation/postgres_bootstrap.pgsql": (
        "0c28d2e444cddea0b61e8ea7ac9f6084b06e2038beb06bb65e754c4712eeb857"
    ),
}

EXPECTED_ACTIVATION_BLOCKERS = [
    "production_activation_not_authorized",
    "inactive_installation_package_not_authorized",
    "semantic_calibration_artifact_unapproved_retrieval_off",
    "live_supabase_runtime_credentials_not_mounted_or_verified",
    "previously_exposed_successor_credentials_not_rotated",
    "database_role_credentials_not_provisioned",
    "conversation_bridge_catalog_hash_not_provisioned",
    "source_logging_policy_not_live_verified",
    "source_logging_parameter_remediation_not_authorized_or_applied",
    "pg_hba_and_transport_not_verified_for_runtime_logins",
    "supabase_auth_sessions_rpc_not_installed_or_live_verified",
    "fresh_isolated_persistent_postgresql_not_created",
    "fresh_isolated_persistent_qdrant_not_created_or_approved",
    "persistent_store_restart_supervision_and_boot_recovery_not_implemented_or_verified",
    "canonical_postgresql_encrypted_backup_and_restore_not_proven",
    "private_frontend_source_firewall_not_proved",
    "tls_termination_or_private_transport_not_decided",
    "production_store_runtime_credentials_and_role_activation_not_authorized_or_executed",
    "successor_http_service_not_installed",
    "successor_worker_service_not_installed",
    "successor_conversation_capture_not_activated",
    "successor_chat_deletion_route_candidate_not_installed_or_live_verified",
    "frontend_successor_deletion_request_idempotency_and_confirmation_binding_not_implemented_or_verified",
    "source_erasure_requester_membership_not_granted_or_verified",
    "provider_adapter_real_call_validation_not_authorized_or_completed",
    "embedding_adapter_real_call_validation_not_authorized_or_completed",
    "projection_reconciliation_and_sequence_safe_qdrant_repair_not_implemented",
    "legacy_project_memory_thread_dependencies_not_separated",
    "trusted_web_transcript_composite_owner_thread_lineage_not_installed",
    "legacy_chat_owner_thread_lineage_not_remediated",
    "frontend_candidate_71377a_undeployed_visual_qa_pending",
    "pilot_owner_and_scope_not_authorized",
    "legacy_memory_owner_scoped_read_write_shadow_quiescence_not_proved",
]

EXACT_TARGETS = {
    "postgres_container": "governed-memory-postgres-9a54cf123493-000001",
    "qdrant_container": "governed-memory-qdrant-9a54cf123493-000001",
    "postgres_volume": "governed-memory-postgres-data-9a54cf123493-000001",
    "qdrant_volume": "governed-memory-qdrant-data-9a54cf123493-000001",
    "network": "governed-memory-net-9a54cf123493-000001",
    "database": "governed_memory",
    "collection": "governed_memory_9a54cf123493_000001",
    "alias": "governed_memory_active",
}
OBSERVATION_KEYS = {
    "schema_version",
    "operation",
    "candidate_git_commit",
    "authorization_scope_sha256",
    "hostname",
    "api_port_available",
    "postgres_port_available",
    "qdrant_port_available",
    "frontend_firewall_proof_sha256",
    "targets",
    "pilot_ever_started",
    "postgresql_user_row_count",
    "qdrant_point_count",
    "active_client_count",
}
EXPECTED_RUNTIME_RECEIPT_KEYS = {
    "build_lock",
    "build_lock_sha256",
    "candidate_python",
    "candidate_python_is_symlink",
    "candidate_python_sha256",
    "legacy_environment_imported",
    "network_calls",
    "persistent_resources_created",
    "pip_present",
    "platform",
    "production_state_changed",
    "project_distribution",
    "project_wheel",
    "project_wheel_sha256",
    "provider_calls",
    "python_version",
    "runtime_lock",
    "runtime_lock_sha256",
    "runtime_package_count",
    "runtime_packages",
    "schema_version",
    "setuptools_present",
    "source_tree_sha256",
    "user_site_enabled",
    "wheel_present",
}


class ReleaseGuardError(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonValue(ValueError):
    pass


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _load_json(path: Path, *, maximum_bytes: int = 256 * 1024) -> object:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum_bytes:
        raise ReleaseGuardError("release_json_invalid")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _NonFiniteJsonValue,
    ) as error:
        raise ReleaseGuardError("release_json_invalid") from error


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseGuardError(code)


def _verify_runtime_receipt(receipt: object) -> None:
    _require(
        isinstance(receipt, dict) and set(receipt) == EXPECTED_RUNTIME_RECEIPT_KEYS,
        "release_runtime_contract_invalid",
    )
    assert isinstance(receipt, dict)
    runtime_package_manifest = _load_json(RUNTIME_PACKAGES)
    _require(
        isinstance(runtime_package_manifest, dict)
        and isinstance(runtime_package_manifest.get("packages"), dict),
        "release_runtime_contract_invalid",
    )
    expected_packages = {
        str(key).lower(): value
        for key, value in runtime_package_manifest["packages"].items()
    }
    _require(
        receipt.get("schema_version") == "governed-memory-runtime-build-receipt-v1"
        and receipt.get("source_tree_sha256") == EXPECTED_RUNTIME_SOURCE_SHA256
        and receipt.get("candidate_python") == EXPECTED_RUNTIME_PYTHON
        and receipt.get("candidate_python_sha256") == EXPECTED_RUNTIME_PYTHON_SHA256
        and receipt.get("candidate_python_is_symlink") is False
        and receipt.get("project_wheel") == EXPECTED_RUNTIME_WHEEL
        and receipt.get("project_wheel_sha256") == EXPECTED_RUNTIME_WHEEL_SHA256
        and receipt.get("runtime_lock")
        == "ops/governed_memory/runtime-requirements.lock"
        and receipt.get("runtime_lock_sha256") == EXPECTED_RUNTIME_LOCK_SHA256
        and receipt.get("build_lock")
        == "ops/governed_memory/build-requirements.lock"
        and receipt.get("build_lock_sha256") == EXPECTED_BUILD_LOCK_SHA256
        and receipt.get("runtime_packages") == expected_packages
        and receipt.get("runtime_package_count") == len(expected_packages)
        and receipt.get("project_distribution")
        == {"name": "governed-memory-successor", "version": "0.0.0"}
        and receipt.get("python_version") == "3.12.3"
        and receipt.get("platform") == "linux_x86_64"
        and receipt.get("network_calls") == 0
        and receipt.get("provider_calls") == 0
        and receipt.get("persistent_resources_created") is False
        and receipt.get("production_state_changed") is False
        and receipt.get("legacy_environment_imported") is False
        and receipt.get("pip_present") is False
        and receipt.get("setuptools_present") is False
        and receipt.get("wheel_present") is False
        and receipt.get("user_site_enabled") is False,
        "release_runtime_contract_invalid",
    )


def _verify_chat_only_scope(value: Mapping[str, object]) -> None:
    _require(
        value.get("source_erasure_selectors")
        == ["thread", "message_tail", "recent", "all_conversations"]
        and value.get("source_erasure_direct_delete_roots")
        == ["public.chat_log", "public.chat_attachments", "public.threads"]
        and value.get("source_erasure_memory_only_or_account_wide_memory_selector_allowed")
        is False
        and value.get("source_erasure_structured_lifeswitch_data_or_accounts_deleted")
        is False
        and value.get("source_erasure_legacy_project_rows_deleted") is False
        and value.get("source_erasure_unclassified_side_effects_allowed") is False
        and value.get("source_erasure_unknown_dependency_action")
        == (
            "fail_closed_before_delete_on_unknown_foreign_key_delete_trigger_"
            "delete_rule_or_inheritance"
        ),
        "release_source_erasure_scope_invalid",
    )


def _verify_phase7c_proof(value: object) -> None:
    error = "release_phase7c_disposable_proof_invalid"
    if not isinstance(value, dict) or set(value) != EXPECTED_PHASE7C_PROOF_WRAPPER_KEYS:
        raise ReleaseGuardError(error)
    proof = value.get("proof_receipt")
    http = value.get("http_vertical_slice_receipt")
    deletion = value.get("deletion_receipt")
    resilience = value.get("deletion_resilience_receipt")
    if (
        not isinstance(proof, dict)
        or set(proof) != EXPECTED_PHASE7C_PROOF_KEYS
        or not isinstance(http, dict)
        or set(http) != EXPECTED_PHASE7C_HTTP_RECEIPT_KEYS
        or not isinstance(deletion, dict)
        or set(deletion) != EXPECTED_PHASE7C_DELETION_RECEIPT_KEYS
        or not isinstance(resilience, dict)
        or set(resilience) != EXPECTED_PHASE7C_RESILIENCE_RECEIPT_KEYS
    ):
        raise ReleaseGuardError(error)
    http_sha256 = _canonical_json_sha256(http)
    deletion_sha256 = _canonical_json_sha256(deletion)
    resilience_sha256 = _canonical_json_sha256(resilience)
    if (
        value.get("schema_version")
        != "governed-memory-current-disposable-proof-v2"
        or value.get("phase") != "phase7c"
        or value.get("attested_pre_promotion_runtime_manifest_sha256")
        != EXPECTED_PHASE7C_ATTESTED_RUNTIME_MANIFEST_SHA256
        or value.get("proof_log_sha256") != EXPECTED_PHASE7C_PROOF_LOG_SHA256
        or value.get("proof_receipt_canonicalization")
        != "utf8_json_sorted_keys_compact_no_newline_v1"
        or value.get("proof_receipt_canonical_sha256")
        != EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256
        or _canonical_json_sha256(proof)
        != EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256
        or http_sha256 != EXPECTED_PHASE7C_HTTP_RECEIPT_SHA256
        or deletion_sha256 != EXPECTED_PHASE7C_DELETION_RECEIPT_SHA256
        or resilience_sha256 != EXPECTED_PHASE7C_RESILIENCE_RECEIPT_SHA256
        or proof.get("integration_receipt_sha256") != http_sha256
        or proof.get("deletion_receipt_sha256") != deletion_sha256
        or proof.get("deletion_resilience_receipt_sha256") != resilience_sha256
    ):
        raise ReleaseGuardError(error)
    if (
        proof.get("schema_version")
        != "governed-memory-successor-disposable-run-v6"
        or proof.get("result") != "passed"
        or proof.get("branch")
        != "codex/clean-memory-successor-phase6b-20260810"
        or proof.get("candidate_head")
        != "5c9524b463c4760297dcebe489271af8d0b246a7"
        or proof.get("candidate_tree")
        != "be2dffb8aa929548462c5e1eafc684d05febb02e"
        or proof.get("source_tree_sha256")
        != EXPECTED_RUNTIME_SOURCE_SHA256
        or proof.get("runtime_build_receipt_sha256")
        != EXPECTED_RUNTIME_RECEIPT_SHA256
        or proof.get("manifest_sha256")
        != EXPECTED_PHASE7C_ATTESTED_MIGRATION_MANIFEST_SHA256
        or proof.get("runtime_packages_sha256")
        != EXPECTED_RUNTIME_PACKAGES_SHA256
        or proof.get("runtime_lock_sha256") != EXPECTED_RUNTIME_LOCK_SHA256
        or proof.get("candidate_unchanged") is not True
        or proof.get("external_network_calls") != 0
        or proof.get("loopback_application_endpoints") is not True
        or proof.get("traced_internal_bridge_connects") is not True
        or proof.get("published_container_ports") is not False
        or proof.get("provider_external_calls") != 0
        or proof.get("production_data_read") is not False
        or proof.get("production_endpoint_calls") != 0
        or proof.get("production_service_invoked") is not False
        or proof.get("docker_persistent_mounts") is not False
        or proof.get("ports_released") is not True
        or proof.get("resources_removed") is not True
        or proof.get("rollback_reapply") != "passed"
        or proof.get("semantic_threshold_calibrated") is not False
        or proof.get("postgres_server_version") != "16.14"
        or proof.get("qdrant_server_version") != "1.19.0"
        or proof.get("qdrant_image_digest")
        != "qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
    ):
        raise ReleaseGuardError(error)
    expected_terminal_identity = {
        "run_id": "019fe927",
        "invocation_id": "1673c63e-c4d0-4107-8597-aad2644fb8e0",
        "connect_trace_sha256": (
            "ca29fcc42714d04ddab5e226fbf9abbe13d7ab7cfa3ebfc79003e29e75b06291"
        ),
        "network_id": (
            "1183060198a217036e9045979bd8c101638acf8927ee2e39e5b1e4f6b7e124b8"
        ),
        "postgres_container_id": (
            "2e2575f210008b7dd36c16b2eb7947aa9ffccbecde0c7356a860ed0202d02faf"
        ),
        "qdrant_container_id": (
            "0d067d4873c92f4f2a937682360f5387ebceaf9057014b0e5b9d02e8703c953c"
        ),
        "postgres_image_id": (
            "sha256:de3a4eab8fdfa507ea92aac488b916b08089e515db49b055fe71dfa271ba3a28"
        ),
        "qdrant_image_id": (
            "sha256:92c4050629efe895f87dafd2830f1cd4d0532bc9967b777cab979ebda71612b3"
        ),
        "foundation_logical_dump_sha256": (
            "d4468ca2ff444cf2aee13c6911d5ba76b5b7daf35afebab18e8be967726af551"
        ),
        "bridge_logical_dump_sha256": (
            "ee016eb399995897cfb14dfd1c83572f11e53481e090c36162b9fa7e5ee9ebe2"
        ),
    }
    if any(
        proof.get(key) != expected
        for key, expected in expected_terminal_identity.items()
    ):
        raise ReleaseGuardError(error)
    for key in (
        "connect_trace_sha256",
        "network_id",
        "postgres_container_id",
        "qdrant_container_id",
        "foundation_logical_dump_sha256",
        "bridge_logical_dump_sha256",
        "integration_receipt_sha256",
        "deletion_receipt_sha256",
        "deletion_resilience_receipt_sha256",
    ):
        if HASH_RE.fullmatch(str(proof.get(key, ""))) is None:
            raise ReleaseGuardError(error)
    for key in ("postgres_image_id", "qdrant_image_id"):
        observed = proof.get(key)
        if not isinstance(observed, str) or not observed.startswith("sha256:"):
            raise ReleaseGuardError(error)
        if HASH_RE.fullmatch(observed.removeprefix("sha256:")) is None:
            raise ReleaseGuardError(error)
    if (
        http.get("schema")
        != "governed-memory-successor-http-integration-receipt-v2"
        or http.get("cold_extraction_lease") is not True
        or http.get("cold_projection_rebuild") is not True
        or http.get("embedding_dispatch_adversarial") is not True
        or http.get("embedding_dispatch_marker_count") != 2
        or http.get("http_owner_lifecycle") is not True
        or http.get("jwks_fetch_count") != 1
        or http.get("projection_lease_version_fenced") is not True
        or http.get("synthetic_jwt_only") is not True
        or http.get("production_data_read") is not False
        or http.get("production_endpoint_calls") != 0
        or http.get("production_service_invoked") is not False
        or http.get("provider_external_calls") != 0
        or http.get("semantic_threshold_calibrated") is not False
    ):
        raise ReleaseGuardError(error)
    expected_http_identity = {
        "answer_binding_sha256": (
            "32335bf8882ad58ad22bba31c746ff16a7c6252a5de13fd6520922484a697822"
        ),
        "auth_negative_matrix_sha256": (
            "c7c34090822a0d7ce32c5ed672cefdd8a89d9f462ed30510a0e923198a57e2b1"
        ),
        "chat_a_thread_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "chat_b_thread_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
        "claim_id": "ff471498-cb50-43c1-9ac7-738b64f8710f",
        "corrected_revision_id": "b8893939-723b-4c91-bc90-2658e7e55101",
        "deletion_receipt_sha256": (
            "55dfdf9988937e4b913f4aabe3d69110e24d4f1fc88aa496166b68a97ac4e3c4"
        ),
        "embedding_dispatch_marker_sha256": (
            "50a5801c0b38caedec174d6f142bdb7d791fd77fd8ce62cbcb5dd3b137a7a9cb"
        ),
        "http_lifecycle_sha256": (
            "e7b0264450891baf8557993abab7fe898e1b2b5c7edadafb17b36c53bd9c6b01"
        ),
        "inactive_worker_runtime_sha256": (
            "6bd10fb1a63d2d75906b59bc70a9313b9f6f1a421a00b769e2b8bc7130de90b1"
        ),
        "initial_revision_id": "4ba71787-d052-495f-a889-863deb6a7f55",
        "jwks_manifest_sha256": (
            "34f3fd2f85e4a11ef17e415f8d21d578164ec2aaeb33941cdc48dec1ba6bcc77"
        ),
        "owner_isolation_sha256": (
            "35a26d7866ad7a18808914a140daa2b6f35a9077db6418ad81e5a4b784d9c655"
        ),
        "rebuild_manifest_sha256": (
            "51bfce19a2b64335e3539a3031deebaabee100af2dfecc978670dec83ddd91fb"
        ),
        "review_surface_sha256": (
            "de529fa76d535a751f0afaf3f0ed4400ba9614a06855aa9a6e4d1ea0341b912d"
        ),
        "route_manifest_sha256": (
            "580abf16d9a08936d15c2c4b31fd1d7023da3a6b8c16314fb2326fda5763cf3b"
        ),
    }
    if any(
        http.get(key) != expected
        for key, expected in expected_http_identity.items()
    ):
        raise ReleaseGuardError(error)
    if (
        deletion.get("schema")
        != "governed-memory-successor-conversation-deletion-disposable-receipt-v1"
        or deletion.get("attachment_thread_bridge_absence") is not True
        or deletion.get("clean_composite_auxiliary_fk_count") != 5
        or deletion.get("target_message_count") != 2
        or deletion.get("target_thread_count") != 1
        or deletion.get("deleted_message_count") != 2
        or deletion.get("deleted_thread_count") != 1
        or deletion.get("deleted_attachment_count") != 2
        or deletion.get("deleted_bridge_row_count") != 1
        or deletion.get("deleted_claim_count") != 1
        or deletion.get("governed_message_tombstone_count") != 2
        or deletion.get("lifeswitch_snapshot_bytes") != 2765
        or deletion.get("lifeswitch_snapshot_sha256")
        != "6fc270d38b681af35d1db008d04ff65fd790a2e44bef5791a23119ab99fcddae"
        or deletion.get("live_runtime_catalog_refusal_count") != 4
        or deletion.get("other_owner_snapshot_bytes") != 19456
        or deletion.get("other_owner_snapshot_sha256")
        != "5ab66318a199fab78182fc7d9fcee43d9323e2c8ada1011367a84a32c89e337d"
        or deletion.get("other_owner_qdrant_snapshot_bytes") != 27628
        or deletion.get("other_owner_qdrant_snapshot_sha256")
        != "430f74a8ed8961f13964bf65b1905685ce5c7643d826441f4c75dfdd7405b1f6"
        or deletion.get("page_boundary_tested") is not False
        or deletion.get("crash_injection_tested") is not False
        or deletion.get("stale_lease_injection_tested") is not False
        or deletion.get("qdrant_target_absent_alias_and_physical") is not True
        or deletion.get("qdrant_delete_outcome_unknown_resolved_by_readback")
        is not True
        or deletion.get("coordinator_no_work_after_completion") is not True
        or deletion.get("exact_completed_request_replay") is not True
        or deletion.get("transient_targets_purged") is not True
        or deletion.get("source_message_tombstone_count") != 2
        or deletion.get("source_thread_tombstone_count") != 1
        or deletion.get("typed_project_conflict_code")
        != "governed_project_thread_erasure_required"
        or deletion.get("typed_project_conflict_status") != 409
        or deletion.get("typed_project_conflict_retryable") is not False
        or deletion.get("production_data_read") is not False
        or deletion.get("production_endpoint_calls") != 0
        or deletion.get("production_service_invoked") is not False
        or deletion.get("provider_external_calls") != 0
        or deletion.get("synthetic_provider_only") is not True
    ):
        raise ReleaseGuardError(error)
    expected_deletion_receipts = {
        "claim_deletion_receipt_sha256": (
            "54269002658c6f56e5d0b9cc6ef4c8c55e2ba725df316bef98ae64420a477f5a"
        ),
        "coordinator_final_receipt_sha256": (
            "c10b5c9803b9ded83b425c76f91a726607eb30751572dab80fa5de0a7dee8556"
        ),
        "governed_final_receipt_sha256": (
            "814be0d9b4f2091132cb9f8c27e001305adb85fc4560369dfbd715cdd281df78"
        ),
        "qdrant_deletion_receipt_sha256": (
            "7d4df2aca2fa36592a8bee118491fa02d1c343e10d871d36d67a8a04f912ea84"
        ),
        "source_conversation_final_receipt_sha256": (
            "27188f3cfb21103a895b0899e6f85b75daeea33781a719f14f92b0f07c8997e8"
        ),
    }
    if any(
        deletion.get(key) != expected
        for key, expected in expected_deletion_receipts.items()
    ):
        raise ReleaseGuardError(error)
    crash_keys = (
        "crash_after_conversation_completion_ack",
        "crash_after_conversation_finalize",
        "crash_after_governed_receipt_handoff",
        "crash_after_source_claim_step",
        "crash_after_source_memory_finalize",
        "crash_after_source_register",
        "crash_after_successor_completion_ack",
        "crash_after_target_append",
        "crash_after_target_seal",
    )
    if (
        resilience.get("schema")
        != "governed-memory-successor-deletion-resilience-receipt-v1"
        or resilience.get("target_count") != 501
        or resilience.get("deleted_message_count") != 501
        or resilience.get("deleted_thread_count") != 1
        or resilience.get("deleted_attachment_count") != 1
        or resilience.get("source_page_count") != 2
        or resilience.get("successor_page_count") != 2
        or resilience.get("crash_boundary_count") != 9
        or any(resilience.get(key) is not True for key in crash_keys)
        or resilience.get("final_absence_verified") is not True
        or resilience.get("future_dated_chat_refused") is not True
        or resilience.get("exhausted_attempts_manual_review") is not True
        or resilience.get("pending_ack_attempt_cap_recovered") is not True
        or resilience.get("post_completion_no_work") is not True
        or resilience.get("stale_lease_read_refused") is not True
        or resilience.get("stale_lease_release_refused") is not True
        or resilience.get("stale_lease_replaced") is not True
        or resilience.get("successor_page_replay_conflict_refused") is not True
        or resilience.get("successor_page_replay_exact") is not True
        or resilience.get("attachment_identity_reuse_refused") is not True
        or resilience.get("attachment_movement_refused") is not True
        or resilience.get("production_data_read") is not False
        or resilience.get("production_endpoint_calls") != 0
        or resilience.get("provider_external_calls") != 0
    ):
        raise ReleaseGuardError(error)
    expected_resilience_receipts = {
        "conversation_final_receipt_sha256": (
            "d694eebdd531c317b00f3980621b785a02c0da011588f397c997d66ff5f878f9"
        ),
        "crash_boundary_manifest_sha256": (
            "f218ee17713c53f32ea3cb47d89f84f330688c99282391ce43723fc81e1b1247"
        ),
        "final_absence_manifest_sha256": (
            "79f69bc6b0e8f2d8bf31e734113d27dc1d68dac42bc993413bc1aed2c394b4da"
        ),
        "source_page_sizes_sha256": (
            "4ad9c2cf3c3cb9eed697e4da63405f5b6b1f01798d8c33005cb817d6b5b960f3"
        ),
        "source_target_manifest_sha256": (
            "ee870faad72c107443dc917697ce6162cb1c2e593bd5066bd1189ac9e1dc6de3"
        ),
        "successor_final_receipt_sha256": (
            "d185d12475b6f4446c48d86abab317a0a16d53e32a77da7f81980f19d9fcf220"
        ),
        "successor_page_sizes_sha256": (
            "e6bf8fb047155955c1a235ce53569b48cd6a3d39c097ea2c9c5ac65f7fbdba82"
        ),
    }
    if any(
        resilience.get(key) != expected
        for key, expected in expected_resilience_receipts.items()
    ):
        raise ReleaseGuardError(error)


def _verify_runtime_manifest(runtime: object) -> None:
    _require(isinstance(runtime, dict), "release_runtime_manifest_invalid")
    assert isinstance(runtime, dict)
    validation = runtime.get("validation_runtime")
    disposable = runtime.get("disposable_validation")
    activation = runtime.get("activation")
    http = runtime.get("http_runtime")
    infrastructure = runtime.get("infrastructure")
    release = runtime.get("release_guard")
    ingestion = runtime.get("ingestion")
    frontend = runtime.get("frontend_candidate")
    history = runtime.get("historical_evidence")
    _require(
        runtime.get("schema_version") == "governed-memory-successor-runtime-manifest-v1"
        and runtime.get("phase")
        == (
            "phase7c_inactive_installation_package_disposable_revalidated_"
            "activation_blocked"
        )
        and runtime.get("production_state_changed") is False
        and runtime.get("legacy_imports_allowed") is False
        and isinstance(validation, dict)
        and validation.get("current_source_tree_sha256")
        == EXPECTED_RUNTIME_SOURCE_SHA256
        and validation.get("current_candidate_python") == EXPECTED_RUNTIME_PYTHON
        and validation.get("current_candidate_python_sha256")
        == EXPECTED_RUNTIME_PYTHON_SHA256
        and validation.get("current_project_wheel_sha256")
        == EXPECTED_RUNTIME_WHEEL_SHA256
        and validation.get("current_build_receipt_sha256")
        == EXPECTED_RUNTIME_RECEIPT_SHA256
        and validation.get("current_source_bound") is True
        and validation.get("current_runtime_rebuild_pending") is False
        and disposable == EXPECTED_DISPOSABLE_VALIDATION
        and isinstance(activation, dict)
        and activation.get("blockers") == EXPECTED_ACTIVATION_BLOCKERS
        and activation.get("production_authorized") is False
        and activation.get("installed_services") == []
        and activation.get("running_services") == []
        and activation.get("enabled_services") == []
        and activation.get("installed_timers") == []
        and activation.get("enabled_timers") == []
        and isinstance(http, dict)
        and http.get("default_mode") == "off"
        and http.get("conversation_erasure_route_candidate_implemented") is True
        and http.get("conversation_erasure_route_installed") is False
        and http.get("conversation_erasure_route_routed") is False
        and http.get("conversation_erasure_route_live_verified") is False
        and http.get("conversation_erasure_route_live_supabase_verified") is False
        and http.get("supabase_auth_sessions_rpc_live_verified") is False
        and http.get("conversation_bridge_catalog_hash_provisioned") is False
        and http.get("source_logging_policy_live_verified") is False
        and http.get("source_log_duration_observed") == "off"
        and http.get("source_log_parameter_max_length_observed")
        == "-1_full_bind_values_unsafe"
        and http.get("source_logging_parameter_remediation_authorized") is False
        and http.get("source_logging_parameter_remediation_applied") is False
        and isinstance(infrastructure, dict)
        and infrastructure.get("postgresql_persistent_resource_created") is False
        and infrastructure.get("qdrant_persistent_resource_created") is False
        and infrastructure.get("existing_production_store_reuse_allowed") is False
        and infrastructure.get("legacy_snapshot_or_mount_reuse_allowed") is False
        and infrastructure.get("qdrant_persistent_pilot_image") is None
        and isinstance(release, dict)
        and release
        == {
            "create_allowed": False,
            "create_refusal_code": "activation_blockers_open",
            "cleanup_allowed": False,
            "cleanup_refusal_code": "authorization_missing",
            "commands_executed": 0,
        }
        and isinstance(ingestion, dict)
        and isinstance(frontend, dict)
        and frontend
        == {
            "git_commit": "71377a838058d75320b55817fc8c9656d404f955",
            "git_commit_short": "71377a",
            "git_tree": "7d29afff30676eccc77465e5f144ec9129aaae4d",
            "parent_live_commit": "9015eb0efc0cddbda3f9fd812c4f550b0c1f5b3b",
            "build_id": "-zedD-GFt2yko7J17swb6",
            "built": True,
            "deployed": False,
            "authenticated_visual_qa_complete": False,
            "successor_operation_id_and_confirmation_semantics_verified": False,
        }
        and isinstance(history, dict)
        and history.get("phase6e_runtime_build_receipt")
        == "ops/governed_memory/history/phase6e/runtime_build_receipt.json"
        and history.get("phase6e_runtime_build_receipt_sha256")
        == EXPECTED_FIXED_ARTIFACT_HASHES[
            "ops/governed_memory/history/phase6e/runtime_build_receipt.json"
        ]
        and history.get("phase6e_disposable_proof_receipt")
        == "ops/governed_memory/history/phase6e/disposable_proof_receipt.json"
        and history.get("phase6e_disposable_proof_receipt_sha256")
        == EXPECTED_FIXED_ARTIFACT_HASHES[
            "ops/governed_memory/history/phase6e/disposable_proof_receipt.json"
        ]
        and history.get("reusable_for_current_candidate") is False,
        "release_runtime_manifest_invalid",
    )
    assert isinstance(ingestion, dict)
    _verify_chat_only_scope(ingestion)


def _verify_bootstrap_and_pilot(bootstrap: object, pilot: object) -> None:
    _require(
        isinstance(bootstrap, dict) and isinstance(pilot, dict),
        "release_governance_contract_invalid",
    )
    assert isinstance(bootstrap, dict) and isinstance(pilot, dict)
    create = bootstrap.get("create_policy")
    cleanup = bootstrap.get("cleanup_policy")
    bridge = bootstrap.get("conversation_bridge")
    source_erasure = pilot.get("source_erasure")
    _require(
        bootstrap.get("schema_version") == "governed-memory-bootstrap-contract-v1"
        and bootstrap.get("state") == "inactive_candidate_no_resources_created"
        and bootstrap.get("production_state_changed") is False
        and isinstance(create, dict)
        and create.get("current_create_authorized") is False
        and create.get("unresolved_creation_prerequisites")
        == EXPECTED_ACTIVATION_BLOCKERS
        and isinstance(cleanup, dict)
        and cleanup.get("current_cleanup_authorized") is False
        and cleanup.get("requires_external_scoped_cleanup_authorization") is True
        and cleanup.get("sql_cascade_allowed") is False
        and cleanup.get("wildcard_target_allowed") is False
        and cleanup.get("prefix_target_allowed") is False
        and isinstance(bridge, dict)
        and bridge.get("source_erasure_status")
        == "phase7c_real_disposable_validated_inactive_not_routed_not_production_applied"
        and bootstrap.get("candidate_implementation_status")
        == EXPECTED_BOOTSTRAP_IMPLEMENTATION_STATUS
        and bridge.get("source_erasure_structured_lifeswitch_tables_or_accounts_deleted")
        is False
        and bridge.get("source_erasure_legacy_project_rows_deleted") is False
        and pilot.get("schema_version") == "governed-memory-pilot-contract-v1"
        and pilot.get("state") == "inactive_candidate_blocked_not_authorized"
        and pilot.get("production_state_changed") is False
        and pilot.get("provider_policy") == EXPECTED_PILOT_PROVIDER_POLICY
        and pilot.get("candidate_surfaces") == EXPECTED_PILOT_CANDIDATE_SURFACES
        and isinstance(source_erasure, dict)
        and source_erasure.get("status")
        == "phase7c_real_disposable_validated_inactive_not_routed_not_production_applied"
        and pilot.get("start_blockers") == EXPECTED_ACTIVATION_BLOCKERS
        and pilot.get("eligible_input", {}).get("old_conversations") is False
        and pilot.get("eligible_input", {}).get("historical_backfill") is False
        and pilot.get("eligible_input", {}).get("attachment_content") is False
        and pilot.get("required_start_state", {}).get("legacy_import_count") == 0
        and pilot.get("required_start_state", {}).get("unprocessed_prefill_count")
        == 0
        and source_erasure.get("structured_lifeswitch_data_deleted") is False
        and source_erasure.get("accounts_deleted") is False
        and source_erasure.get("legacy_project_rows_deleted") is False,
        "release_governance_contract_invalid",
    )


def _verify_installation_text_contracts() -> None:
    _require(not FINALIZER.exists(), "release_retired_finalizer_present")
    _require(
        not RETIRED_CURRENT_PHASE6E_PROOF.exists(),
        "release_retired_current_proof_present",
    )
    package_text = PACKAGE_MANIFEST.read_text(encoding="utf-8")
    installer_text = INSTALLER.read_text(encoding="utf-8")
    _require(
        "canonical_bootstrap_finalize.pgsql" not in package_text
        and "canonical_bootstrap_finalize.pgsql" not in installer_text,
        "release_retired_finalizer_referenced",
    )
    canonical = (
        OPS / "installation" / "postgres" / "canonical_cluster.pgsql.in"
    ).read_text(encoding="utf-8")
    source = (
        OPS / "installation" / "postgres" / "source_cluster_roles.pgsql.in"
    ).read_text(encoding="utf-8")
    roles_preflight = (
        MIGRATION_ROOT / "roles_preflight.pgsql"
    ).read_text(encoding="utf-8")
    _require(
        "current_setting('shared_preload_libraries')" not in roles_preflight
        and "privileged canonical cluster bootstrap verifies" in roles_preflight,
        "release_logging_preflight_boundary_invalid",
    )

    for value in (canonical, source):
        _require(
            "log_parameter_max_length" in value
            and "log_parameter_max_length_on_error" in value
            and "log_duration" in value
            and "pgaudit" in value.lower(),
            "release_logging_preflight_invalid",
        )


def verify_candidate_artifacts() -> dict[str, object]:
    observed_hashes: dict[str, str] = {}
    for relative, expected in sorted(EXPECTED_FIXED_ARTIFACT_HASHES.items()):
        path = ROOT / relative
        actual = _sha256(path)
        _require(actual == expected, "release_artifact_hash_mismatch")
        observed_hashes[relative] = actual
    _require(
        _sha256(RUNTIME_LOCK) == EXPECTED_RUNTIME_LOCK_SHA256
        and _sha256(BUILD_LOCK) == EXPECTED_BUILD_LOCK_SHA256
        and _sha256(RUNTIME_PACKAGES) == EXPECTED_RUNTIME_PACKAGES_SHA256,
        "release_runtime_lock_invalid",
    )
    try:
        package = verify_installation_package()
    except InstallationPackageError as error:
        raise ReleaseGuardError("release_installation_package_invalid") from error
    _require(
        package.get("package_manifest_sha256")
        == EXPECTED_PACKAGE_MANIFEST_SHA256
        and package.get("evaluator_mutating_commands_executed") == 0
        and package.get("evaluator_provider_calls") == 0
        and package.get("evaluator_state_changed") is False,
        "release_installation_package_invalid",
    )
    migration = verify_migration_manifest(MIGRATION_ROOT)
    _require(
        migration
        == {
            "schema_version": "governed-memory-migration-verification-v4",
            "result": "verified",
            "validation_state": (
                "disposable_validated"
            ),
            "manifest_sha256": EXPECTED_MIGRATION_MANIFEST_SHA256,
            "migration_package_id_sha256": EXPECTED_MIGRATION_PACKAGE_ID_SHA256,
            "file_count": 15,
        },
        "release_migration_manifest_invalid",
    )
    receipt = _load_json(RUNTIME_BUILD_RECEIPT)
    phase7c_proof = _load_json(PHASE7C_DISPOSABLE_PROOF)
    runtime = _load_json(RUNTIME_MANIFEST)
    bootstrap = _load_json(BOOTSTRAP)
    pilot = _load_json(PILOT)
    _verify_runtime_receipt(receipt)
    _verify_phase7c_proof(phase7c_proof)
    _verify_runtime_manifest(runtime)
    _verify_bootstrap_and_pilot(bootstrap, pilot)
    _verify_installation_text_contracts()
    receipt_schema = _load_json(RECEIPT_SCHEMA)
    _require(
        isinstance(receipt_schema, dict)
        and receipt_schema.get("additionalProperties") is False,
        "release_receipt_schema_invalid",
    )
    observed_hashes.update(
        {
            RUNTIME_LOCK.relative_to(ROOT).as_posix(): _sha256(RUNTIME_LOCK),
            BUILD_LOCK.relative_to(ROOT).as_posix(): _sha256(BUILD_LOCK),
            RUNTIME_PACKAGES.relative_to(ROOT).as_posix(): _sha256(
                RUNTIME_PACKAGES
            ),
            RELEASE_GUARD.relative_to(ROOT).as_posix(): _sha256(RELEASE_GUARD),
        }
    )
    return {
        "schema_version": "governed-memory-release-artifact-verification-v2",
        "phase": "phase7c_disposable_revalidated_inactive_installation_package",
        "artifact_sha256": dict(sorted(observed_hashes.items())),
        "installation_package_artifact_count": len(
            package.get("artifact_sha256", {})
        ),
        "installation_package_manifest_sha256": EXPECTED_PACKAGE_MANIFEST_SHA256,
        "migration_manifest_sha256": EXPECTED_MIGRATION_MANIFEST_SHA256,
        "runtime_source_sha256": EXPECTED_RUNTIME_SOURCE_SHA256,
        "disposable_revalidation_required": False,
        "installation_authorized": False,
        "activation_authorized": False,
        "external_calls": 0,
        "commands_executed": 0,
        "production_state_changed": False,
    }


def _checked_observation(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != OBSERVATION_KEYS:
        raise ReleaseGuardError("release_observation_invalid")
    if (
        document["schema_version"] != "governed-memory-release-observation-v1"
        or document["operation"] not in {"create", "cleanup"}
        or not isinstance(document["candidate_git_commit"], str)
        or COMMIT_RE.fullmatch(document["candidate_git_commit"]) is None
        or not isinstance(document["authorization_scope_sha256"], str)
        or HASH_RE.fullmatch(document["authorization_scope_sha256"]) is None
        or document["hostname"] != "ip-172-31-32-171"
    ):
        raise ReleaseGuardError("release_observation_invalid")
    for key in (
        "api_port_available",
        "postgres_port_available",
        "qdrant_port_available",
        "pilot_ever_started",
    ):
        if type(document[key]) is not bool:
            raise ReleaseGuardError("release_observation_invalid")
    proof = document["frontend_firewall_proof_sha256"]
    if proof is not None and (
        not isinstance(proof, str) or HASH_RE.fullmatch(proof) is None
    ):
        raise ReleaseGuardError("release_observation_invalid")
    for key in (
        "postgresql_user_row_count",
        "qdrant_point_count",
        "active_client_count",
    ):
        if type(document[key]) is not int or document[key] < 0:
            raise ReleaseGuardError("release_observation_invalid")
    targets = document["targets"]
    if not isinstance(targets, dict) or set(targets) != set(EXACT_TARGETS):
        raise ReleaseGuardError("release_observation_invalid")
    for key, exact_name in EXACT_TARGETS.items():
        state = targets[key]
        if (
            not isinstance(state, dict)
            or set(state) != {"name", "state"}
            or state["name"] != exact_name
            or state["state"] not in {"absent", "present_exact"}
        ):
            raise ReleaseGuardError("release_observation_invalid")
    return document


def evaluate_release_observation(document: object) -> dict[str, object]:
    observed = _checked_observation(document)
    operation = observed["operation"]
    reason = (
        "activation_blockers_open"
        if operation == "create"
        else "authorization_missing"
    )
    return {
        "schema_version": "governed-memory-release-decision-v1",
        "operation": operation,
        "allowed": False,
        "reason_code": reason,
        "exact_action_plan": [],
        "commands_executed": 0,
        "production_state_changed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="governed-memory-release-guard")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify-artifacts")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--observation", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify-artifacts":
            result = verify_candidate_artifacts()
            exit_code = 0
        else:
            result = evaluate_release_observation(
                _load_json(arguments.observation.resolve())
            )
            exit_code = 2
    except ReleaseGuardError as error:
        result = {
            "schema_version": "governed-memory-release-guard-error-v1",
            "error": {"code": str(error)},
        }
        exit_code = 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


__all__ = [
    "EXACT_TARGETS",
    "EXPECTED_ACTIVATION_BLOCKERS",
    "EXPECTED_MIGRATION_MANIFEST_SHA256",
    "EXPECTED_PACKAGE_MANIFEST_SHA256",
    "EXPECTED_RUNTIME_MANIFEST_SHA256",
    "ReleaseGuardError",
    "evaluate_release_observation",
    "verify_candidate_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
