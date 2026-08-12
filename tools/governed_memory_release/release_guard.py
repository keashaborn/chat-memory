#!/usr/bin/env python3
from __future__ import annotations

"""Repository-only integrity verifier and refusal-only release guard.

This module has no installation, rollback, activation, Docker, network, secret,
PostgreSQL, or Qdrant execution surface. Phase 7C receipts are immutable
historical evidence only. The current Phase 8G build is verified separately;
release remains refused until its disposable proof is promoted.
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
PHASE8F_DISPOSITION = OPS / "phase8f_component_disposition.json"
PHASE7C_APPLICATION_PROOF = OPS / "phase7c_disposable_proof_receipt.json"
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

EXPECTED_CURRENT_RUNTIME_RECEIPT_SHA256 = (
    "25ca53e683e53f79b726909ef64bc8afad30269cce3f59804cf66335667a8108"
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
    "ops/governed_memory/phase7c_disposable_proof_receipt.json": (
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
        "phase8f_current_candidate_disposable_revalidation_not_completed",
    }
)
CURRENT_PROOF_REFUSAL_CODE = "current_candidate_disposable_proof_missing"

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
        and isinstance(package_artifacts, dict)
        and bool(package_artifacts)
        and package_receipt.get("artifact_count") == len(package_artifacts)
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
        error,
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
    blockers = _checked_activation_blockers(activation)
    assert isinstance(activation, dict)

    current_state = current.get("state") if isinstance(current, dict) else None
    _require(
        runtime.get("schema_version")
        == "governed-memory-successor-runtime-manifest-v2"
        and runtime.get("phase")
        == (
            "phase8g_current_runtime_rebuilt_disposable_revalidation_"
            "pending_activation_blocked"
        )
        and runtime.get("production_state_changed") is False
        and runtime.get("legacy_imports_allowed") is False
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
        and isinstance(phase7c, dict)
        and phase7c.get("scope")
        == "historical_phase7c_application_runtime_and_deletion_proof"
        and phase7c.get("evidence_role")
        == (
            "historical_application_evidence_not_current_candidate_store_"
            "installation_or_live_proof"
        )
        and phase7c.get("evidence_status")
        == (
            "historical_phase7c_disposable_application_validation_passed_not_"
            "current_phase8f_candidate_proof"
        )
        and phase7c.get("phase7c_proof_complete") is True
        and phase7c.get("reusable_as_current_store_installation_proof") is False
        and phase7c.get("reusable_as_live_proof") is False
        and phase7c.get("disposable_revalidation_required") is True
        and phase7c.get("runner_path")
        == "tools/governed_memory_validation/run_disposable_successor.sh"
        and phase7c.get("runner_sha256") == _sha256(DISPOSABLE_RUNNER)
        and phase7c.get("current_runner_execution_attested_by_phase7c_proof")
        is False
        and phase7c.get("current_runner_proof_execution_semantics_changed")
        is True
        and phase7c.get("historical_proof_receipt")
        == "ops/governed_memory/phase7c_disposable_proof_receipt.json"
        and phase7c.get("historical_proof_receipt_sha256")
        == EXPECTED_PHASE7C_PROOF_RECEIPT_SHA256
        and phase7c.get("current_phase8f_disposable_proof_complete") is False
        and phase7c.get("production_routes_installed") is False
        and phase7c.get("authenticated_frontend_verified") is False
        and phase7c.get("production_data_read") is False
        and phase7c.get("production_endpoint_calls") == 0
        and phase7c.get("provider_external_calls") == 0
        and phase7c.get("persistent_resources_created") is False
        and isinstance(current, dict)
        and current.get("scope") == "current_inactive_stores_only_package"
        and current_state
        == "phase8f_static_package_rebound_and_verified_not_authorized"
        and current.get("package_manifest")
        == "ops/governed_memory/installation/phase8b/package_manifest.json"
        and current.get("package_manifest_schema_version")
        == "governed-memory-phase8b-inactive-execution-package-manifest-v2"
        and current.get("package_manifest_sha256")
        == package_receipt.get("package_manifest_sha256")
        and current.get("package_artifact_count")
        == package_receipt.get("artifact_count")
        and current.get("contract_canonical_sha256")
        == package_receipt.get("contract_canonical_sha256")
        and current.get("contract")
        == "ops/governed_memory/installation/phase8b/contract.json"
        and current.get("controller_plan_canonical_sha256")
        == package_receipt.get("plan_canonical_sha256")
        and current.get("controller_plan")
        == "ops/governed_memory/installation/phase8b/controller_plan.json"
        and current.get("store_migration_manifest_sha256")
        == store_receipt.get("manifest_sha256")
        and current.get("store_migration_manifest")
        == "ops/governed_memory/installation/phase8b/migration_manifest.json"
        and current.get("store_migration_file_count")
        == store_receipt.get("file_count")
        and current.get("static_package_verification_complete") is True
        and current.get("historical_phase8b_static_package_verification_complete")
        is True
        and current.get("synthetic_proof_harness_packaged") is True
        and current.get("synthetic_proof_executed_for_current_package") is False
        and current.get("historical_phase8b_synthetic_proof_executed") is True
        and current.get("synthetic_proof_outcome")
        == (
            "historical_synthetic_matrix_passed_repository_only_not_current_"
            "phase8f_package_proof"
        )
        and current.get("synthetic_proof_executed_by_release_guard") is False
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
        is False
        and http.get("brains_erasure_proxy_matching_service_token_provisioned")
        is False
        and http.get("supabase_auth_sessions_rpc_live_verified") is False
        and isinstance(infrastructure, dict)
        and infrastructure.get("persistent_composition_packaged") is False
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
        and release.get("create_refusal_code") == CURRENT_PROOF_REFUSAL_CODE
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
        == (
            "phase8g_current_source_bound_runtime_rebuilt_disposable_"
            "revalidation_required_inactive"
        )
        and candidate.get("current_runtime_rebuild_pending") is False
        and candidate.get("current_candidate_disposable_validation_complete")
        is False
        and pilot.get("schema_version") == "governed-memory-pilot-contract-v1"
        and pilot.get("state") == "inactive_candidate_blocked_not_authorized"
        and pilot.get("production_state_changed") is False
        and isinstance(pilot_validation, dict)
        and pilot_validation.get("current_runtime_rebuild_pending") is False
        and pilot_validation.get(
            "current_candidate_disposable_validation_complete"
        )
        is False
        and pilot.get("start_blockers") == list(blockers)
        and pilot.get("eligible_input", {}).get("old_conversations") is False
        and pilot.get("eligible_input", {}).get("historical_backfill") is False
        and pilot.get("eligible_input", {}).get("attachment_content") is False
        and pilot.get("required_start_state", {}).get("legacy_import_count") == 0
        and pilot.get("required_start_state", {}).get("unprocessed_prefill_count")
        == 0
        and pilot.get("candidate_surfaces", {}).get("runtime")
        == (
            "phase8g_current_source_bound_runtime_rebuilt_disposable_"
            "revalidation_required_inactive"
        )
        and isinstance(source_erasure, dict)
        and source_erasure.get("structured_lifeswitch_data_deleted") is False
        and source_erasure.get("accounts_deleted") is False
        and source_erasure.get("legacy_project_rows_deleted") is False
        and schema_contract.get("status")
        == (
            "phase8f_repository_candidate_disposable_revalidation_required_"
            "not_production_applied"
        )
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

    try:
        migration_receipt = verify_migration_manifest.verify(MIGRATION_ROOT)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseGuardError("release_current_migration_artifacts_invalid") from error
    _require(
        isinstance(migration_receipt, dict)
        and migration_receipt.get("schema_version")
        == "governed-memory-migration-verification-v5"
        and migration_receipt.get("result") == "artifact_integrity_verified"
        and migration_receipt.get("validation_state")
        == "phase8f_disposable_revalidation_required"
        and migration_receipt.get("current_disposable_validation_complete")
        is False
        and migration_receipt.get("disposable_revalidation_required") is True
        and migration_receipt.get(
            "historical_phase7c_proof_reusable_for_current_candidate"
        )
        is False
        and migration_receipt.get("production_state_changed") is False
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
    historical_runtime_receipt = _load_json(
        HISTORICAL_PHASE7C_RUNTIME_BUILD_RECEIPT
    )
    phase7c = _load_json(PHASE7C_APPLICATION_PROOF)
    runtime = _load_json(RUNTIME_MANIFEST)
    bootstrap = _load_json(BOOTSTRAP)
    pilot = _load_json(PILOT)
    schema_contract = _load_json(SCHEMA_CONTRACT)
    _verify_historical_phase7c_runtime_receipt(historical_runtime_receipt)
    _verify_current_runtime_receipt(current_runtime_receipt)
    _verify_phase7c_application_proof(phase7c)
    blockers = _verify_runtime_manifest(runtime, package_receipt, store_receipt)
    _verify_governance_refusals(bootstrap, pilot, schema_contract, blockers)
    receipt_schema = _load_json(RECEIPT_SCHEMA)
    phase8f_disposition = _load_json(PHASE8F_DISPOSITION)
    _require(
        isinstance(receipt_schema, dict)
        and receipt_schema.get("additionalProperties") is False,
        "release_receipt_schema_invalid",
    )
    disposition_safety = (
        phase8f_disposition.get("safety")
        if isinstance(phase8f_disposition, dict)
        else None
    )
    _require(
        isinstance(phase8f_disposition, dict)
        and phase8f_disposition.get("schema_version")
        == "governed-memory-phase8f-component-disposition-v1"
        and isinstance(disposition_safety, dict)
        and disposition_safety.get("repository_only") is True
        and disposition_safety.get("production_state_changed") is False,
        "release_component_disposition_invalid",
    )

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
            "ops/governed_memory/pilot_contract.json": _sha256(PILOT),
            "ops/governed_memory/phase8f_component_disposition.json": _sha256(
                PHASE8F_DISPOSITION
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
            "ops/governed_memory/installation/phase8b/package_manifest.json": str(
                package_receipt["package_manifest_sha256"]
            ),
            "ops/governed_memory/installation/phase8b/migration_manifest.json": str(
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
        "schema_version": "governed-memory-release-artifact-verification-v4",
        "phase": (
            "phase8g_current_runtime_rebuilt_disposable_revalidation_"
            "pending_activation_blocked"
        ),
        "artifact_sha256": dict(sorted(observed_hashes.items())),
        "artifact_integrity_verified": True,
        "historical_phase7c_runtime_build_evidence_verified": True,
        "current_runtime_build_evidence_verified": True,
        "current_runtime_rebuild_required": False,
        "historical_phase7c_application_proof_verified": True,
        "historical_phase7c_proof_reusable_for_current_candidate": False,
        "historical_phase7c_proof_is_live_proof": False,
        "current_migration_artifact_integrity_verified": True,
        "current_migration_disposable_validation_complete": False,
        "current_migration_disposable_revalidation_required": True,
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
        "historical_phase8b_synthetic_proof_reusable_for_current_candidate": False,
        "synthetic_proof_executed_by_release_guard": False,
        "synthetic_proof_receipt_promoted": False,
        "current_candidate_disposable_proof_complete": False,
        "release_allowed": False,
        "release_refusal_code": CURRENT_PROOF_REFUSAL_CODE,
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
        CURRENT_PROOF_REFUSAL_CODE
        if operation == "create"
        else "authorization_missing"
    )
    return {
        "schema_version": "governed-memory-release-decision-v1",
        "operation": operation,
        "allowed": False,
        "reason_code": reason,
        "current_candidate_disposable_proof_complete": False,
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
    "CURRENT_PROOF_REFUSAL_CODE",
    "EXACT_TARGETS",
    "EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES",
    "REQUIRED_ACTIVATION_BLOCKERS",
    "ReleaseGuardError",
    "evaluate_release_observation",
    "verify_candidate_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
