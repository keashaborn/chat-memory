#!/usr/bin/env python3
from __future__ import annotations

"""Repository-only verifier for the current inactive governed-Memory package.

This module has no installation, rollback, activation, Docker, network, secret,
PostgreSQL, or Qdrant execution surface.  It verifies immutable application
evidence, the current closed stores-only package, and refusal-only release
observations.  The retained Phase 7C receipt is application/runtime evidence;
it is not proof of the current store installation package or a live system.
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
from tools.governed_memory_validation import verify_store_migration_manifest


OPS = ROOT / "ops" / "governed_memory"
RUNTIME_MANIFEST = OPS / "runtime_manifest.json"
RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
RUNTIME_LOCK = OPS / "runtime-requirements.lock"
BUILD_LOCK = OPS / "build-requirements.lock"
RUNTIME_PACKAGES = (
    ROOT / "tools" / "governed_memory_validation" / "runtime_packages.json"
)
BOOTSTRAP = OPS / "bootstrap_contract.json"
PILOT = OPS / "pilot_contract.json"
RECEIPT_SCHEMA = OPS / "release_receipt.schema.json"
PHASE7C_APPLICATION_PROOF = OPS / "phase7c_disposable_proof_receipt.json"
PHASE8D_RETIREMENT_LEDGER = (
    OPS
    / "history"
    / "phase8d"
    / "phase8a_successor_installation_stack_retirement.json"
)
ROOT_MIGRATION_MANIFEST = ROOT / "governed-memory-migrations" / "manifest.json"

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
EXPECTED_ROOT_MIGRATION_MANIFEST_SHA256 = (
    "57ea2a0b151b0ac4a84f0df86041e418d1b1a7843cbfd9281175e34500e15150"
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
EXPECTED_PHASE8D_RETIREMENT_LEDGER_SHA256 = (
    "593e1c76e66905c8855625c88c42b1cb080c5e0f0380230cbb03246eaafccae6"
)
EXPECTED_PHASE8D_SYNTHETIC_RECEIPT_SHA256 = (
    "ce9a57f6f01c670dfbc030bff812b99bfb13e9782f406d7e576f0d89af1f33c6"
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

# This is deliberately the sole direct raw-byte pin for the mutable summary.
EXPECTED_RUNTIME_MANIFEST_SHA256 = (
    "99072a21c7ebd4c16b6a329b1d02e024c7d0e308d690060519740bbd86339b78"
)

EXPECTED_FIXED_APPLICATION_ARTIFACT_HASHES = {
    "governed-memory-migrations/manifest.json": (
        EXPECTED_ROOT_MIGRATION_MANIFEST_SHA256
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
    "ops/governed_memory/history/phase8d/phase8a_successor_installation_stack_retirement.json": (
        EXPECTED_PHASE8D_RETIREMENT_LEDGER_SHA256
    ),
    "ops/governed_memory/phase7c_disposable_proof_receipt.json": (
        EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
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
    "tools/governed_memory_validation/postgres_bootstrap.pgsql": (
        "0c28d2e444cddea0b61e8ea7ac9f6084b06e2038beb06bb65e754c4712eeb857"
    ),
    "tools/governed_memory_validation/run_disposable_successor.sh": (
        "2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b"
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


def _verify_phase7c_application_proof(value: object) -> None:
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
        and proof.get("source_tree_sha256") == EXPECTED_RUNTIME_SOURCE_SHA256
        and proof.get("runtime_build_receipt_sha256")
        == EXPECTED_RUNTIME_RECEIPT_SHA256
        and proof.get("manifest_sha256")
        == EXPECTED_PHASE7C_ATTESTED_MIGRATION_MANIFEST_SHA256
        and proof.get("runtime_packages_sha256")
        == EXPECTED_RUNTIME_PACKAGES_SHA256
        and proof.get("runtime_lock_sha256") == EXPECTED_RUNTIME_LOCK_SHA256
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
        == "governed-memory-phase8b-package-verification-v3"
        and package_receipt.get("artifact_count") == 44
        and isinstance(package_artifacts, dict)
        and len(package_artifacts) == 44
        and package_receipt.get("guarded_synthetic_proof_harness_packaged") is True
        and package_receipt.get("synthetic_proof_executed_by_verifier") is False
        and package_receipt.get("synthetic_proof_receipt_promoted") is False
        and package_receipt.get("installation_executor_packaged") is False
        and package_receipt.get("rollback_executor_packaged") is False
        and package_receipt.get("activation_executor_packaged") is False
        and package_receipt.get("installation_performed_by_verifier") is False
        and package_receipt.get("images_staged_by_verifier") is False
        and package_receipt.get("secrets_touched_by_verifier") is False
        and package_receipt.get("activation_performed_by_verifier") is False
        and store_receipt.get("schema_version")
        == "governed-memory-phase8b-store-migration-verification-v2"
        and store_receipt.get("file_count") == 10
        and isinstance(store_artifacts, dict)
        and len(store_artifacts) == 10
        and store_receipt.get("source_bridge_artifact_count") == 0
        and store_receipt.get("historical_package_descriptor_count") == 0
        and store_receipt.get("production_state_changed") is False
        and package_receipt.get("migration_manifest_sha256")
        == store_receipt.get("manifest_sha256"),
        "release_current_store_package_invalid",
    )
    return package_receipt, store_receipt


def _verify_runtime_manifest(
    runtime: object,
    package_receipt: Mapping[str, object],
    store_receipt: Mapping[str, object],
) -> None:
    expected_top_level = {
        "schema_version",
        "phase",
        "python_runtime",
        "validation_runtime",
        "authority",
        "infrastructure",
        "http_runtime",
        "activation",
        "release_guard",
        "ingestion",
        "worker_adapters",
        "provider_policy",
        "phase7c_application_validation",
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
        "release_runtime_manifest_invalid",
    )
    assert isinstance(runtime, dict)
    validation = runtime.get("validation_runtime")
    phase7c = runtime.get("phase7c_application_validation")
    current = runtime.get("inactive_store_package")
    activation = runtime.get("activation")
    http = runtime.get("http_runtime")
    infrastructure = runtime.get("infrastructure")
    release = runtime.get("release_guard")
    ingestion = runtime.get("ingestion")
    frontend = runtime.get("frontend_candidate")
    history = runtime.get("historical_evidence")
    _require(
        runtime.get("schema_version")
        == "governed-memory-successor-runtime-manifest-v2"
        and runtime.get("phase")
        == (
            "phase8d_repository_only_phase8a_successor_installation_stack_"
            "retired_"
            "current_inactive_store_package_activation_blocked"
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
        and isinstance(phase7c, dict)
        and phase7c.get("scope")
        == "retained_phase7c_application_runtime_and_deletion_proof"
        and phase7c.get("evidence_role")
        == "application_evidence_not_current_store_installation_or_live_proof"
        and phase7c.get("phase7c_proof_complete") is True
        and phase7c.get("reusable_as_current_store_installation_proof") is False
        and phase7c.get("reusable_as_live_proof") is False
        and phase7c.get("current_proof_receipt")
        == "ops/governed_memory/phase7c_disposable_proof_receipt.json"
        and phase7c.get("current_proof_receipt_sha256")
        == EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
        and phase7c.get("production_routes_installed") is False
        and phase7c.get("authenticated_frontend_verified") is False
        and phase7c.get("semantic_threshold_calibrated") is False
        and phase7c.get("production_data_read") is False
        and phase7c.get("production_endpoint_calls") == 0
        and phase7c.get("provider_external_calls") == 0
        and phase7c.get("persistent_resources_created") is False
        and isinstance(current, dict)
        and current.get("scope") == "current_inactive_stores_only_package"
        and current.get("state")
        == (
            "static_verified_synthetic_harness_executed_separately_passed_not_"
            "promoted_no_phase8d_installation_not_authorized"
        )
        and current.get("package_manifest")
        == "ops/governed_memory/installation/phase8b/package_manifest.json"
        and current.get("package_manifest_sha256")
        == package_receipt.get("package_manifest_sha256")
        and current.get("package_manifest_schema_version")
        == "governed-memory-phase8b-inactive-execution-package-manifest-v2"
        and current.get("package_artifact_count")
        == package_receipt.get("artifact_count")
        and current.get("contract")
        == "ops/governed_memory/installation/phase8b/contract.json"
        and current.get("contract_canonical_sha256")
        == package_receipt.get("contract_canonical_sha256")
        and current.get("controller_plan")
        == "ops/governed_memory/installation/phase8b/controller_plan.json"
        and current.get("controller_plan_canonical_sha256")
        == package_receipt.get("plan_canonical_sha256")
        and current.get("store_migration_manifest")
        == "ops/governed_memory/installation/phase8b/migration_manifest.json"
        and current.get("store_migration_manifest_sha256")
        == store_receipt.get("manifest_sha256")
        and current.get("store_migration_file_count")
        == store_receipt.get("file_count")
        and current.get("static_package_verification_complete") is True
        and current.get("synthetic_proof_harness_packaged") is True
        and current.get("synthetic_proof_executed_for_current_package") is True
        and current.get("synthetic_proof_executed_by_release_guard") is False
        and current.get("synthetic_proof_scenario_count") == 131
        and current.get("synthetic_proof_outcome")
        == "synthetic_matrix_passed_repository_only_not_live_proof"
        and current.get("synthetic_proof_receipt_sha256")
        == EXPECTED_PHASE8D_SYNTHETIC_RECEIPT_SHA256
        and current.get("synthetic_proof_execution_record")
        == (
            "ops/governed_memory/history/phase8d/"
            "phase8a_successor_installation_stack_retirement.json"
        )
        and current.get("synthetic_proof_full_receipt_persisted") is False
        and current.get("synthetic_proof_receipt_promoted") is False
        and current.get("live_installation_proof_complete") is False
        and current.get("installation_executor_packaged") is False
        and current.get("rollback_executor_packaged") is False
        and current.get("activation_executor_packaged") is False
        and current.get("phase8d_installation_performed") is False
        and current.get("live_installation_state_reverified_by_phase8d") is False
        and current.get("installation_authorized") is False
        and current.get("activation_authorized") is False
        and current.get("production_state_changed") is False
        and isinstance(activation, dict)
        and activation.get("blockers") == EXPECTED_ACTIVATION_BLOCKERS
        and activation.get("production_authorized") is False
        and activation.get("retained_phase7a_snapshot_installed_services") == []
        and activation.get("retained_phase7a_snapshot_running_services") == []
        and activation.get("retained_phase7a_snapshot_enabled_services") == []
        and activation.get("retained_phase7a_snapshot_installed_timers") == []
        and activation.get("retained_phase7a_snapshot_enabled_timers") == []
        and activation.get("phase8d_live_successor_service_state_reverified")
        is False
        and "installed_services" not in activation
        and "running_services" not in activation
        and "enabled_services" not in activation
        and "installed_timers" not in activation
        and "enabled_timers" not in activation
        and isinstance(http, dict)
        and http.get("default_mode") == "off"
        and http.get("conversation_erasure_route_installed") is False
        and http.get("conversation_erasure_route_routed") is False
        and http.get("conversation_erasure_route_live_verified") is False
        and http.get("supabase_auth_sessions_rpc_live_verified") is False
        and http.get("source_logging_policy_live_verified") is False
        and http.get("source_logging_parameter_remediation_authorized") is False
        and http.get("source_logging_parameter_remediation_applied") is False
        and isinstance(infrastructure, dict)
        and infrastructure.get("current_inactive_store_contract")
        == "ops/governed_memory/installation/phase8b/contract.json"
        and infrastructure.get("current_inactive_store_package_manifest")
        == "ops/governed_memory/installation/phase8b/package_manifest.json"
        and infrastructure.get("phase7c_disposable_application_compose")
        == "ops/governed_memory/compose.candidate.yaml"
        and infrastructure.get("phase7c_disposable_application_compose_role")
        == (
            "retained_application_validation_input_not_current_inactive_store_"
            "composition"
        )
        and infrastructure.get("persistent_composition_packaged") is False
        and "compose_candidate" not in infrastructure
        and "persistent_compose_candidate" not in infrastructure
        and "compose_scope" not in infrastructure
        and "inactive_installation_contract" not in infrastructure
        and "inactive_installation_package_manifest" not in infrastructure
        and infrastructure.get(
            "retained_phase7a_snapshot_postgresql_persistent_resource_created"
        )
        is False
        and infrastructure.get(
            "retained_phase7a_snapshot_qdrant_persistent_resource_created"
        )
        is False
        and infrastructure.get("phase8d_live_store_state_reverified") is False
        and "postgresql_persistent_resource_created" not in infrastructure
        and "qdrant_persistent_resource_created" not in infrastructure
        and infrastructure.get("existing_production_store_reuse_allowed") is False
        and infrastructure.get("legacy_snapshot_or_mount_reuse_allowed") is False
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
        and frontend.get("deployed") is False
        and frontend.get("authenticated_visual_qa_complete") is False
        and isinstance(history, dict)
        and history.get("reusable_for_current_candidate") is False,
        "release_runtime_manifest_invalid",
    )
    assert isinstance(ingestion, dict)
    _verify_chat_only_scope(ingestion)


def _verify_governance_refusals(bootstrap: object, pilot: object) -> None:
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
        and bridge.get(
            "source_erasure_structured_lifeswitch_tables_or_accounts_deleted"
        )
        is False
        and bridge.get("source_erasure_legacy_project_rows_deleted") is False
        and pilot.get("schema_version") == "governed-memory-pilot-contract-v1"
        and pilot.get("state") == "inactive_candidate_blocked_not_authorized"
        and pilot.get("production_state_changed") is False
        and pilot.get("start_blockers") == EXPECTED_ACTIVATION_BLOCKERS
        and pilot.get("eligible_input", {}).get("old_conversations") is False
        and pilot.get("eligible_input", {}).get("historical_backfill") is False
        and pilot.get("eligible_input", {}).get("attachment_content") is False
        and pilot.get("required_start_state", {}).get("legacy_import_count") == 0
        and pilot.get("required_start_state", {}).get("unprocessed_prefill_count")
        == 0
        and isinstance(source_erasure, dict)
        and source_erasure.get("structured_lifeswitch_data_deleted") is False
        and source_erasure.get("accounts_deleted") is False
        and source_erasure.get("legacy_project_rows_deleted") is False,
        "release_governance_contract_invalid",
    )


def verify_candidate_artifacts() -> dict[str, object]:
    observed_hashes: dict[str, str] = {}
    for relative, expected in sorted(
        EXPECTED_FIXED_APPLICATION_ARTIFACT_HASHES.items()
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
        _sha256(RUNTIME_LOCK) == EXPECTED_RUNTIME_LOCK_SHA256
        and _sha256(BUILD_LOCK) == EXPECTED_BUILD_LOCK_SHA256
        and _sha256(RUNTIME_PACKAGES) == EXPECTED_RUNTIME_PACKAGES_SHA256,
        "release_runtime_lock_invalid",
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
    receipt = _load_json(RUNTIME_BUILD_RECEIPT)
    phase7c = _load_json(PHASE7C_APPLICATION_PROOF)
    phase8d = _load_json(PHASE8D_RETIREMENT_LEDGER)
    runtime = _load_json(RUNTIME_MANIFEST)
    bootstrap = _load_json(BOOTSTRAP)
    pilot = _load_json(PILOT)
    _verify_runtime_receipt(receipt)
    _verify_phase7c_application_proof(phase7c)
    phase8d_verification = (
        phase8d.get("verification") if isinstance(phase8d, dict) else None
    )
    phase8d_safety = phase8d.get("safety") if isinstance(phase8d, dict) else None
    phase8d_replacement = (
        phase8d.get("replacement") if isinstance(phase8d, dict) else None
    )
    _require(
        isinstance(phase8d, dict)
        and phase8d.get("schema_version")
        == (
            "governed-memory-phase8d-phase8a-successor-installation-stack-"
            "retirement-v1"
        )
        and phase8d.get("state")
        == (
            "repository_retirement_verified_no_phase8d_live_mutation_live_"
            "absence_not_reverified"
        )
        and isinstance(phase8d_replacement, dict)
        and phase8d_replacement.get("package_manifest_sha256")
        == package_receipt.get("package_manifest_sha256")
        and phase8d_replacement.get("artifact_count")
        == package_receipt.get("artifact_count")
        and isinstance(phase8d_verification, dict)
        and phase8d_verification.get("synthetic_scenario_count") == 131
        and phase8d_verification.get("synthetic_outcome")
        == "synthetic_matrix_passed_not_live_proof"
        and phase8d_verification.get("synthetic_receipt_sha256")
        == EXPECTED_PHASE8D_SYNTHETIC_RECEIPT_SHA256
        and isinstance(phase8d_safety, dict)
        and phase8d_safety.get("docker_run_or_image_operation_performed") is False
        and phase8d_safety.get("secret_read_write_or_generation_performed")
        is False
        and phase8d_safety.get("service_change_performed") is False
        and phase8d_safety.get("postgresql_operation_performed") is False
        and phase8d_safety.get("qdrant_operation_performed") is False
        and phase8d_safety.get("installation_performed") is False
        and phase8d_safety.get("activation_performed") is False,
        "release_phase8d_retirement_ledger_invalid",
    )
    _verify_runtime_manifest(runtime, package_receipt, store_receipt)
    _verify_governance_refusals(bootstrap, pilot)
    receipt_schema = _load_json(RECEIPT_SCHEMA)
    _require(
        isinstance(receipt_schema, dict)
        and receipt_schema.get("additionalProperties") is False,
        "release_receipt_schema_invalid",
    )
    _require(
        _sha256(RUNTIME_MANIFEST) == EXPECTED_RUNTIME_MANIFEST_SHA256,
        "release_runtime_manifest_hash_mismatch",
    )
    package_artifacts = package_receipt["artifact_sha256"]
    assert isinstance(package_artifacts, dict)
    observed_hashes.update(
        {
            "ops/governed_memory/runtime_manifest.json": _sha256(
                RUNTIME_MANIFEST
            ),
            "ops/governed_memory/runtime-requirements.lock": _sha256(
                RUNTIME_LOCK
            ),
            "ops/governed_memory/build-requirements.lock": _sha256(BUILD_LOCK),
            "tools/governed_memory_validation/runtime_packages.json": _sha256(
                RUNTIME_PACKAGES
            ),
            "ops/governed_memory/installation/phase8b/package_manifest.json": str(
                package_receipt["package_manifest_sha256"]
            ),
            "ops/governed_memory/installation/phase8b/migration_manifest.json": str(
                store_receipt["manifest_sha256"]
            ),
        }
    )
    return {
        "schema_version": "governed-memory-release-artifact-verification-v3",
        "phase": (
            "phase8d_repository_only_phase8a_successor_installation_stack_"
            "retired_"
            "current_inactive_store_package_activation_blocked"
        ),
        "artifact_sha256": dict(sorted(observed_hashes.items())),
        "runtime_build_evidence_verified": True,
        "phase7c_application_proof_verified": True,
        "phase7c_proof_is_current_store_installation_proof": False,
        "phase7c_proof_is_live_proof": False,
        "current_store_package_static_verification_complete": True,
        "current_store_package_artifact_count": len(package_artifacts),
        "current_store_package_manifest_sha256": package_receipt[
            "package_manifest_sha256"
        ],
        "current_store_migration_manifest_sha256": store_receipt[
            "manifest_sha256"
        ],
        "synthetic_proof_harness_packaged": True,
        "synthetic_proof_executed_separately_for_current_package": True,
        "synthetic_proof_executed_by_release_guard": False,
        "synthetic_proof_scenario_count": 131,
        "synthetic_proof_receipt_sha256": (
            EXPECTED_PHASE8D_SYNTHETIC_RECEIPT_SHA256
        ),
        "synthetic_proof_receipt_promoted": False,
        "live_installation_proof_complete": False,
        "installation_executor_packaged": False,
        "rollback_executor_packaged": False,
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
    "EXPECTED_PHASE8D_RETIREMENT_LEDGER_SHA256",
    "EXPECTED_PHASE8D_SYNTHETIC_RECEIPT_SHA256",
    "EXPECTED_ROOT_MIGRATION_MANIFEST_SHA256",
    "EXPECTED_RUNTIME_MANIFEST_SHA256",
    "ReleaseGuardError",
    "evaluate_release_observation",
    "verify_candidate_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
