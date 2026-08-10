#!/usr/bin/env python3
from __future__ import annotations

"""Offline artifact verifier and exact-target release decision guard.

This tool never executes a Docker, SQL, HTTP, systemd, firewall, or deletion
command.  It emits a content-free exact action plan only when an externally
captured observation closes every required guard.  Execution remains a
separate explicit activation or cleanup checkpoint.
"""

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

try:
    from tools.governed_memory_release.build_candidate_runtime import (
        _source_tree_sha256,
    )
except ModuleNotFoundError:  # Direct script execution from this directory.
    from build_candidate_runtime import _source_tree_sha256


ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops" / "governed_memory"
BOOTSTRAP = OPS / "bootstrap_contract.json"
PILOT = OPS / "pilot_contract.json"
RECEIPT_SCHEMA = OPS / "release_receipt.schema.json"
COMPOSE = OPS / "compose.candidate.yaml"
RUNTIME_MANIFEST = OPS / "runtime_manifest.json"
RUNTIME_BUILD_RECEIPT = OPS / "runtime_build_receipt.json"
RUNTIME_LOCK = OPS / "runtime-requirements.lock"
BUILD_LOCK = OPS / "build-requirements.lock"
RUNTIME_PACKAGES = ROOT / "tools" / "governed_memory_validation" / "runtime_packages.json"
SYSTEMD_HTTP_TEMPLATE = OPS / "systemd" / "governed-memory-http.service.in"
SYSTEMD_WORKER_TEMPLATE = OPS / "systemd" / "governed-memory-worker.service.in"
HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)

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
EXPECTED_CREATE_BLOCKERS = [
    "production_activation_not_authorized",
    "supabase_auth_sessions_rpc_not_installed_or_live_verified",
    "phase6b_migration_contract_disposable_proof_pending",
    "chat_deletion_memory_cancellation_coordination_not_implemented",
    "calibration_artifact_unapproved_retrieval_off",
    "frontend_candidate_6d80ba_undeployed_visual_qa_pending",
]
EXPECTED_CLEANUP_BLOCKERS: list[str] = []
EXPECTED_RUNTIME_SOURCE_SHA256 = (
    "d08cc71966beec1e31e107c08b71daa4e51daf3c0b3b6f5ef584ef8bae41c0e0"
)
EXPECTED_RUNTIME_RECEIPT_SHA256 = (
    "ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69"
)
EXPECTED_RUNTIME_MANIFEST_SHA256 = (
    "1e2d6ccb127064e449eb4f7836d58f75ffa93cb37313b60e9c6efa954d0c0276"
)
EXPECTED_RUNTIME_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
EXPECTED_RUNTIME_WHEEL_SHA256 = (
    "c1605f2a572dfde4d1c5b6246d331a88413f3db051cbb8a3c24ffdf6be98c5db"
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
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-"
    f"{EXPECTED_RUNTIME_SOURCE_SHA256}/bin/python"
)
EXPECTED_RUNTIME_WHEEL = (
    "/tmp/governed-memory-successor-build-"
    f"{EXPECTED_BUILD_LOCK_SHA256}-{EXPECTED_RUNTIME_SOURCE_SHA256}/dist/"
    "governed_memory_successor-0.0.0-py3-none-any.whl"
)
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
EXPECTED_ACTIVATION_BLOCKERS = [
    "production_activation_not_authorized",
    "semantic_calibration_artifact_unapproved_retrieval_off",
    "live_supabase_runtime_credentials_not_mounted_or_verified",
    "supabase_auth_sessions_rpc_not_installed_or_live_verified",
    "fresh_isolated_persistent_postgresql_not_created",
    "private_frontend_source_firewall_not_proved",
    "tls_termination_or_private_transport_not_decided",
    "production_store_role_bootstrap_not_implemented_or_authorized",
    "successor_http_service_not_installed",
    "successor_worker_service_not_installed",
    "successor_conversation_capture_not_activated",
    "provider_adapter_real_call_validation_not_authorized_or_completed",
    "embedding_adapter_real_call_validation_not_authorized_or_completed",
    "projection_reconciliation_and_sequence_safe_qdrant_repair_not_implemented",
    "phase6b_migration_contract_disposable_proof_pending",
    "chat_deletion_memory_cancellation_coordination_not_implemented",
    "frontend_candidate_6d80ba_undeployed_visual_qa_pending",
    "pilot_owner_and_scope_not_authorized",
    "legacy_memory_owner_scoped_read_write_shadow_quiescence_not_proved",
]


