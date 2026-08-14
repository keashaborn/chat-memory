#!/usr/bin/env python3
from __future__ import annotations

"""Repository-only integrity verifier and refusal-only release guard.

This module has no installation, rollback, activation, Docker, network, secret,
PostgreSQL, or Qdrant execution surface. Phase 7C receipts are immutable
historical evidence only. The current Phase 8G application proof, sealed Phase
9J inactive store package, and published dormant Phase 9J controller substrate are
verified separately. Phase 9J packages an administrative cooperative writer
fence but does not claim to exclude an equivalent privileged-root bypass;
production installation and activation remain refused.
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

from tools.governed_memory_install import package
from tools.governed_memory_validation import (
    verify_migration_manifest,
    verify_store_migration_manifest,
)


OPS = ROOT / "ops" / "governed_memory"
RUNTIME_MANIFEST = OPS / "runtime_manifest.json"
CURRENT_RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT = (
    OPS / "history" / "phase7c" / "runtime_build_receipt.json"
)
RUNTIME_LOCK = OPS / "runtime-requirements.lock"
BUILD_LOCK = OPS / "build-requirements.lock"
RUNTIME_PACKAGES = (
    ROOT / "tools" / "governed_memory_validation" / "runtime_packages.json"
)
BOOTSTRAP = OPS / "bootstrap_contract.json"
PILOT = OPS / "pilot_contract.json"
RECEIPT_SCHEMA = OPS / "release_receipt.schema.json"
CURRENT_COMPONENT_DISPOSITION = OPS / "current_component_disposition.json"
HISTORICAL_PHASE7C_APPLICATION_PROOF = (
    OPS / "history" / "phase7c" / "disposable_proof_receipt.json"
)
CURRENT_PHASE8G_APPLICATION_PROOF = OPS / "phase8g_disposable_proof_receipt.json"
CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT = (
    OPS / "phase9j_controller_runtime_release_receipt.json"
)
ROOT_MIGRATION_MANIFEST = ROOT / "governed-memory-migrations" / "manifest.json"
MIGRATION_ROOT = ROOT / "governed-memory-migrations"
SCHEMA_CONTRACT = MIGRATION_ROOT / "schema_contract.json"
DISPOSABLE_RUNNER = (
    ROOT / "tools" / "governed_memory_validation" / "run_disposable_successor.sh"
)
POSTGRES_BOOTSTRAP = (
    ROOT / "tools" / "governed_memory_validation" / "postgres_bootstrap.pgsql"
)

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
EXPECTED_PHASE7C_RUNTIME_RECEIPT_SHA256 = (
    "210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2"
)
EXPECTED_PHASE7C_RUNTIME_SOURCE_SHA256 = (
    "610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd"
)
EXPECTED_PHASE7C_RUNTIME_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
EXPECTED_PHASE7C_RUNTIME_WHEEL_SHA256 = (
    "2cd060454039e8e16149cd670bf0f8dba70d6d93cc749c542fa5cc86b938c05e"
)
EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_PHASE7C_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_PHASE7C_RUNTIME_PACKAGES_SHA256 = (
    "ed9273d6bd6dad6cf5680c478dff1beab453f66ab607914994fe8dc2b9d4e882"
)
EXPECTED_PHASE7C_RUNTIME_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256}-"
    f"{EXPECTED_PHASE7C_RUNTIME_SOURCE_SHA256}/bin/python"
)
EXPECTED_PHASE7C_RUNTIME_WHEEL = (
    "/tmp/governed-memory-successor-build-"
    f"{EXPECTED_PHASE7C_BUILD_LOCK_SHA256}-"
    f"{EXPECTED_PHASE7C_RUNTIME_SOURCE_SHA256}/dist/"
    "governed_memory_successor-0.0.0-py3-none-any.whl"
)

EXPECTED_PHASE8G_PROOF_RECEIPT_SHA256 = (
    "9ce44ac7d3ed08970d88f9ad40df14f82c9ad677af2629a9ff2f7d2aa9385e76"
)
EXPECTED_PHASE8G_PROOF_CANONICAL_SHA256 = (
    "5d4f1fd1a92dff80e3e3a779d634c2b996886368535c7fbd1530e1e0da6d46b8"
)
EXPECTED_PHASE8G_HTTP_RECEIPT_SHA256 = (
    "30105b2742079edf79a350f3e683979e70777b5334a16f683074eb65eb607229"
)
EXPECTED_PHASE8G_DELETION_RECEIPT_SHA256 = (
    "5f79424751272bb0058b6fd853e83f24c55be86b435ed90fe0ef529822919f10"
)
EXPECTED_PHASE8G_RESILIENCE_RECEIPT_SHA256 = (
    "37be6871ff6b2d39fba3cb82968a64ed479865d86b0ce1f191d43b2423543e68"
)
EXPECTED_PHASE8G_PROOF_LOG_SHA256 = (
    "9ff51264aff1c56d2c8570311ba116b64e1adae8d0282bae2cd395a745a400c1"
)
EXPECTED_PHASE8G_ATTESTED_RUNTIME_MANIFEST_SHA256 = (
    "5eb4f9e9c7a3249a8bbac86507a245c676829dbad70748b8f9cce20064754786"
)
EXPECTED_PHASE8G_ATTESTED_MIGRATION_MANIFEST_SHA256 = (
    "cb0633096bdd7f961dcb05881d722e7c0a53cb0661f66b5aca6f0dfde632ca5c"
)
EXPECTED_PHASE8G_PROMOTED_MIGRATION_MANIFEST_SHA256 = (
    "831962c268fc0f0be96d19d3186f0a26e7c60cf8f49bf28e80aa5e9d63a1bf99"
)
EXPECTED_PHASE8G_CANDIDATE_BRANCH = (
    "codex/governed-memory-phase8d-retirement-20260812"
)
EXPECTED_PHASE8G_CANDIDATE_HEAD = "c8691f0bef993b8e2edda982fe634c4b83e68590"
EXPECTED_PHASE8G_CANDIDATE_TREE = "9d95027a736a44d394c0f859821daa1554c88e22"
EXPECTED_PHASE8G_POSTGRES_IMAGE_DIGEST = (
    "postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)

EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256 = (
    "25ca53e683e53f79b726909ef64bc8afad30269cce3f59804cf66335667a8108"
)
EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256 = (
    "7de191f42a1c14b6bc2c29b3e7b425bd1b2593f7de95e03af7d2f93ff5d4a53b"
)
EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256 = (
    "2e5da1091b456b705d955c5cc3a119e507116f892de0bcb92017185ca4ed2e89"
)
EXPECTED_PHASE9J_RUNTIME_TREE_SHA256 = (
    "8842461fb701fc559437f52f71c95d47363e5bece2b9efbf246b2a54530b6403"
)
EXPECTED_PHASE9J_RELEASE_TREE_SHA256 = (
    "a8f9226a30718a4155c9835325aa13ed904ec8adbd453041ce2a7e89c235a2f8"
)
EXPECTED_CURRENT_RUNTIME_MANIFEST_SHA256 = (
    "23b0673d682a734cdf9fd886db014cacbe9697802efb5c5973a82f1c1f49d8a9"
)
EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256 = (
    "b52b753dc7974ee120e4abe264bb36f16b53341fa86b2ea8e0ab5bf86c735c20"
)
EXPECTED_CURRENT_RUNTIME_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
EXPECTED_CURRENT_RUNTIME_WHEEL_SHA256 = (
    "1a13ebf4d686e5d3ddb7a04979042f0751f78cc97722a733a193bb83bcef23d2"
)
EXPECTED_CURRENT_RUNTIME_PYTHON = (
    "/tmp/governed-memory-successor-runtime-"
    f"{EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256}-"
    f"{EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256}/bin/python"
)
EXPECTED_CURRENT_RUNTIME_WHEEL = (
    "/tmp/governed-memory-successor-build-"
    f"{EXPECTED_PHASE7C_BUILD_LOCK_SHA256}-"
    f"{EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256}/dist/"
    "governed_memory_successor-0.0.0-py3-none-any.whl"
)

EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES = {
    "ops/governed_memory/history/phase7c/disposable_proof_receipt.json": (
        EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
    ),
    "ops/governed_memory/history/phase7c/runtime_build_receipt.json": (
        EXPECTED_PHASE7C_RUNTIME_RECEIPT_SHA256
    ),
}

REQUIRED_ACTIVATION_BLOCKERS = frozenset(
    {
        "production_activation_not_authorized",
        "inactive_installation_package_not_authorized",
        (
            "lifeswitch_chat_answer_binding_provenance_and_owner_context_"
            "erasure_not_implemented_or_verified"
        ),
        (
            "conversation_erasure_auxiliary_deleted_object_counts_not_"
            "implemented_or_verified"
        ),
        "successor_answer_binding_chat_transaction_recovery_not_implemented",
    }
)
EXPECTED_ACTIVATION_BLOCKER_COUNT = 41
CURRENT_CREATE_REFUSAL_CODE = "inactive_installation_package_not_authorized"

EXACT_TARGETS = {
    "postgres_container": "governed-memory-postgres-9a54cf123493-000003",
    "qdrant_container": "governed-memory-qdrant-9a54cf123493-000003",
    "postgres_volume": "governed-memory-postgres-data-9a54cf123493-000003",
    "qdrant_volume": "governed-memory-qdrant-data-9a54cf123493-000003",
    "network": "governed-memory-net-9a54cf123493-000003",
    "database": "governed_memory",
    "collection": "governed_memory_9a54cf123493_000003",
    "alias": "governed_memory_active",
}

EXPECTED_CURRENT_COMPONENT_DISPOSITION: Mapping[str, object] = {
    "schema_version": "governed-memory-current-component-disposition-v3",
    "status": (
        "single_current_successor_with_published_dormant_controller_"
        "substrate_and_sealed_inactive_store_package"
    ),
    "authority": {
        "accounts": "supabase_auth_retained",
        "canonical_governed_memory": "fresh_successor_postgresql_only",
        "derived_vectors": "fresh_successor_qdrant_only_rebuildable",
        "conversation_source": "existing_owner_scoped_chat_tables",
        "structured_lifeswitch": (
            "outside_memory_install_rollback_and_chat_erasure_authority"
        ),
    },
    "current_successor": {
        "application_root": "app.py",
        "memory_runtime": "rag_engine/governed_memory",
        "chat_integrity": "rag_engine/chat_integrity.py",
        "chat_erasure_proxy": "rag_engine/governed_memory_erasure_proxy_v1.py",
        "package_manifest": (
            "ops/governed_memory/installation/current/package_manifest.json"
        ),
        "package_verifier": "tools/governed_memory_install/package.py",
        "install_entrypoint": (
            "tools/governed_memory_install/install_entrypoint.py"
        ),
        "empty_rollback_entrypoint": (
            "tools/governed_memory_install/rollback_entrypoint.py"
        ),
        "durable_receipt_store": (
            "tools/governed_memory_install/durable_receipts.py"
        ),
        "closed_linux_install_adapter": (
            "tools/governed_memory_install/linux_store_effects.py"
        ),
        "fixed_loopback_readiness_adapter": (
            "tools/governed_memory_install/linux_store_readiness.py"
        ),
        "physical_empty_rollback_adapter": (
            "tools/governed_memory_install/rollback_live_adapter.py"
        ),
        "controller_runtime_release_builder": (
            "tools/governed_memory_release/controller_runtime_builder.py"
        ),
        "closed_live_transport_contracts": (
            "tools/governed_memory_install/linux_live_transports.py"
        ),
        "closed_non_postgresql_live_linux_adapters": (
            "tools/governed_memory_install/linux_live_adapters.py"
        ),
        "controller_rollback_marker_transport": (
            "tools/governed_memory_install/live_rollback_marker.py"
        ),
        "postgresql_driver_adapter": (
            "tools/governed_memory_install/psycopg_postgres_adapter.py"
        ),
        "postgresql_native_stage_machine": (
            "tools/governed_memory_install/postgres_native_stages.py"
        ),
        "postgresql_native_stage_contract": (
            "ops/governed_memory/installation/current/postgres/native_stage_contract.json"
        ),
        "controller_runtime_publication_policy_transport": (
            "tools/governed_memory_release/runtime_publication_transport.py"
        ),
        "controller_runtime_publication_primitives": (
            "tools/governed_memory_release/linux_runtime_publication_primitives.py"
        ),
        "controller_runtime_input_stager": (
            "tools/governed_memory_release/runtime_input_stager.py"
        ),
        "standalone_cpython_inspector": (
            "tools/governed_memory_release/inspect_standalone_cpython.py"
        ),
        "synthetic_proof_entrypoint": (
            "tools/governed_memory_validation/"
            "run_installation_synthetic_proof.py"
        ),
        "disposable_linux_proof_manager_entrypoint": (
            "tools/governed_memory_validation/"
            "execute_phase9_disposable_live_proof_controller.py"
        ),
        "disposable_linux_proof_private_issuer": (
            "tools/governed_memory_validation/"
            "issue_disposable_installation_live_proof.py"
        ),
        "disposable_linux_proof_sealed_runner": (
            "tools/governed_memory_validation/"
            "run_disposable_installation_live_proof.py"
        ),
        "legacy_memory_fallback_allowed": False,
        "stored_assistant_preferences_allowed": False,
        "legacy_memory_prompt_object_allowed": False,
    },
    "sealed_dormant_store_package": {
        "state": (
            "sealed_package_snapshot_before_controller_substrate_publication_"
            "not_installed_not_authorized_not_activated"
        ),
        "server_target": "seebx",
        "install_and_empty_rollback_are_distinct_signed_operations": True,
        "exact_controller_process_is_trusted": True,
        "hostile_same_process_python_isolation_claimed": False,
        "claim_bound_install_controller_composition_packaged": True,
        "claim_bound_empty_rollback_controller_composition_packaged": True,
        "controller_runtime_verification_capability_packaged": True,
        "controller_runtime_release_builder_orchestration_packaged": True,
        "controller_runtime_builder_publication_transport_packaged": True,
        "controller_runtime_publication_policy_transport_packaged": True,
        "runtime_publication_durable_intent_before_first_rename_packaged": True,
        "runtime_publication_post_intent_generic_cleanup_forbidden": True,
        "runtime_publication_crash_prefix_manual_review_fence_packaged": True,
        "runtime_publication_same_device_rename_precondition_packaged": True,
        "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged": True,
        "production_runtime_publication_primitives_packaged": True,
        "approved_standalone_cpython_substrate_digest_bound": True,
        "independent_standalone_cpython_payload_tree_proof_packaged": True,
        "full_controller_release_tree_verification_packaged": True,
        "exact_locked_controller_distribution_set_verification_packaged": True,
        "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt": True,
        "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_marker_and_receipt": True,
        "supervisor_launcher_source_packaged": True,
        "controller_runtime_built_or_installed_at_package_sealing": False,
        "controller_release_staged_at_package_sealing": False,
        "controller_runtime_and_release_required_separate_publication_authority": True,
        "stores_install_owns_or_removes_controller_substrate": False,
        "resolved_store_spec_and_exact_docker_labels_bound": True,
        "resource_identity_ledger_v2_packaged": True,
        "physical_postgresql_qdrant_writer_exclusion_packaged": False,
        "empty_rollback_controller_authority_marker_packaged": True,
        "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence": True,
        "destructive_rollback_steps_atomically_recheck_empty_under_fence": False,
        "closed_live_transport_contracts_packaged": True,
        "complete_closed_live_transport_substrate_set_packaged": True,
        "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": True,
        "driver_native_postgresql_stage_contract_packaged": True,
        "driver_native_postgresql_executable_stage_machine_packaged": True,
        "concrete_psycopg_postgresql_transport_packaged": True,
        "runtime_input_selection_contract_repaired": True,
        "runtime_build_receipt_provenance_v4_packaged": True,
        "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
        "approved_terminal_postgresql_catalog_manifest_selected": True,
        "physical_postgresql_qdrant_writer_exclusion_transport_packaged": False,
        "durable_controller_rollback_marker_transport_packaged": True,
        "external_direct_writer_exclusion_implemented": False,
        "stopped_store_semantic_empty_recheck_is_valid": False,
        "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented": True,
        "retained_audit_artifact_hashes_bound": True,
        "durable_create_once_receipt_store_packaged": True,
        "public_entrypoints_require_canonical_root_owned_production_receipt_store": True,
        "synthetic_receipt_stores_are_private_test_only": True,
        "install_receipt_binds_fresh_terminal_canonical_store_readiness": True,
        "empty_rollback_requires_opaque_verified_install_receipt_and_ledger": True,
        "completed_install_and_empty_rollback_replay_reverification_packaged": True,
        "closed_post_claim_linux_install_adapter_and_factory_packaged": True,
        "fixed_loopback_store_readiness_dto_adapter_packaged": True,
        "physical_ledger_bound_empty_rollback_adapter_packaged": True,
        "concrete_install_store_effect_adapters_packaged": True,
        "concrete_empty_rollback_store_effect_adapters_packaged": True,
        "selected_live_linux_platform_transports_packaged": True,
        "selected_non_postgresql_live_linux_platform_transport_factory_packaged": True,
        "pinned_postgresql_driver_selected_or_packaged": True,
        "activation_entrypoint_packaged": False,
        "validation_scope": (
            "synthetic_in_process_and_separately_authorized_disposable_linux_"
            "proof_pending"
        ),
        "live_linux_execution_proven": False,
        "live_installation_proof_complete": False,
        "installation_performed": False,
        "empty_rollback_performed": False,
        "production_state_changed": False,
    },
    "published_controller_substrate": {
        "state": "published_dormant_not_store_installed_not_activated",
        "server_target": "seebx",
        "receipt": (
            "ops/governed_memory/phase9j_controller_runtime_release_receipt.json"
        ),
        "receipt_sha256": (
            EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256
        ),
        "package_manifest_sha256": EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256,
        "runtime_root": (
            "/opt/governed-memory-controller/runtimes/"
            + EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256
        ),
        "runtime_tree_sha256": EXPECTED_PHASE9J_RUNTIME_TREE_SHA256,
        "release_root": (
            "/opt/governed-memory-controller/releases/"
            + EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256
        ),
        "release_tree_sha256": EXPECTED_PHASE9J_RELEASE_TREE_SHA256,
        "persistent_controller_substrate_created": True,
        "persistent_store_resources_created": False,
        "disposable_live_proof_complete": False,
        "installation_performed": False,
        "empty_rollback_performed": False,
        "production_state_changed": False,
        "activation_authorized": False,
    },
    "historical_only": {
        "documentation_root": "docs/history/governed_memory",
        "operational_metadata_root": "ops/governed_memory/history",
        "may_be_imported_or_executed": False,
        "may_be_used_as_current_release_authority": False,
    },
    "quarantined_legacy": [
        {
            "component": "memory_v1_v5_runtime",
            "path_families": [
                "rag_engine/memory_v1*",
                "rag_engine/governed_memory_provider_v1.py",
                "rag_engine/memory_prompt_*",
            ],
            "current_role": (
                "not_imported_by_current_application_or_response_graph_"
                "pending_separate_retirement_authority"
            ),
        },
        {
            "component": "legacy_preferences_identity_admin_and_vantage",
            "path_families": [
                "rag_engine/assistant_response_preference*",
                "rag_engine/assistant_response_preferences*",
                "rag_engine/admin_memory_*",
                "rag_engine/vantage_*",
            ],
            "current_role": (
                "not_consumed_by_current_successor_pending_precise_retirement"
            ),
        },
        {
            "component": "legacy_live_services_and_store_state",
            "path_families": [
                "legacy_memory_systemd_timers_and_services",
                "legacy_memory_scripts_and_cron",
                "legacy_postgresql_memory_objects",
                "legacy_qdrant_memory_collections",
            ],
            "current_role": "outside_repository_only_candidate_unchanged",
        },
    ],
    "future_deletion_gates": [
        "zero_current_import_and_dynamic_load_closure",
        "legacy_services_timers_cron_and_admin_paths_quiescent",
        (
            "legacy_postgresql_and_qdrant_zero_reader_zero_writer_"
            "observation_window"
        ),
        "rollback_retention_receipt_complete",
        "separate_exact_deletion_batch_authorized",
    ],
    "safety": {
        "controller_substrate_publication_completed": True,
        "controller_substrate_publication_receipt_promoted": True,
        "store_installation_or_rollback_performed": False,
        "disposable_live_proof_performed": False,
        "production_services_changed": False,
        "production_secrets_read_or_changed": False,
        "production_postgresql_read_or_changed": False,
        "production_qdrant_read_or_changed": False,
        "production_data_read": False,
        "provider_calls": 0,
        "structured_lifeswitch_data_in_scope": False,
        "accounts_in_scope": False,
        "activation_authorized": False,
    },
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
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _load_json(path: Path, *, maximum_bytes: int = 256 * 1024) -> object:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReleaseGuardError("release_artifact_read_failed") from error
    if len(raw) > maximum_bytes:
        raise ReleaseGuardError("release_artifact_too_large")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _NonFiniteJsonValue,
    ) as error:
        raise ReleaseGuardError("release_artifact_json_invalid") from error


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ReleaseGuardError("release_artifact_json_invalid") from error
    return hashlib.sha256(payload).hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseGuardError(code)


def _verify_runtime_receipt(
    receipt: object,
    *,
    expected_source_sha256: str,
    expected_python: str,
    expected_python_sha256: str,
    expected_wheel: str,
    expected_wheel_sha256: str,
    error: str,
) -> None:
    _require(
        isinstance(receipt, dict) and set(receipt) == EXPECTED_RUNTIME_RECEIPT_KEYS,
        error,
    )
    assert isinstance(receipt, dict)
    runtime_packages = receipt.get("runtime_packages")
    _require(
        receipt.get("schema_version") == "governed-memory-runtime-build-receipt-v1"
        and receipt.get("source_tree_sha256") == expected_source_sha256
        and receipt.get("candidate_python") == expected_python
        and receipt.get("candidate_python_sha256") == expected_python_sha256
        and receipt.get("candidate_python_is_symlink") is False
        and receipt.get("project_wheel") == expected_wheel
        and receipt.get("project_wheel_sha256") == expected_wheel_sha256
        and receipt.get("runtime_lock")
        == "ops/governed_memory/runtime-requirements.lock"
        and receipt.get("runtime_lock_sha256")
        == EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256
        and receipt.get("build_lock")
        == "ops/governed_memory/build-requirements.lock"
        and receipt.get("build_lock_sha256")
        == EXPECTED_PHASE7C_BUILD_LOCK_SHA256
        and isinstance(runtime_packages, dict)
        and receipt.get("runtime_package_count") == len(runtime_packages)
        and all(
            isinstance(name, str)
            and name == name.lower()
            and isinstance(version, str)
            and bool(version)
            for name, version in runtime_packages.items()
        )
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
        error,
    )


def _verify_historical_phase7c_runtime_receipt(receipt: object) -> None:
    _verify_runtime_receipt(
        receipt,
        expected_source_sha256=EXPECTED_PHASE7C_RUNTIME_SOURCE_SHA256,
        expected_python=EXPECTED_PHASE7C_RUNTIME_PYTHON,
        expected_python_sha256=EXPECTED_PHASE7C_RUNTIME_PYTHON_SHA256,
        expected_wheel=EXPECTED_PHASE7C_RUNTIME_WHEEL,
        expected_wheel_sha256=EXPECTED_PHASE7C_RUNTIME_WHEEL_SHA256,
        error="release_historical_phase7c_runtime_contract_invalid",
    )


def _verify_current_runtime_receipt(receipt: object) -> None:
    _verify_runtime_receipt(
        receipt,
        expected_source_sha256=EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256,
        expected_python=EXPECTED_CURRENT_RUNTIME_PYTHON,
        expected_python_sha256=EXPECTED_CURRENT_RUNTIME_PYTHON_SHA256,
        expected_wheel=EXPECTED_CURRENT_RUNTIME_WHEEL,
        expected_wheel_sha256=EXPECTED_CURRENT_RUNTIME_WHEEL_SHA256,
        error="release_current_runtime_contract_invalid",
    )


def _verify_phase9j_controller_runtime_release_receipt(receipt: object) -> None:
    error = "release_phase9j_controller_runtime_release_receipt_invalid"
    _require(
        isinstance(receipt, dict)
        and _sha256(CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT)
        == EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256
        and receipt.get("schema_version")
        == "governed-memory-controller-runtime-build-receipt-v4"
        and receipt.get("result") == "isolated_controller_runtime_built_and_closed"
        and receipt.get("package_manifest_sha256")
        == EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256
        and receipt.get("runtime_tree_sha256")
        == EXPECTED_PHASE9J_RUNTIME_TREE_SHA256
        and receipt.get("release_tree_sha256")
        == EXPECTED_PHASE9J_RELEASE_TREE_SHA256
        and receipt.get("persistent_controller_substrate_created") is True
        and receipt.get("persistent_store_resources_created") is False
        and receipt.get("active_production_state_changed") is False
        and receipt.get("production_data_read") is False
        and receipt.get("network_calls") == 0
        and receipt.get("provider_calls") == 0,
        error,
    )


def _verify_historical_phase7c_application_proof(value: object) -> None:
    error = "release_phase7c_application_proof_invalid"
    wrapper_keys = {
        "schema_version",
        "phase",
        "attested_pre_promotion_runtime_manifest_sha256",
        "proof_log_sha256",
        "proof_receipt_canonicalization",
        "proof_receipt_canonical_sha256",
        "proof_receipt",
        "http_vertical_slice_receipt",
        "deletion_receipt",
        "deletion_resilience_receipt",
    }
    if not isinstance(value, dict) or set(value) != wrapper_keys:
        raise ReleaseGuardError(error)
    proof = value.get("proof_receipt")
    http = value.get("http_vertical_slice_receipt")
    deletion = value.get("deletion_receipt")
    resilience = value.get("deletion_resilience_receipt")
    if not all(isinstance(item, dict) for item in (proof, http, deletion, resilience)):
        raise ReleaseGuardError(error)
    assert isinstance(proof, dict)
    assert isinstance(http, dict)
    assert isinstance(deletion, dict)
    assert isinstance(resilience, dict)
    http_sha256 = _canonical_json_sha256(http)
    deletion_sha256 = _canonical_json_sha256(deletion)
    resilience_sha256 = _canonical_json_sha256(resilience)
    _require(
        value.get("schema_version") == "governed-memory-current-disposable-proof-v2"
        and value.get("phase") == "phase7c"
        and value.get("attested_pre_promotion_runtime_manifest_sha256")
        == EXPECTED_PHASE7C_ATTESTED_RUNTIME_MANIFEST_SHA256
        and value.get("proof_log_sha256") == EXPECTED_PHASE7C_PROOF_LOG_SHA256
        and value.get("proof_receipt_canonicalization")
        == "utf8_json_sorted_keys_compact_no_newline_v1"
        and value.get("proof_receipt_canonical_sha256")
        == EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256
        and _canonical_json_sha256(proof)
        == EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256
        and http_sha256 == EXPECTED_PHASE7C_HTTP_RECEIPT_SHA256
        and deletion_sha256 == EXPECTED_PHASE7C_DELETION_RECEIPT_SHA256
        and resilience_sha256 == EXPECTED_PHASE7C_RESILIENCE_RECEIPT_SHA256
        and proof.get("integration_receipt_sha256") == http_sha256
        and proof.get("deletion_receipt_sha256") == deletion_sha256
        and proof.get("deletion_resilience_receipt_sha256") == resilience_sha256
        and proof.get("schema_version")
        == "governed-memory-successor-disposable-run-v6"
        and proof.get("result") == "passed"
        and proof.get("source_tree_sha256")
        == EXPECTED_PHASE7C_RUNTIME_SOURCE_SHA256
        and proof.get("runtime_build_receipt_sha256")
        == EXPECTED_PHASE7C_RUNTIME_RECEIPT_SHA256
        and proof.get("manifest_sha256")
        == EXPECTED_PHASE7C_ATTESTED_MIGRATION_MANIFEST_SHA256
        and proof.get("runtime_packages_sha256")
        == EXPECTED_PHASE7C_RUNTIME_PACKAGES_SHA256
        and proof.get("runtime_lock_sha256")
        == EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256
        and proof.get("candidate_unchanged") is True
        and proof.get("production_data_read") is False
        and proof.get("production_endpoint_calls") == 0
        and proof.get("production_service_invoked") is False
        and proof.get("provider_external_calls") == 0
        and proof.get("external_network_calls") == 0
        and proof.get("resources_removed") is True
        and proof.get("ports_released") is True
        and proof.get("rollback_reapply") == "passed"
        and http.get("production_data_read") is False
        and http.get("production_endpoint_calls") == 0
        and http.get("production_service_invoked") is False
        and http.get("provider_external_calls") == 0
        and deletion.get("production_data_read") is False
        and deletion.get("production_endpoint_calls") == 0
        and deletion.get("production_service_invoked") is False
        and deletion.get("provider_external_calls") == 0
        and deletion.get("lifeswitch_snapshot_bytes") == 2765
        and deletion.get("lifeswitch_snapshot_sha256")
        == "6fc270d38b681af35d1db008d04ff65fd790a2e44bef5791a23119ab99fcddae"
        and resilience.get("production_data_read") is False
        and resilience.get("production_endpoint_calls") == 0
        and resilience.get("provider_external_calls") == 0,
        error,
    )


def _verify_current_phase8g_application_proof(value: object) -> None:
    error = "release_current_phase8g_application_proof_invalid"
    wrapper_keys = {
        "schema_version",
        "phase",
        "attested_pre_promotion_runtime_manifest_sha256",
        "proof_log_sha256",
        "proof_receipt_canonicalization",
        "proof_receipt_canonical_sha256",
        "proof_receipt",
        "http_vertical_slice_receipt",
        "deletion_receipt",
        "deletion_resilience_receipt",
    }
    if not isinstance(value, dict) or set(value) != wrapper_keys:
        raise ReleaseGuardError(error)
    proof = value.get("proof_receipt")
    http = value.get("http_vertical_slice_receipt")
    deletion = value.get("deletion_receipt")
    resilience = value.get("deletion_resilience_receipt")
    if not all(isinstance(item, dict) for item in (proof, http, deletion, resilience)):
        raise ReleaseGuardError(error)
    assert isinstance(proof, dict)
    assert isinstance(http, dict)
    assert isinstance(deletion, dict)
    assert isinstance(resilience, dict)
    http_sha256 = _canonical_json_sha256(http)
    deletion_sha256 = _canonical_json_sha256(deletion)
    resilience_sha256 = _canonical_json_sha256(resilience)
    _require(
        value.get("schema_version") == "governed-memory-current-disposable-proof-v3"
        and value.get("phase") == "phase8g"
        and value.get("attested_pre_promotion_runtime_manifest_sha256")
        == EXPECTED_PHASE8G_ATTESTED_RUNTIME_MANIFEST_SHA256
        and value.get("proof_log_sha256") == EXPECTED_PHASE8G_PROOF_LOG_SHA256
        and value.get("proof_receipt_canonicalization")
        == "utf8_json_sorted_keys_compact_no_newline_v1"
        and value.get("proof_receipt_canonical_sha256")
        == EXPECTED_PHASE8G_PROOF_CANONICAL_SHA256
        and _canonical_json_sha256(proof)
        == EXPECTED_PHASE8G_PROOF_CANONICAL_SHA256
        and http_sha256 == EXPECTED_PHASE8G_HTTP_RECEIPT_SHA256
        and deletion_sha256 == EXPECTED_PHASE8G_DELETION_RECEIPT_SHA256
        and resilience_sha256 == EXPECTED_PHASE8G_RESILIENCE_RECEIPT_SHA256
        and proof.get("integration_receipt_sha256") == http_sha256
        and proof.get("deletion_receipt_sha256") == deletion_sha256
        and proof.get("deletion_resilience_receipt_sha256") == resilience_sha256
        and proof.get("schema_version")
        == "governed-memory-successor-disposable-run-v7"
        and proof.get("branch") == EXPECTED_PHASE8G_CANDIDATE_BRANCH
        and proof.get("candidate_head") == EXPECTED_PHASE8G_CANDIDATE_HEAD
        and proof.get("candidate_tree") == EXPECTED_PHASE8G_CANDIDATE_TREE
        and proof.get("result") == "passed"
        and proof.get("source_tree_sha256")
        == EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256
        and proof.get("runtime_build_receipt_sha256")
        == EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256
        and proof.get("manifest_sha256")
        == EXPECTED_PHASE8G_ATTESTED_MIGRATION_MANIFEST_SHA256
        and proof.get("runtime_packages_sha256")
        == EXPECTED_PHASE7C_RUNTIME_PACKAGES_SHA256
        and proof.get("runtime_lock_sha256")
        == EXPECTED_PHASE7C_RUNTIME_LOCK_SHA256
        and proof.get("postgres_image_digest")
        == EXPECTED_PHASE8G_POSTGRES_IMAGE_DIGEST
        and proof.get("qdrant_image_digest")
        == "qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
        and proof.get("candidate_unchanged") is True
        and proof.get("production_data_read") is False
        and proof.get("production_endpoint_calls") == 0
        and proof.get("production_service_invoked") is False
        and proof.get("provider_external_calls") == 0
        and proof.get("external_network_calls") == 0
        and proof.get("published_container_ports") is False
        and proof.get("docker_persistent_mounts") is False
        and proof.get("resources_removed") is True
        and proof.get("ports_released") is True
        and proof.get("rollback_reapply") == "passed"
        and http.get("production_data_read") is False
        and http.get("production_endpoint_calls") == 0
        and http.get("production_service_invoked") is False
        and http.get("provider_external_calls") == 0
        and http.get("synthetic_jwt_only") is True
        and deletion.get("production_data_read") is False
        and deletion.get("production_endpoint_calls") == 0
        and deletion.get("production_service_invoked") is False
        and deletion.get("provider_external_calls") == 0
        and deletion.get("lifeswitch_snapshot_bytes") == 2765
        and deletion.get("lifeswitch_snapshot_sha256")
        == "6fc270d38b681af35d1db008d04ff65fd790a2e44bef5791a23119ab99fcddae"
        and deletion.get("transient_targets_purged") is True
        and deletion.get("qdrant_target_absent_alias_and_physical") is True
        and resilience.get("production_data_read") is False
        and resilience.get("production_endpoint_calls") == 0
        and resilience.get("provider_external_calls") == 0
        and resilience.get("final_absence_verified") is True
        and resilience.get("post_completion_no_work") is True,
        error,
    )


def _verify_chat_only_scope(value: Mapping[str, object]) -> None:
    _require(
        value.get("source_erasure_selectors")
        == ["thread", "message_tail", "recent", "all_conversations"]
        and value.get("source_erasure_direct_delete_roots")
        == ["public.chat_log", "public.chat_attachments", "public.threads"]
        and value.get(
            "source_erasure_memory_only_or_account_wide_memory_selector_allowed"
        )
        is False
        and value.get(
            "source_erasure_structured_lifeswitch_data_or_accounts_deleted"
        )
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


def _verify_current_component_disposition(value: object) -> None:
    _require(
        type(value) is dict
        and _canonical_json_sha256(value)
        == _canonical_json_sha256(EXPECTED_CURRENT_COMPONENT_DISPOSITION),
        "release_component_disposition_invalid",
    )


def _verify_current_package_receipts(
    package_receipt: object,
    store_receipt: object,
) -> tuple[dict[str, object], dict[str, object]]:
    _require(
        isinstance(package_receipt, dict) and isinstance(store_receipt, dict),
        "release_current_store_package_invalid",
    )
    assert isinstance(package_receipt, dict) and isinstance(store_receipt, dict)
    package_artifacts = package_receipt.get("artifact_sha256")
    store_artifacts = store_receipt.get("artifact_sha256")
    _require(
        package_receipt.get("schema_version")
        == "governed-memory-dormant-store-install-package-verification-v6"
        and isinstance(package_artifacts, dict)
        and bool(package_artifacts)
        and package_receipt.get("artifact_count") == len(package_artifacts)
        and package_receipt.get("guarded_synthetic_proof_harness_packaged") is True
        and package_receipt.get("synthetic_proof_executed_by_verifier") is False
        and package_receipt.get("synthetic_proof_receipt_present_at_package_sealing")
        is False
        and package_receipt.get("proof_receipt_is_external_to_package") is True
        and package_receipt.get(
            "authority_gated_disposable_linux_proof_runner_packaged"
        )
        is True
        and package_receipt.get("disposable_linux_proof_runner_executed_by_verifier")
        is False
        and package_receipt.get(
            "disposable_linux_proof_receipt_present_at_package_sealing"
        )
        is False
        and package_receipt.get("claim_bound_install_controller_composition_packaged") is True
        and package_receipt.get("claim_bound_empty_rollback_controller_composition_packaged") is True
        and package_receipt.get("controller_runtime_verification_capability_packaged") is True
        and package_receipt.get("full_controller_release_tree_verification_packaged") is True
        and package_receipt.get("exact_locked_controller_distribution_set_verification_packaged") is True
        and package_receipt.get(
            "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt"
        )
        is True
        and package_receipt.get(
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_authority_marker_and_receipt"
        )
        is True
        and package_receipt.get("supervisor_launcher_source_packaged") is True
        and package_receipt.get("controller_runtime_built_or_installed") is False
        and package_receipt.get("controller_release_staged") is False
        and package_receipt.get(
            "controller_runtime_and_release_require_separate_future_build_and_install_authority"
        )
        is True
        and package_receipt.get("stores_install_owns_or_removes_controller_substrate") is False
        and package_receipt.get(
            "resolved_store_spec_and_exact_docker_labels_bound"
        )
        is True
        and package_receipt.get("resource_identity_ledger_v2_packaged") is True
        and package_receipt.get(
            "empty_rollback_controller_authority_marker_packaged"
        )
        is True
        and package_receipt.get(
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence"
        )
        is True
        and package_receipt.get(
            "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held"
        )
        is True
        and package_receipt.get("closed_live_transport_contracts_packaged") is True
        and package_receipt.get("complete_closed_live_transport_substrate_set_packaged") is True
        and package_receipt.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is True
        and package_receipt.get(
            "driver_native_postgresql_stage_contract_packaged"
        )
        is True
        and package_receipt.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is True
        and package_receipt.get(
            "concrete_psycopg_postgresql_transport_packaged"
        )
        is True
        and package_receipt.get("runtime_input_selection_contract_repaired") is True
        and package_receipt.get("runtime_build_receipt_provenance_v4_packaged") is True
        and package_receipt.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is True
        and package_receipt.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is True
        and package_receipt.get(
            "durable_live_empty_rollback_controller_authority_marker_transport_packaged"
        )
        is True
        and package_receipt.get(
            "administrative_cooperative_writer_fence_implemented"
        )
        is True
        and package_receipt.get("equivalent_privileged_root_bypass_excluded")
        is False
        and package_receipt.get("stopped_store_semantic_empty_recheck_is_valid")
        is False
        and package_receipt.get(
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented"
        )
        is True
        and package_receipt.get("retained_audit_artifact_hashes_bound") is True
        and package_receipt.get("install_controller_emits_canonical_receipt") is True
        and package_receipt.get("empty_rollback_controller_emits_canonical_receipt") is True
        and package_receipt.get(
            "install_receipt_binds_fresh_terminal_canonical_store_readiness"
        )
        is True
        and package_receipt.get(
            "empty_rollback_requires_opaque_verified_install_receipt_and_ledger"
        )
        is True
        and package_receipt.get(
            "completed_install_and_empty_rollback_replay_reverification_packaged"
        )
        is True
        and package_receipt.get("closed_install_store_effect_adapter_packaged") is True
        and package_receipt.get("closed_empty_rollback_store_effect_adapter_packaged") is True
        and package_receipt.get("selected_live_platform_transports_packaged") is True
        and package_receipt.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is True
        and package_receipt.get("pinned_postgresql_driver_selected") is True
        and package_receipt.get("durable_create_once_receipt_store_packaged") is True
        and package_receipt.get(
            "controller_runtime_release_builder_orchestration_packaged"
        )
        is True
        and package_receipt.get("controller_runtime_build_transport_packaged")
        is True
        and package_receipt.get(
            "controller_runtime_publication_policy_transport_packaged"
        )
        is True
        and package_receipt.get(
            "runtime_publication_durable_intent_before_first_rename_packaged"
        )
        is True
        and package_receipt.get(
            "runtime_publication_post_intent_generic_cleanup_forbidden"
        )
        is True
        and package_receipt.get(
            "runtime_publication_crash_prefix_manual_review_fence_packaged"
        )
        is True
        and package_receipt.get(
            "runtime_publication_same_device_rename_precondition_packaged"
        )
        is True
        and package_receipt.get(
            "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged"
        )
        is True
        and package_receipt.get(
            "production_runtime_publication_primitives_packaged"
        )
        is True
        and package_receipt.get(
            "independent_standalone_cpython_payload_tree_proof_packaged"
        )
        is True
        and package_receipt.get("activation_entrypoint_packaged") is False
        and package_receipt.get("installation_performed_by_verifier") is False
        and package_receipt.get("images_staged_by_verifier") is False
        and package_receipt.get("secrets_touched_by_verifier") is False
        and package_receipt.get("activation_performed_by_verifier") is False
        and store_receipt.get("schema_version")
        == "governed-memory-dormant-store-install-store-migration-verification-v3"
        and isinstance(store_artifacts, dict)
        and bool(store_artifacts)
        and store_receipt.get("file_count") == len(store_artifacts)
        and store_receipt.get("source_bridge_artifact_count") == 0
        and store_receipt.get("historical_package_descriptor_count") == 0
        and store_receipt.get("production_state_changed") is False
        and package_receipt.get("migration_manifest_sha256")
        == store_receipt.get("manifest_sha256"),
        "release_current_store_package_invalid",
    )
    return package_receipt, store_receipt


def _checked_activation_blockers(activation: object) -> list[str]:
    error = "release_runtime_manifest_invalid"
    _require(isinstance(activation, dict), error)
    assert isinstance(activation, dict)
    blockers = activation.get("blockers")
    _require(
        isinstance(blockers, list)
        and bool(blockers)
        and all(isinstance(item, str) and bool(item) for item in blockers)
        and len(blockers) == len(set(blockers))
        and len(blockers) == EXPECTED_ACTIVATION_BLOCKER_COUNT
        and REQUIRED_ACTIVATION_BLOCKERS.issubset(blockers),
        error,
    )
    return blockers


def _verify_runtime_manifest(
    runtime: object,
    package_receipt: Mapping[str, object],
    store_receipt: Mapping[str, object],
) -> list[str]:
    error = "release_runtime_manifest_invalid"
    expected_top_level = {
        "schema_version",
        "phase",
        "python_runtime",
        "validation_runtime",
        "controller_runtime_publication",
        "authority",
        "infrastructure",
        "http_runtime",
        "activation",
        "release_guard",
        "ingestion",
        "worker_adapters",
        "provider_policy",
        "disposable_validation",
        "inactive_store_package",
        "calibration",
        "frontend_candidate",
        "historical_evidence",
        "owner_routes",
        "prohibited_routes",
        "legacy_imports_allowed",
        "production_state_changed",
    }
    _require(
        isinstance(runtime, dict) and set(runtime) == expected_top_level,
        error,
    )
    assert isinstance(runtime, dict)
    validation = runtime.get("validation_runtime")
    publication = runtime.get("controller_runtime_publication")
    authority_metadata = runtime.get("authority")
    disposable = runtime.get("disposable_validation")
    current = runtime.get("inactive_store_package")
    activation = runtime.get("activation")
    http = runtime.get("http_runtime")
    infrastructure = runtime.get("infrastructure")
    release = runtime.get("release_guard")
    ingestion = runtime.get("ingestion")
    frontend = runtime.get("frontend_candidate")
    history = runtime.get("historical_evidence")
    phase8g_authority = (
        authority_metadata.get("phase8g_application_validation_snapshot")
        if isinstance(authority_metadata, dict)
        else None
    )
    phase9j_target = (
        authority_metadata.get("phase9j_inactive_store_target")
        if isinstance(authority_metadata, dict)
        else None
    )
    blockers = _checked_activation_blockers(activation)
    assert isinstance(activation, dict)

    current_state = current.get("state") if isinstance(current, dict) else None
    _require(
        runtime.get("schema_version")
        == "governed-memory-successor-runtime-manifest-v4"
        and runtime.get("phase")
        == (
            "phase9j_install_ready_closed_runtime_and_store_transports_"
            "packaged_inactive_activation_blocked"
        )
        and runtime.get("production_state_changed") is False
        and runtime.get("legacy_imports_allowed") is False
        and isinstance(authority_metadata, dict)
        and set(authority_metadata)
        == {
            "phase8g_application_validation_snapshot",
            "phase9j_inactive_store_target",
        }
        and isinstance(phase8g_authority, dict)
        and phase8g_authority.get("evidence_role")
        == (
            "historical_disposable_application_validation_not_current_store_"
            "or_routing_authority"
        )
        and phase8g_authority.get("database_target") == "127.0.0.1:55432"
        and phase8g_authority.get("qdrant_target") == "127.0.0.1:6343"
        and phase8g_authority.get("qdrant_collection")
        == "governed_memory_9a54cf123493_000001"
        and phase8g_authority.get("conversation_database") == "memory"
        and phase8g_authority.get("conversation_database_target")
        == "127.0.0.1:5432"
        and isinstance(phase9j_target, dict)
        and set(phase9j_target)
        == {
            "source_contract",
            "source_store_spec",
            "candidate_id",
            "database",
            "database_target",
            "qdrant_target",
            "qdrant_collection",
            "qdrant_alias",
            "store_installation_performed",
            "current_route_installed",
            "activation_authorized",
        }
        and phase9j_target.get("source_contract")
        == "ops/governed_memory/installation/current/contract.json"
        and phase9j_target.get("source_store_spec")
        == "ops/governed_memory/installation/store_spec-v3.json"
        and phase9j_target.get("candidate_id")
        == "governed_memory_9a54cf123493_000003"
        and phase9j_target.get("database") == "governed_memory"
        and phase9j_target.get("database_target") == "127.0.0.1:55434"
        and phase9j_target.get("qdrant_target") == "127.0.0.1:6345"
        and phase9j_target.get("qdrant_collection")
        == "governed_memory_9a54cf123493_000003"
        and phase9j_target.get("qdrant_alias") == "governed_memory_active"
        and phase9j_target.get("store_installation_performed") is False
        and phase9j_target.get("current_route_installed") is False
        and phase9j_target.get("activation_authorized") is False
        and isinstance(validation, dict)
        and validation.get("runtime_lock")
        == "ops/governed_memory/runtime-requirements.lock"
        and validation.get("runtime_lock_sha256") == _sha256(RUNTIME_LOCK)
        and validation.get("build_lock")
        == "ops/governed_memory/build-requirements.lock"
        and validation.get("build_lock_sha256") == _sha256(BUILD_LOCK)
        and validation.get("current_source_tree_sha256")
        == EXPECTED_CURRENT_RUNTIME_SOURCE_SHA256
        and validation.get("current_candidate_python")
        == EXPECTED_CURRENT_RUNTIME_PYTHON
        and validation.get("current_candidate_python_sha256")
        == EXPECTED_CURRENT_RUNTIME_PYTHON_SHA256
        and validation.get("current_project_wheel_sha256")
        == EXPECTED_CURRENT_RUNTIME_WHEEL_SHA256
        and validation.get("current_source_bound") is True
        and validation.get("current_runtime_rebuild_pending") is False
        and validation.get("current_build_receipt")
        == "ops/governed_memory/runtime_build_receipt.json"
        and validation.get("current_build_receipt_sha256")
        == EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256
        and validation.get("current_build_receipt_present") is True
        and validation.get("historical_phase7c_build_receipt")
        == "ops/governed_memory/history/phase7c/runtime_build_receipt.json"
        and validation.get("historical_phase7c_build_receipt_sha256")
        == EXPECTED_PHASE7C_RUNTIME_RECEIPT_SHA256
        and validation.get("historical_phase7c_build_receipt_present") is True
        and validation.get(
            "historical_phase7c_build_receipt_reusable_for_current_candidate"
        )
        is False
        and isinstance(publication, dict)
        and set(publication)
        == {
            "state",
            "server_target",
            "receipt",
            "receipt_sha256",
            "package_manifest_sha256",
            "runtime_root",
            "runtime_tree_sha256",
            "release_root",
            "release_tree_sha256",
            "persistent_controller_substrate_created",
            "persistent_store_resources_created",
            "active_production_state_changed",
            "disposable_live_proof_complete",
            "production_installation_authorized",
            "activation_authorized",
            "current_blockers",
        }
        and publication.get("state")
        == "published_dormant_controller_substrate_only"
        and publication.get("server_target") == "seebx"
        and publication.get("receipt")
        == "ops/governed_memory/phase9j_controller_runtime_release_receipt.json"
        and publication.get("receipt_sha256")
        == EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256
        and publication.get("package_manifest_sha256")
        == EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256
        and publication.get("package_manifest_sha256")
        == package_receipt.get("package_manifest_sha256")
        and publication.get("runtime_root")
        == (
            "/opt/governed-memory-controller/runtimes/"
            + EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256
        )
        and publication.get("runtime_tree_sha256")
        == EXPECTED_PHASE9J_RUNTIME_TREE_SHA256
        and publication.get("release_root")
        == (
            "/opt/governed-memory-controller/releases/"
            + EXPECTED_PHASE9J_PACKAGE_MANIFEST_SHA256
        )
        and publication.get("release_tree_sha256")
        == EXPECTED_PHASE9J_RELEASE_TREE_SHA256
        and publication.get("persistent_controller_substrate_created") is True
        and publication.get("persistent_store_resources_created") is False
        and publication.get("active_production_state_changed") is False
        and publication.get("disposable_live_proof_complete") is False
        and publication.get("production_installation_authorized") is False
        and publication.get("activation_authorized") is False
        and publication.get("current_blockers")
        == [
            "external_phase9j_disposable_live_proof_receipt_absent",
            "production_installation_not_authorized",
            "production_activation_not_authorized",
        ]
        and isinstance(disposable, dict)
        and disposable.get("scope")
        == "phase8g_current_candidate_successor_disposable_only"
        and disposable.get("evidence_role")
        == (
            "current_candidate_application_and_deletion_proof_not_"
            "installation_or_live_proof"
        )
        and disposable.get("evidence_status")
        == (
            "phase8g_current_candidate_disposable_validation_passed_inactive_"
            "not_production_activation"
        )
        and disposable.get("current_proof_complete") is True
        and disposable.get("reusable_as_current_store_installation_proof") is False
        and disposable.get("reusable_as_live_proof") is False
        and disposable.get("disposable_revalidation_required") is False
        and disposable.get("runner_path")
        == "tools/governed_memory_validation/run_disposable_successor.sh"
        and disposable.get("runner_sha256") == _sha256(DISPOSABLE_RUNNER)
        and disposable.get("runner_sealed") is True
        and disposable.get("proof_execution_runner_sha256")
        == _sha256(DISPOSABLE_RUNNER)
        and disposable.get("current_runner_execution_attested_by_phase8g_proof")
        is True
        and disposable.get("current_runner_post_proof_change_scope") == "none"
        and disposable.get("current_runner_proof_execution_semantics_changed")
        is False
        and disposable.get("current_candidate_python")
        == EXPECTED_CURRENT_RUNTIME_PYTHON
        and disposable.get("current_proof_receipt")
        == "ops/governed_memory/phase8g_disposable_proof_receipt.json"
        and disposable.get("current_proof_receipt_sha256")
        == EXPECTED_PHASE8G_PROOF_RECEIPT_SHA256
        and disposable.get("attested_pre_promotion_migration_manifest_sha256")
        == EXPECTED_PHASE8G_ATTESTED_MIGRATION_MANIFEST_SHA256
        and disposable.get("current_promoted_migration_manifest_sha256")
        == EXPECTED_PHASE8G_PROMOTED_MIGRATION_MANIFEST_SHA256
        and disposable.get("postgresql_fresh_empty") is True
        and disposable.get("qdrant_fresh_empty") is True
        and disposable.get("migration_forward_rollback_reapply") is True
        and disposable.get("normalized_catalog_equivalent_after_reapply") is True
        and disposable.get("deletion_coordination_disposable_proof_complete")
        is True
        and disposable.get("production_routes_installed") is False
        and disposable.get("authenticated_frontend_verified") is False
        and disposable.get("production_data_read") is False
        and disposable.get("production_endpoint_calls") == 0
        and disposable.get("provider_external_calls") == 0
        and disposable.get("persistent_resources_created") is False
        and disposable.get("final_resources_absent") is True
        and disposable.get("resource_cleanup_complete") is True
        and isinstance(current, dict)
        and current.get("scope") == "current_inactive_stores_only_package"
        and current_state
        == (
            "phase9j_install_ready_closed_runtime_and_store_transports_"
            "packaged_not_installed_not_activated"
        )
        and current.get("package_manifest")
        == "ops/governed_memory/installation/current/package_manifest.json"
        and current.get("package_manifest_schema_version")
        == "governed-memory-dormant-store-install-inactive-execution-package-manifest-v6"
        and current.get("package_manifest_sha256")
        == package_receipt.get("package_manifest_sha256")
        and current.get("package_artifact_count")
        == package_receipt.get("artifact_count")
        and current.get("contract_canonical_sha256")
        == package_receipt.get("contract_canonical_sha256")
        and current.get("contract")
        == "ops/governed_memory/installation/current/contract.json"
        and current.get("controller_plan_canonical_sha256")
        == package_receipt.get("plan_canonical_sha256")
        and current.get("controller_plan")
        == "ops/governed_memory/installation/current/controller_plan.json"
        and current.get("execution_contract_canonical_sha256")
        == package_receipt.get("execution_contract_canonical_sha256")
        and current.get("execution_contract")
        == "ops/governed_memory/installation/current/execution_contract.json"
        and current.get("controller_runtime_contract_canonical_sha256")
        == package_receipt.get("controller_runtime_contract_canonical_sha256")
        and current.get("controller_runtime_contract")
        == "ops/governed_memory/installation/current/controller_runtime_contract.json"
        and current.get("postgres_native_stage_contract_canonical_sha256")
        == package_receipt.get("postgres_native_stage_contract_canonical_sha256")
        and current.get("postgres_native_stage_contract")
        == "ops/governed_memory/installation/current/postgres/native_stage_contract.json"
        and current.get("store_migration_manifest_sha256")
        == store_receipt.get("manifest_sha256")
        and current.get("store_migration_manifest")
        == "ops/governed_memory/installation/current/migration_manifest.json"
        and current.get("store_migration_file_count")
        == store_receipt.get("file_count")
        and current.get("static_package_verification_complete") is True
        and current.get("historical_dormant_store_install_static_package_verification_complete")
        is True
        and current.get("synthetic_proof_harness_packaged") is True
        and current.get("synthetic_proof_executed_for_current_package") is False
        and current.get("historical_dormant_store_install_synthetic_proof_executed") is True
        and current.get("synthetic_proof_outcome")
        == "in_process_test_evidence_only_not_promoted_current_package_proof"
        and current.get("synthetic_proof_executed_by_release_guard") is False
        and current.get("synthetic_proof_receipt_promoted") is False
        and current.get("live_installation_proof_complete") is False
        and current.get("installation_executor_packaged") is True
        and current.get("rollback_executor_packaged") is True
        and current.get("controller_runtime_verification_capability_packaged")
        is True
        and current.get("full_controller_release_tree_verification_packaged")
        is True
        and current.get(
            "exact_locked_controller_distribution_set_verification_packaged"
        )
        is True
        and current.get(
            "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt"
        )
        is True
        and current.get(
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_marker_and_receipt"
        )
        is True
        and current.get("supervisor_launcher_source_packaged") is True
        and current.get("controller_runtime_built_or_installed") is False
        and current.get("controller_release_staged") is False
        and current.get(
            "controller_runtime_and_release_require_separate_future_build_and_install_authority"
        )
        is True
        and current.get("stores_install_owns_or_removes_controller_substrate")
        is False
        and current.get("resolved_store_spec_and_exact_docker_labels_bound")
        is True
        and current.get("resource_identity_ledger_v2_packaged") is True
        and current.get("physical_postgresql_qdrant_writer_exclusion_packaged")
        is False
        and current.get(
            "empty_rollback_controller_authority_marker_packaged"
        )
        is True
        and current.get(
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence"
        )
        is True
        and current.get(
            "destructive_rollback_steps_atomically_recheck_empty_under_fence"
        )
        is False
        and current.get("closed_live_transport_contracts_packaged") is True
        and current.get("complete_closed_live_transport_substrate_set_packaged") is True
        and current.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is True
        and current.get("driver_native_postgresql_stage_contract_packaged") is True
        and current.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is True
        and current.get("concrete_psycopg_postgresql_transport_packaged")
        is True
        and current.get("runtime_input_selection_contract_repaired") is True
        and current.get("runtime_build_receipt_provenance_v4_packaged") is True
        and current.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is True
        and current.get("approved_terminal_postgresql_catalog_manifest_selected")
        is True
        and current.get(
            "physical_postgresql_qdrant_writer_exclusion_transport_packaged"
        )
        is False
        and current.get("durable_controller_rollback_marker_transport_packaged")
        is True
        and current.get("external_direct_writer_exclusion_implemented") is False
        and current.get("stopped_store_semantic_empty_recheck_is_valid") is False
        and current.get(
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented"
        )
        is True
        and current.get("retained_audit_artifact_hashes_bound") is True
        and current.get("durable_create_once_receipt_store_packaged") is True
        and current.get(
            "public_entrypoints_require_canonical_root_owned_production_receipt_store"
        )
        is True
        and current.get("synthetic_receipt_stores_are_private_test_only") is True
        and current.get("closed_post_claim_linux_install_adapter_and_factory_packaged")
        is True
        and current.get("fixed_loopback_store_readiness_dto_adapter_packaged")
        is True
        and current.get("physical_ledger_bound_empty_rollback_adapter_packaged")
        is True
        and current.get("concrete_install_store_effect_adapters_packaged") is True
        and current.get("concrete_empty_rollback_store_effect_adapters_packaged") is True
        and current.get("selected_live_linux_platform_transports_packaged") is True
        and current.get(
            "selected_non_postgresql_live_linux_platform_transport_factory_packaged"
        )
        is True
        and current.get("pinned_postgresql_driver_selected_or_packaged") is True
        and current.get(
            "controller_runtime_release_builder_orchestration_packaged"
        )
        is True
        and current.get("controller_runtime_builder_publication_transport_packaged")
        is True
        and current.get(
            "controller_runtime_publication_policy_transport_packaged"
        )
        is True
        and current.get(
            "runtime_publication_durable_intent_before_first_rename_packaged"
        )
        is True
        and current.get(
            "runtime_publication_post_intent_generic_cleanup_forbidden"
        )
        is True
        and current.get(
            "runtime_publication_crash_prefix_manual_review_fence_packaged"
        )
        is True
        and current.get(
            "runtime_publication_same_device_rename_precondition_packaged"
        )
        is True
        and current.get(
            "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged"
        )
        is True
        and current.get("production_runtime_publication_primitives_packaged")
        is True
        and current.get("approved_standalone_cpython_substrate_digest_bound")
        is True
        and current.get(
            "independent_standalone_cpython_payload_tree_proof_packaged"
        )
        is True
        and current.get("install_controller_emits_canonical_receipt") is True
        and current.get("empty_rollback_controller_emits_canonical_receipt") is True
        and current.get(
            "install_receipt_binds_fresh_terminal_canonical_store_readiness"
        )
        is True
        and current.get(
            "empty_rollback_requires_opaque_verified_install_receipt_and_ledger"
        )
        is True
        and current.get(
            "completed_install_and_empty_rollback_replay_reverification_packaged"
        )
        is True
        and current.get("activation_executor_packaged") is False
        and current.get("installation_performed") is False
        and current.get("live_installation_state_reverified_for_current_candidate") is False
        and current.get("installation_authorized") is False
        and current.get("activation_authorized") is False
        and current.get("production_state_changed") is False
        and activation.get("production_authorized") is False
        and activation.get("retained_phase7a_snapshot_installed_services") == []
        and activation.get("retained_phase7a_snapshot_running_services") == []
        and activation.get("retained_phase7a_snapshot_enabled_services") == []
        and activation.get("retained_phase7a_snapshot_installed_timers") == []
        and activation.get("retained_phase7a_snapshot_enabled_timers") == []
        and activation.get("phase8d_live_successor_service_state_reverified")
        is False
        and isinstance(http, dict)
        and http.get("default_mode") == "off"
        and http.get("transport") == "permissioned_unix_socket"
        and http.get("unix_socket_path") == "/run/governed-memory/http.sock"
        and http.get("tcp_listener_allowed") is False
        and http.get("conversation_erasure_route_installed") is False
        and http.get("conversation_erasure_route_routed") is False
        and http.get("conversation_erasure_route_live_verified") is False
        and http.get("conversation_erasure_route_current_disposable_integration_verified")
        is True
        and http.get("brains_erasure_proxy_matching_service_token_provisioned")
        is False
        and http.get("supabase_auth_sessions_rpc_live_verified") is False
        and isinstance(infrastructure, dict)
        and infrastructure.get("persistent_composition_packaged") is False
        and infrastructure.get("closed_live_transport_contracts_packaged")
        is True
        and infrastructure.get(
            "complete_closed_live_transport_substrate_set_packaged"
        )
        is True
        and infrastructure.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is True
        and infrastructure.get("driver_native_postgresql_stage_contract_packaged")
        is True
        and infrastructure.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is True
        and infrastructure.get("concrete_psycopg_postgresql_transport_packaged")
        is True
        and infrastructure.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is True
        and infrastructure.get(
            "physical_postgresql_qdrant_writer_exclusion_transport_packaged"
        )
        is False
        and infrastructure.get(
            "durable_controller_rollback_marker_transport_packaged"
        )
        is True
        and infrastructure.get("external_direct_writer_exclusion_implemented")
        is False
        and infrastructure.get("selected_live_linux_platform_transports_packaged")
        is True
        and infrastructure.get(
            "selected_non_postgresql_live_linux_platform_transport_factory_packaged"
        )
        is True
        and infrastructure.get("phase7c_disposable_application_compose_role")
        == (
            "retained_application_validation_input_not_current_inactive_store_"
            "composition"
        )
        and infrastructure.get(
            "retained_phase7a_snapshot_postgresql_persistent_resource_created"
        )
        is False
        and infrastructure.get(
            "retained_phase7a_snapshot_qdrant_persistent_resource_created"
        )
        is False
        and infrastructure.get("phase8d_live_store_state_reverified") is False
        and infrastructure.get("existing_production_store_reuse_allowed") is False
        and infrastructure.get("legacy_snapshot_or_mount_reuse_allowed") is False
        and isinstance(release, dict)
        and release.get("create_allowed") is False
        and release.get("create_refusal_code") == CURRENT_CREATE_REFUSAL_CODE
        and release.get("cleanup_allowed") is False
        and release.get("cleanup_refusal_code") == "authorization_missing"
        and release.get("commands_executed") == 0
        and isinstance(ingestion, dict)
        and isinstance(frontend, dict)
        and frontend.get("deployed") is False
        and frontend.get("authenticated_visual_qa_complete") is False
        and isinstance(history, dict)
        and history.get("phase7c_runtime_build_receipt_sha256")
        == EXPECTED_PHASE7C_RUNTIME_RECEIPT_SHA256
        and history.get("phase7c_disposable_proof_receipt_sha256")
        == EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
        and history.get("phase7c_disposable_proof_receipt")
        == "ops/governed_memory/history/phase7c/disposable_proof_receipt.json"
        and history.get("reusable_for_current_candidate") is False,
        error,
    )
    assert isinstance(ingestion, dict)
    _verify_chat_only_scope(ingestion)
    return blockers


def _verify_governance_refusals(
    bootstrap: object,
    pilot: object,
    schema_contract: object,
    blockers: Sequence[str],
) -> None:
    _require(
        isinstance(bootstrap, dict)
        and isinstance(pilot, dict)
        and isinstance(schema_contract, dict),
        "release_governance_contract_invalid",
    )
    assert isinstance(bootstrap, dict)
    assert isinstance(pilot, dict)
    assert isinstance(schema_contract, dict)
    create = bootstrap.get("create_policy")
    cleanup = bootstrap.get("cleanup_policy")
    bridge = bootstrap.get("conversation_bridge")
    candidate = bootstrap.get("candidate_implementation_status")
    pilot_validation = pilot.get("validation_state")
    source_erasure = pilot.get("source_erasure")
    _require(
        bootstrap.get("schema_version") == "governed-memory-bootstrap-contract-v1"
        and bootstrap.get("state") == "inactive_candidate_no_resources_created"
        and bootstrap.get("production_state_changed") is False
        and isinstance(create, dict)
        and create.get("current_create_authorized") is False
        and create.get("unresolved_creation_prerequisites") == list(blockers)
        and isinstance(cleanup, dict)
        and cleanup.get("current_cleanup_authorized") is False
        and cleanup.get("requires_external_scoped_cleanup_authorization") is True
        and cleanup.get("sql_cascade_allowed") is False
        and cleanup.get("wildcard_target_allowed") is False
        and cleanup.get("prefix_target_allowed") is False
        and isinstance(bridge, dict)
        and bridge.get(
            "source_erasure_structured_lifeswitch_tables_or_accounts_deleted"
        )
        is False
        and bridge.get("source_erasure_legacy_project_rows_deleted") is False
        and isinstance(candidate, dict)
        and candidate.get("runtime")
        == "phase8g_current_source_bound_runtime_disposable_validated_inactive"
        and candidate.get("current_runtime_rebuild_pending") is False
        and candidate.get("current_candidate_disposable_validation_complete")
        is True
        and pilot.get("schema_version") == "governed-memory-pilot-contract-v1"
        and pilot.get("state") == "inactive_candidate_blocked_not_authorized"
        and pilot.get("production_state_changed") is False
        and isinstance(pilot_validation, dict)
        and pilot_validation.get("current_runtime_rebuild_pending") is False
        and pilot_validation.get(
            "current_candidate_disposable_validation_complete"
        )
        is True
        and pilot.get("start_blockers") == list(blockers)
        and pilot.get("eligible_input", {}).get("old_conversations") is False
        and pilot.get("eligible_input", {}).get("historical_backfill") is False
        and pilot.get("eligible_input", {}).get("attachment_content") is False
        and pilot.get("required_start_state", {}).get("legacy_import_count") == 0
        and pilot.get("required_start_state", {}).get("unprocessed_prefill_count")
        == 0
        and pilot.get("candidate_surfaces", {}).get("runtime")
        == "phase8g_current_source_bound_runtime_disposable_validated_inactive"
        and isinstance(source_erasure, dict)
        and source_erasure.get("structured_lifeswitch_data_deleted") is False
        and source_erasure.get("accounts_deleted") is False
        and source_erasure.get("legacy_project_rows_deleted") is False
        and schema_contract.get("status")
        == "isolated_candidate_disposable_validated_not_production_applied"
        and isinstance(schema_contract.get("hard_requirements"), dict)
        and schema_contract["hard_requirements"].get(
            "production_activation_blockers"
        )
        == list(blockers),
        "release_governance_contract_invalid",
    )


def verify_candidate_artifacts() -> dict[str, object]:
    observed_hashes: dict[str, str] = {}
    for relative, expected in sorted(
        EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES.items()
    ):
        path = ROOT / relative
        _require(
            path.is_file() and not path.is_symlink(),
            "release_artifact_missing_or_symlink",
        )
        actual = _sha256(path)
        _require(actual == expected, "release_artifact_hash_mismatch")
        observed_hashes[relative] = actual

    _require(
        CURRENT_PHASE8G_APPLICATION_PROOF.is_file()
        and not CURRENT_PHASE8G_APPLICATION_PROOF.is_symlink(),
        "release_artifact_missing_or_symlink",
    )
    _require(
        _sha256(CURRENT_PHASE8G_APPLICATION_PROOF)
        == EXPECTED_PHASE8G_PROOF_RECEIPT_SHA256,
        "release_artifact_hash_mismatch",
    )
    _require(
        CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT.is_file()
        and not CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT.is_symlink(),
        "release_artifact_missing_or_symlink",
    )
    _require(
        _sha256(CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT)
        == EXPECTED_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT_SHA256,
        "release_artifact_hash_mismatch",
    )

    try:
        migration_receipt = verify_migration_manifest.verify(MIGRATION_ROOT)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseGuardError("release_current_migration_artifacts_invalid") from error
    _require(
        isinstance(migration_receipt, dict)
        and migration_receipt.get("schema_version")
        == "governed-memory-migration-verification-v6"
        and migration_receipt.get("result") == "artifact_integrity_verified"
        and migration_receipt.get("validation_state")
        == "phase8g_current_candidate_disposable_validated"
        and migration_receipt.get("current_disposable_validation_complete")
        is True
        and migration_receipt.get("disposable_revalidation_required") is False
        and migration_receipt.get(
            "historical_phase7c_proof_reusable_for_current_candidate"
        )
        is False
        and migration_receipt.get("production_state_changed") is False
        and migration_receipt.get("manifest_sha256")
        == EXPECTED_PHASE8G_PROMOTED_MIGRATION_MANIFEST_SHA256
        and migration_receipt.get("manifest_sha256")
        == _sha256(ROOT_MIGRATION_MANIFEST),
        "release_current_migration_artifacts_invalid",
    )

    try:
        package_receipt = package.verify()
        store_receipt = verify_store_migration_manifest.verify()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseGuardError("release_current_store_package_invalid") from error
    package_receipt, store_receipt = _verify_current_package_receipts(
        package_receipt,
        store_receipt,
    )
    current_runtime_receipt = _load_json(CURRENT_RUNTIME_BUILD_RECEIPT)
    phase9j_controller_runtime_release_receipt = _load_json(
        CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT
    )
    historical_runtime_receipt = _load_json(
        HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT
    )
    historical_phase7c = _load_json(HISTORICAL_PHASE7C_APPLICATION_PROOF)
    current_phase8g = _load_json(CURRENT_PHASE8G_APPLICATION_PROOF)
    _require(
        _sha256(RUNTIME_MANIFEST) == EXPECTED_CURRENT_RUNTIME_MANIFEST_SHA256,
        "release_artifact_hash_mismatch",
    )
    runtime = _load_json(RUNTIME_MANIFEST)
    bootstrap = _load_json(BOOTSTRAP)
    pilot = _load_json(PILOT)
    schema_contract = _load_json(SCHEMA_CONTRACT)
    _verify_historical_phase7c_runtime_receipt(historical_runtime_receipt)
    _verify_current_runtime_receipt(current_runtime_receipt)
    _verify_phase9j_controller_runtime_release_receipt(
        phase9j_controller_runtime_release_receipt
    )
    _verify_historical_phase7c_application_proof(historical_phase7c)
    _verify_current_phase8g_application_proof(current_phase8g)
    blockers = _verify_runtime_manifest(runtime, package_receipt, store_receipt)
    _verify_governance_refusals(bootstrap, pilot, schema_contract, blockers)
    receipt_schema = _load_json(RECEIPT_SCHEMA)
    current_disposition = _load_json(CURRENT_COMPONENT_DISPOSITION)
    _require(
        isinstance(receipt_schema, dict)
        and receipt_schema.get("additionalProperties") is False,
        "release_receipt_schema_invalid",
    )
    _verify_current_component_disposition(current_disposition)

    package_artifacts = package_receipt["artifact_sha256"]
    assert isinstance(package_artifacts, dict)
    observed_hashes.update(
        {
            "governed-memory-migrations/manifest.json": _sha256(
                ROOT_MIGRATION_MANIFEST
            ),
            "governed-memory-migrations/schema_contract.json": _sha256(
                SCHEMA_CONTRACT
            ),
            "ops/governed_memory/bootstrap_contract.json": _sha256(BOOTSTRAP),
            "ops/governed_memory/runtime_build_receipt.json": _sha256(
                CURRENT_RUNTIME_BUILD_RECEIPT
            ),
            "ops/governed_memory/history/phase7c/runtime_build_receipt.json": _sha256(
                HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT
            ),
            "ops/governed_memory/runtime_manifest.json": _sha256(
                RUNTIME_MANIFEST
            ),
            "ops/governed_memory/phase9j_controller_runtime_release_receipt.json": _sha256(
                CURRENT_PHASE9J_CONTROLLER_RUNTIME_RELEASE_RECEIPT
            ),
            "ops/governed_memory/phase8g_disposable_proof_receipt.json": _sha256(
                CURRENT_PHASE8G_APPLICATION_PROOF
            ),
            "ops/governed_memory/pilot_contract.json": _sha256(PILOT),
            "ops/governed_memory/current_component_disposition.json": _sha256(
                CURRENT_COMPONENT_DISPOSITION
            ),
            "ops/governed_memory/release_receipt.schema.json": _sha256(
                RECEIPT_SCHEMA
            ),
            "ops/governed_memory/runtime-requirements.lock": _sha256(
                RUNTIME_LOCK
            ),
            "ops/governed_memory/build-requirements.lock": _sha256(BUILD_LOCK),
            "tools/governed_memory_validation/runtime_packages.json": _sha256(
                RUNTIME_PACKAGES
            ),
            "ops/governed_memory/installation/current/package_manifest.json": str(
                package_receipt["package_manifest_sha256"]
            ),
            "ops/governed_memory/installation/current/migration_manifest.json": str(
                store_receipt["manifest_sha256"]
            ),
            "tools/governed_memory_validation/postgres_bootstrap.pgsql": _sha256(
                POSTGRES_BOOTSTRAP
            ),
            "tools/governed_memory_validation/run_disposable_successor.sh": _sha256(
                DISPOSABLE_RUNNER
            ),
        }
    )
    return {
        "schema_version": "governed-memory-release-artifact-verification-v8",
        "phase": (
            "phase9j_install_ready_closed_runtime_and_store_transports_"
            "packaged_inactive_activation_blocked"
        ),
        "artifact_sha256": dict(sorted(observed_hashes.items())),
        "artifact_integrity_verified": True,
        "historical_phase7c_runtime_build_evidence_verified": True,
        "current_runtime_build_evidence_verified": True,
        "controller_runtime_release_receipt_verified": True,
        "controller_runtime_substrate_published_dormant": True,
        "persistent_store_resources_created": False,
        "current_runtime_rebuild_required": False,
        "historical_phase7c_application_proof_verified": True,
        "historical_phase7c_proof_reusable_for_current_candidate": False,
        "historical_phase7c_proof_is_live_proof": False,
        "current_phase8g_application_proof_verified": True,
        "current_migration_artifact_integrity_verified": True,
        "current_migration_disposable_validation_complete": True,
        "current_migration_disposable_revalidation_required": False,
        "current_store_package_static_verification_complete": True,
        "current_store_package_artifact_count": len(package_artifacts),
        "current_store_package_manifest_sha256": package_receipt[
            "package_manifest_sha256"
        ],
        "current_store_migration_manifest_sha256": store_receipt[
            "manifest_sha256"
        ],
        "synthetic_proof_harness_packaged": True,
        "current_store_synthetic_proof_complete": False,
        "historical_dormant_store_install_synthetic_proof_reusable_for_current_candidate": False,
        "synthetic_proof_executed_by_release_guard": False,
        "synthetic_proof_receipt_promoted": False,
        "current_candidate_disposable_proof_complete": True,
        "release_allowed": False,
        "release_refusal_code": CURRENT_CREATE_REFUSAL_CODE,
        "live_installation_proof_complete": False,
        "installation_executor_packaged": True,
        "rollback_executor_packaged": True,
        "controller_runtime_verification_capability_packaged": True,
        "full_controller_release_tree_verification_packaged": True,
        "exact_locked_controller_distribution_set_verification_packaged": True,
        "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt": True,
        "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_marker_and_receipt": True,
        "supervisor_launcher_source_packaged": True,
        "sealed_package_controller_runtime_built_or_installed_at_package_sealing": False,
        "sealed_package_controller_release_staged_at_package_sealing": False,
        "controller_runtime_and_release_required_separate_publication_authority": True,
        "stores_install_owns_or_removes_controller_substrate": False,
        "resolved_store_spec_and_exact_docker_labels_bound": True,
        "resource_identity_ledger_v2_packaged": True,
        "physical_postgresql_qdrant_writer_exclusion_packaged": False,
        "empty_rollback_controller_authority_marker_packaged": True,
        "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence": True,
        "destructive_rollback_steps_atomically_recheck_empty_under_fence": False,
        "empty_rollback_receipt_persisted_create_once_while_controller_marker_held": True,
        "closed_live_transport_contracts_packaged": True,
        "complete_closed_live_transport_substrate_set_packaged": True,
        "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": True,
        "driver_native_postgresql_stage_contract_packaged": True,
        "driver_native_postgresql_executable_stage_machine_packaged": True,
        "concrete_psycopg_postgresql_transport_packaged": True,
        "runtime_input_selection_contract_repaired": True,
        "runtime_build_receipt_provenance_v4_packaged": True,
        "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
        "approved_terminal_postgresql_catalog_manifest_selected": True,
        "physical_postgresql_qdrant_writer_exclusion_transport_packaged": False,
        "durable_controller_rollback_marker_transport_packaged": True,
        "external_direct_writer_exclusion_implemented": False,
        "stopped_store_semantic_empty_recheck_is_valid": False,
        "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented": True,
        "retained_audit_artifact_hashes_bound": True,
        "closed_install_store_effect_adapter_packaged": True,
        "closed_empty_rollback_store_effect_adapter_packaged": True,
        "selected_live_platform_transports_packaged": True,
        "selected_non_postgresql_live_platform_transport_factory_packaged": True,
        "pinned_postgresql_driver_selected": True,
        "durable_create_once_receipt_store_packaged": True,
        "controller_runtime_release_builder_orchestration_packaged": True,
        "controller_runtime_build_transport_packaged": True,
        "controller_runtime_publication_policy_transport_packaged": True,
        "runtime_publication_durable_intent_before_first_rename_packaged": True,
        "runtime_publication_post_intent_generic_cleanup_forbidden": True,
        "runtime_publication_crash_prefix_manual_review_fence_packaged": True,
        "runtime_publication_same_device_rename_precondition_packaged": True,
        "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged": True,
        "production_runtime_publication_primitives_packaged": True,
        "independent_standalone_cpython_payload_tree_proof_packaged": True,
        "activation_executor_packaged": False,
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
        CURRENT_CREATE_REFUSAL_CODE
        if operation == "create"
        else "authorization_missing"
    )
    return {
        "schema_version": "governed-memory-release-decision-v1",
        "operation": operation,
        "allowed": False,
        "reason_code": reason,
        "current_candidate_disposable_proof_complete": True,
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
    "CURRENT_CREATE_REFUSAL_CODE",
    "EXACT_TARGETS",
    "EXPECTED_ACTIVATION_BLOCKER_COUNT",
    "EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES",
    "REQUIRED_ACTIVATION_BLOCKERS",
    "ReleaseGuardError",
    "evaluate_release_observation",
    "verify_candidate_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
