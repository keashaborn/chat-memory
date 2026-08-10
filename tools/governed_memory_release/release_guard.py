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


ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops" / "governed_memory"
BOOTSTRAP = OPS / "bootstrap_contract.json"
PILOT = OPS / "pilot_contract.json"
RECEIPT_SCHEMA = OPS / "release_receipt.schema.json"
COMPOSE = OPS / "compose.candidate.yaml"
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
    "final_phase6b_runtime_rebuild_and_receipt_pending",
    "phase6b_migration_contract_disposable_proof_pending",
    "chat_deletion_memory_cancellation_coordination_not_implemented",
    "calibration_artifact_unapproved_retrieval_off",
    "frontend_candidate_35a684_undeployed_visual_qa_pending",
]
EXPECTED_CLEANUP_BLOCKERS: list[str] = []


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
    if not isinstance(bootstrap, dict) or not isinstance(pilot, dict) or not isinstance(receipt, dict):
        raise ReleaseGuardError("release_contract_invalid")
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
        or create_policy.get("unresolved_activation_blockers")
        != EXPECTED_CREATE_BLOCKERS
        or not isinstance(implementation, dict)
        or implementation.get("session_id_required") is not True
        or implementation.get("owner_claim_fact_detail")
        != "implemented_candidate_disposable_proof_passed_not_production_applied"
        or implementation.get("qdrant_adapter")
        != "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved"
        or implementation.get("pilot_marker")
        != "implemented_disposable_proof_passed_not_production_applied"
        or implementation.get("calibration")
        != "independently_bound_unapproved_retrieval_off"
    ):
        raise ReleaseGuardError("release_create_contract_invalid")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("current_cleanup_authorized") is not False
        or cleanup.get("unresolved_activation_blockers")
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
        != "strict_3072_fake_tested_zero_real_calls"
        or pilot.get("provider_policy", {}).get("calibration_status")
        != "independently_bound_unapproved_retrieval_off"
        or pilot.get("candidate_surfaces", {}).get("owner_claim_fact_detail")
        != "implemented_candidate_disposable_proof_passed_not_production_applied"
        or pilot.get("candidate_surfaces", {}).get("pilot_marker")
        != "implemented_disposable_proof_passed_not_production_applied"
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