class ReleaseGuardError(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _load_json(path: Path, *, maximum_bytes: int = 128 * 1024) -> object:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum_bytes:
        raise ReleaseGuardError("release_json_invalid")
    try:
        return json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_closed_object,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
    ) as exc:
        raise ReleaseGuardError("release_json_invalid") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_candidate_artifacts() -> dict[str, object]:
    bootstrap = _load_json(BOOTSTRAP)
    pilot = _load_json(PILOT)
    receipt = _load_json(RECEIPT_SCHEMA)
    runtime_manifest = _load_json(RUNTIME_MANIFEST)
    runtime_build_receipt = _load_json(RUNTIME_BUILD_RECEIPT)
    if not all(
        isinstance(value, dict)
        for value in (
            bootstrap,
            pilot,
            receipt,
            runtime_manifest,
            runtime_build_receipt,
        )
    ):
        raise ReleaseGuardError("release_contract_invalid")
    for path, expected_sha256 in (
        (RUNTIME_LOCK, EXPECTED_RUNTIME_LOCK_SHA256),
        (BUILD_LOCK, EXPECTED_BUILD_LOCK_SHA256),
        (RUNTIME_PACKAGES, EXPECTED_RUNTIME_PACKAGES_SHA256),
    ):
        if (
            not path.is_file()
            or path.is_symlink()
            or _sha256(path) != expected_sha256
        ):
            raise ReleaseGuardError("release_runtime_dependency_invalid")
    runtime_package_manifest = _load_json(RUNTIME_PACKAGES)
    if (
        not isinstance(runtime_package_manifest, dict)
        or set(runtime_package_manifest)
        != {
            "schema_version",
            "python_implementation",
            "python_version",
            "runtime_lock_path",
            "runtime_lock_sha256",
            "candidate_project",
            "packages",
        }
        or runtime_package_manifest.get("schema_version")
        != "governed-memory-validation-runtime-v2"
        or runtime_package_manifest.get("python_implementation") != "CPython"
        or runtime_package_manifest.get("python_version") != "3.12.3"
        or runtime_package_manifest.get("runtime_lock_path")
        != "ops/governed_memory/runtime-requirements.lock"
        or runtime_package_manifest.get("runtime_lock_sha256")
        != EXPECTED_RUNTIME_LOCK_SHA256
        or runtime_package_manifest.get("candidate_project")
        != {"name": "governed-memory-successor", "version": "0.0.0"}
        or not isinstance(runtime_package_manifest.get("packages"), dict)
        or len(runtime_package_manifest["packages"]) != 19
    ):
        raise ReleaseGuardError("release_runtime_dependency_invalid")
    expected_receipt_packages = {
        re.sub(r"[-_.]+", "-", name).lower(): version
        for name, version in runtime_package_manifest["packages"].items()
    }
    runtime_validation = runtime_manifest.get("validation_runtime")
    runtime_disposable = runtime_manifest.get("disposable_validation")
    runtime_activation = runtime_manifest.get("activation")
    runtime_release_guard = runtime_manifest.get("release_guard")
    if (
        set(runtime_build_receipt) != EXPECTED_RUNTIME_RECEIPT_KEYS
        or _sha256(RUNTIME_BUILD_RECEIPT) != EXPECTED_RUNTIME_RECEIPT_SHA256
        or _sha256(RUNTIME_MANIFEST) != EXPECTED_RUNTIME_MANIFEST_SHA256
        or _source_tree_sha256(ROOT) != EXPECTED_RUNTIME_SOURCE_SHA256
        or runtime_manifest.get("schema_version")
        != "governed-memory-successor-runtime-manifest-v1"
        or runtime_manifest.get("production_state_changed") is not False
        or runtime_build_receipt.get("schema_version")
        != "governed-memory-runtime-build-receipt-v1"
        or runtime_build_receipt.get("python_version") != "3.12.3"
        or runtime_build_receipt.get("platform") != "linux_x86_64"
        or runtime_build_receipt.get("runtime_lock")
        != "ops/governed_memory/runtime-requirements.lock"
        or runtime_build_receipt.get("build_lock")
        != "ops/governed_memory/build-requirements.lock"
        or runtime_build_receipt.get("project_distribution")
        != {"name": "governed-memory-successor", "version": "0.0.0"}
        or runtime_build_receipt.get("source_tree_sha256")
        != EXPECTED_RUNTIME_SOURCE_SHA256
        or runtime_build_receipt.get("candidate_python")
        != EXPECTED_RUNTIME_PYTHON
        or runtime_build_receipt.get("candidate_python_sha256")
        != EXPECTED_RUNTIME_PYTHON_SHA256
        or runtime_build_receipt.get("runtime_lock_sha256")
        != EXPECTED_RUNTIME_LOCK_SHA256
        or runtime_build_receipt.get("build_lock_sha256")
        != EXPECTED_BUILD_LOCK_SHA256
        or runtime_build_receipt.get("project_wheel") != EXPECTED_RUNTIME_WHEEL
        or runtime_build_receipt.get("project_wheel_sha256")
        != EXPECTED_RUNTIME_WHEEL_SHA256
        or type(runtime_build_receipt.get("runtime_package_count")) is not int
        or runtime_build_receipt["runtime_package_count"] != 19
        or not isinstance(runtime_build_receipt.get("runtime_packages"), dict)
        or len(runtime_build_receipt["runtime_packages"]) != 19
        or runtime_build_receipt["runtime_packages"]
        != expected_receipt_packages
        or type(runtime_build_receipt.get("network_calls")) is not int
        or runtime_build_receipt["network_calls"] != 0
        or type(runtime_build_receipt.get("provider_calls")) is not int
        or runtime_build_receipt["provider_calls"] != 0
        or runtime_build_receipt.get("candidate_python_is_symlink") is not False
        or runtime_build_receipt.get("legacy_environment_imported") is not False
        or runtime_build_receipt.get("pip_present") is not False
        or runtime_build_receipt.get("setuptools_present") is not False
        or runtime_build_receipt.get("wheel_present") is not False
        or runtime_build_receipt.get("user_site_enabled") is not False
        or runtime_build_receipt.get("persistent_resources_created") is not False
        or runtime_build_receipt.get("production_state_changed") is not False
        or not isinstance(runtime_validation, dict)
        or runtime_validation
        != {
            "manifest": "tools/governed_memory_validation/runtime_packages.json",
            "runtime_lock": "ops/governed_memory/runtime-requirements.lock",
            "runtime_lock_sha256": EXPECTED_RUNTIME_LOCK_SHA256,
            "build_lock": "ops/governed_memory/build-requirements.lock",
            "build_lock_sha256": EXPECTED_BUILD_LOCK_SHA256,
            "current_source_tree_sha256": EXPECTED_RUNTIME_SOURCE_SHA256,
            "current_candidate_python": EXPECTED_RUNTIME_PYTHON,
            "current_candidate_python_sha256": EXPECTED_RUNTIME_PYTHON_SHA256,
            "current_project_wheel_sha256": EXPECTED_RUNTIME_WHEEL_SHA256,
            "current_source_bound": True,
            "final_phase6b_runtime_rebuild_pending": False,
            "current_build_receipt": "ops/governed_memory/runtime_build_receipt.json",
            "current_build_receipt_sha256": EXPECTED_RUNTIME_RECEIPT_SHA256,
            "current_build_receipt_present": True,
        }
        or runtime_validation.get("current_source_tree_sha256")
        != runtime_build_receipt["source_tree_sha256"]
        or runtime_validation.get("current_candidate_python")
        != runtime_build_receipt["candidate_python"]
        or runtime_validation.get("current_candidate_python_sha256")
        != runtime_build_receipt["candidate_python_sha256"]
        or runtime_validation.get("current_project_wheel_sha256")
        != runtime_build_receipt["project_wheel_sha256"]
        or runtime_validation.get("current_build_receipt_sha256")
        != EXPECTED_RUNTIME_RECEIPT_SHA256
        or runtime_validation.get("current_source_bound") is not True
        or runtime_validation.get("final_phase6b_runtime_rebuild_pending")
        is not False
        or runtime_validation.get("current_build_receipt_present") is not True
        or not isinstance(runtime_disposable, dict)
        or runtime_disposable.get("current_full_proof_complete") is not False
        or runtime_disposable.get("current_candidate_python") is not None
        or runtime_disposable.get("current_proof_receipt") is not None
        or runtime_disposable.get("final_resources_absent") is not False
        or runtime_disposable.get("resource_cleanup_complete") is not False
        or not isinstance(runtime_activation, dict)
        or runtime_activation.get("production_authorized") is not False
        or runtime_activation.get("installed_services") != []
        or runtime_activation.get("enabled_services") != []
        or runtime_activation.get("running_services") != []
        or runtime_activation.get("installed_timers") != []
        or runtime_activation.get("enabled_timers") != []
        or runtime_activation.get("blockers") != EXPECTED_ACTIVATION_BLOCKERS
        or runtime_release_guard
        != {
            "create_allowed": False,
            "create_refusal_code": "activation_blockers_open",
            "cleanup_allowed": False,
            "cleanup_refusal_code": "authorization_missing",
            "commands_executed": 0,
        }
    ):
        raise ReleaseGuardError("release_runtime_contract_invalid")
    if (
        bootstrap.get("schema_version") != "governed-memory-bootstrap-contract-v1"
        or bootstrap.get("state") != "inactive_candidate_no_resources_created"
        or bootstrap.get("production_state_changed") is not False
        or bootstrap.get("conversation_bridge", {}).get("outbox")
        != "memory_ingest_private.memory_ingest_outbox"
        or bootstrap.get("postgresql", {}).get("existing_target_action") != "refuse"
        or bootstrap.get("qdrant", {}).get("existing_target_action") != "refuse"
    ):
        raise ReleaseGuardError("release_bootstrap_contract_invalid")
    cleanup = bootstrap.get("cleanup_policy")
    create_policy = bootstrap.get("create_policy")
    implementation = bootstrap.get("candidate_implementation_status")
    if (
        not isinstance(create_policy, dict)
        or create_policy.get("current_create_authorized") is not False
        or create_policy.get("requires_all_creation_prerequisites_closed")
        is not True
        or create_policy.get("unresolved_creation_prerequisites")
        != EXPECTED_CREATE_BLOCKERS
        or not isinstance(implementation, dict)
        or implementation.get("session_id_required") is not True
        or implementation.get("runtime")
        != "phase6b_source_bound_receipt_present_disposable_execution_pending"
        or implementation.get("owner_claim_fact_detail")
        != "implemented_candidate_phase6b_disposable_revalidation_pending_not_production_applied"
        or implementation.get("qdrant_adapter")
        != "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved"
        or implementation.get("pilot_marker")
        != "implemented_phase6b_disposable_revalidation_pending_not_production_applied"
        or implementation.get("calibration")
        != "independently_bound_unapproved_retrieval_off"
    ):
        raise ReleaseGuardError("release_create_contract_invalid")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("current_cleanup_authorized") is not False
        or cleanup.get("unresolved_cleanup_prerequisites")
        != EXPECTED_CLEANUP_BLOCKERS
        or cleanup.get("requires_pilot_ever_started_false") is not True
        or cleanup.get("refuse_after_any_pilot_row") is not True
        or cleanup.get("sql_cascade_allowed") is not False
        or cleanup.get("wildcard_target_allowed") is not False
        or cleanup.get("prefix_target_allowed") is not False
        or set(cleanup.get("exact_targets", []))
        != {
            EXACT_TARGETS["postgres_container"],
            EXACT_TARGETS["qdrant_container"],
            EXACT_TARGETS["postgres_volume"],
            EXACT_TARGETS["qdrant_volume"],
            EXACT_TARGETS["network"],
        }
    ):
        raise ReleaseGuardError("release_cleanup_contract_invalid")
    if (
        pilot.get("schema_version") != "governed-memory-pilot-contract-v1"
        or pilot.get("state") != "inactive_candidate_blocked_not_authorized"
        or pilot.get("production_state_changed") is not False
        or pilot.get("required_start_state", {}).get("legacy_import_count") != 0
        or pilot.get("eligible_input", {}).get("attachment_content") is not False
        or pilot.get("authentication", {}).get(
            "fresh_user_check_claimed_as_immediate_signout_revocation"
        )
        is not False
        or pilot.get("authentication", {}).get("session_id_required") is not True
        or pilot.get("authentication", {}).get(
            "supabase_auth_sessions_rpc_live_verified"
        )
        is not False
        or pilot.get("provider_policy", {}).get("provider_adapter_status")
        != "strict_fake_tested_zero_real_calls"
        or pilot.get("provider_policy", {}).get("embedding_adapter_status")
        != "strict_3072_fake_tested_durable_request_dispatch_marker_implemented_disposable_proof_pending_zero_real_calls"
        or pilot.get("provider_policy", {}).get("calibration_status")
        != "independently_bound_unapproved_retrieval_off"
        or pilot.get("candidate_surfaces", {}).get("owner_claim_fact_detail")
        != "implemented_candidate_phase6b_disposable_revalidation_pending_not_production_applied"
        or pilot.get("candidate_surfaces", {}).get("runtime")
        != "phase6b_source_bound_receipt_present_disposable_execution_pending"
        or pilot.get("candidate_surfaces", {}).get("pilot_marker")
        != "implemented_phase6b_disposable_revalidation_pending_not_production_applied"
        or pilot.get("provider_policy", {}).get("qdrant_adapter_status")
        != "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved"
    ):
        raise ReleaseGuardError("release_pilot_contract_invalid")
    if (
        receipt.get("additionalProperties") is not False
        or receipt.get("properties", {}).get("reason_code", {}).get("enum") is None
    ):
        raise ReleaseGuardError("release_receipt_schema_invalid")

    compose = COMPOSE.read_text(encoding="utf-8")
    required_compose = (
        "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
        "qdrant/qdrant:v1.19.0@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc",
        '"127.0.0.1:55432:5432"',
        '"127.0.0.1:6343:6333"',
        "log_parameter_max_length=0",
        "log_parameter_max_length_on_error=0",
        '${GOVERNED_MEMORY_BOOTSTRAP_PASSWORD:?required}',
        '${GOVERNED_MEMORY_QDRANT_API_KEY:?required}',
        'restart: "no"',
    )
    if any(value not in compose for value in required_compose) or ":latest" in compose:
        raise ReleaseGuardError("release_compose_contract_invalid")
    http_unit = SYSTEMD_HTTP_TEMPLATE.read_text(encoding="utf-8")
    worker_unit = SYSTEMD_WORKER_TEMPLATE.read_text(encoding="utf-8")
    if (
        "[Install]" in http_unit
        or "WantedBy=" in http_unit
        or "Restart=no" not in http_unit
        or "GOVERNED_MEMORY_HTTP_MODE=off" not in http_unit
        or "SocketBindAllow=tcp:8091" not in http_unit
        or "/opt/chat-memory" in http_unit
        or "[Install]" in worker_unit
        or "WantedBy=" in worker_unit
        or "Restart=no" not in worker_unit
        or "Type=oneshot" not in worker_unit
        or "GOVERNED_MEMORY_WORKER_MODE=off" not in worker_unit
        or "ConditionPathExists=/etc/governed-memory/worker.env" not in worker_unit
        or "ConditionPathExists=/etc/governed-memory/pilot.env" not in worker_unit
        or "EnvironmentFile=/etc/governed-memory/worker.env" not in worker_unit
        or "EnvironmentFile=/etc/governed-memory/pilot.env" not in worker_unit
        or "/opt/chat-memory" in worker_unit
        or "DISPOSABLE VALIDATION ONLY" not in compose
        or "x-governed-memory-scope: disposable-validation-only" not in compose
    ):
        raise ReleaseGuardError("release_systemd_contract_invalid")
    hashes = {
        path.relative_to(ROOT).as_posix(): _sha256(path)
        for path in (
            BOOTSTRAP,
            PILOT,
            RECEIPT_SCHEMA,
            COMPOSE,
            RUNTIME_MANIFEST,
            RUNTIME_BUILD_RECEIPT,
            RUNTIME_LOCK,
            BUILD_LOCK,
            RUNTIME_PACKAGES,
            SYSTEMD_HTTP_TEMPLATE,
            SYSTEMD_WORKER_TEMPLATE,
        )
    }
    return {
        "schema_version": "governed-memory-release-artifact-verification-v1",
        "artifact_sha256": dict(sorted(hashes.items())),
        "production_state_changed": False,
        "external_calls": 0,
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
    if operation == "create":
        reason = "activation_blockers_open"
        allowed = False
        actions: list[list[str]] = []
    else:
        reason = "authorization_missing"
        allowed = False
        actions = []
    return {
        "schema_version": "governed-memory-release-decision-v1",
        "operation": operation,
        "allowed": allowed,
        "reason_code": reason,
        "exact_action_plan": actions,
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
        else:
            result = evaluate_release_observation(
                _load_json(arguments.observation.resolve())
            )
    except ReleaseGuardError as exc:
        print(
            json.dumps(
                {
                    "error": {"code": str(exc)},
                    "schema_version": "governed-memory-release-guard-error-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


__all__ = [
    "EXACT_TARGETS",
    "ReleaseGuardError",
    "evaluate_release_observation",
    "main",
    "verify_candidate_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
