#!/usr/bin/env python3
from __future__ import annotations

"""Verify installation artifacts and evaluate content-free observations.

This module intentionally has no mutation or network implementation. It does
not invoke Docker, systemd, PostgreSQL, Qdrant, or a provider. It may read
explicitly supplied observation and prior-receipt paths outside the checked-in
package, but performs no writes, commands, or authorization. Its receipts are
structural evidence only and can never authorize installation, rollback,
activation, or resource mutation.
"""

from collections.abc import Mapping
import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INSTALLATION = ROOT / "ops" / "governed_memory" / "installation"
CONTRACT = INSTALLATION / "contract.json"
PACKAGE_MANIFEST = INSTALLATION / "package_manifest.json"
COMPOSE = INSTALLATION / "compose.persistent.in.yaml"
QUIESCENCE = INSTALLATION / "legacy_quiescence_manifest.json"
SERVICE_ACCOUNT = INSTALLATION / "service-account.json"
SECRET_ROOT = INSTALLATION / "secrets"

SECRET_TEMPLATES = {
    "bootstrap.env.example": {
        "GOVERNED_MEMORY_BOOTSTRAP_PASSWORD",
        "GOVERNED_MEMORY_QDRANT_API_KEY",
    },
    "http.env.example": {
        "GOVERNED_MEMORY_HTTP_MODE",
        "GOVERNED_MEMORY_POSTGRES_DSN",
        "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN",
        "GOVERNED_MEMORY_CONVERSATION_BRIDGE_CATALOG_SHA256",
        "GOVERNED_MEMORY_SUPABASE_ISSUER",
        "GOVERNED_MEMORY_SUPABASE_JWKS_URL",
        "GOVERNED_MEMORY_SUPABASE_API_KEY",
        "GOVERNED_MEMORY_SERVICE_TOKEN",
    },
    "pilot.env.example": {
        "GOVERNED_MEMORY_EXPECTED_PILOT_ID",
        "GOVERNED_MEMORY_EXPECTED_PILOT_CONTRACT_SHA256",
        "GOVERNED_MEMORY_EXPECTED_AUTHORIZATION_RECEIPT_SHA256",
    },
    "worker.env.example": {
        "GOVERNED_MEMORY_WORKER_MODE",
        "GOVERNED_MEMORY_EXCLUSIVE_MODE",
        "GOVERNED_MEMORY_POSTGRES_DSN",
        "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN",
        "GOVERNED_MEMORY_OPENAI_API_KEY",
        "GOVERNED_MEMORY_OPENAI_EXTRACTION_MODEL",
        "GOVERNED_MEMORY_QDRANT_URL",
        "GOVERNED_MEMORY_QDRANT_API_KEY",
    },
}
PACKAGE_ARTIFACTS = {
    "governed-memory-migrations/0001_foundation/forward.pgsql",
    "governed-memory-migrations/0001_foundation/package.json",
    "governed-memory-migrations/0001_foundation/rollback.pgsql",
    "governed-memory-migrations/0002_conversation_bridge/forward.pgsql",
    "governed-memory-migrations/0002_conversation_bridge/package.json",
    "governed-memory-migrations/0002_conversation_bridge/rollback.pgsql",
    "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
    "governed-memory-migrations/0003_owner_claim_detail/package.json",
    "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
    "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
    "governed-memory-migrations/0004_pilot_marker/package.json",
    "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
    "governed-memory-migrations/manifest.json",
    "governed-memory-migrations/predicate_catalog.json",
    "governed-memory-migrations/roles_preflight.pgsql",
    "governed-memory-migrations/schema_contract.json",
    "ops/governed_memory/bootstrap_contract.json",
    "ops/governed_memory/build-requirements.lock",
    "ops/governed_memory/calibration/semantic_retrieval.unapproved.json",
    "ops/governed_memory/installation/compose.persistent.in.yaml",
    "ops/governed_memory/installation/contract.json",
    "ops/governed_memory/installation/legacy_quiescence_manifest.json",
    "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
    "ops/governed_memory/installation/postgres/source_cluster_roles.pgsql.in",
    "ops/governed_memory/installation/receipt.schema.json",
    "ops/governed_memory/installation/secrets/bootstrap.env.example",
    "ops/governed_memory/installation/secrets/http.env.example",
    "ops/governed_memory/installation/secrets/pilot.env.example",
    "ops/governed_memory/installation/secrets/worker.env.example",
    "ops/governed_memory/installation/service-account.json",
    "ops/governed_memory/phase6b_component_disposition.json",
    "ops/governed_memory/pilot_contract.json",
    "ops/governed_memory/qdrant_alias.create.json",
    "ops/governed_memory/qdrant_collection.create.json",
    "ops/governed_memory/runtime-requirements.lock",
    "ops/governed_memory/runtime_build_receipt.json",
    "ops/governed_memory/runtime_manifest.json",
    "ops/governed_memory/supabase_session_authority/contract.json",
    "ops/governed_memory/supabase_session_authority/forward.pgsql",
    "ops/governed_memory/supabase_session_authority/rollback.pgsql",
    "ops/governed_memory/systemd/governed-memory-http.service.in",
    "ops/governed_memory/systemd/governed-memory-worker.service.in",
    "tools/governed_memory_install/__init__.py",
    "tools/governed_memory_install/inactive_installation.py",
    "tools/governed_memory_validation/run_disposable_successor.sh",
    "tools/governed_memory_validation/verify_migration_manifest.py",
}
MEMORY_TIMER_STOP_SET = {
    "memory-v1-deferred-reconciliation-scan.timer",
    "memory-v1-evidence-intake-dispatcher.timer",
    "memory-v1-projection.timer",
    "memory-v1-v5-2-local-packet-router.timer",
    "memory-v1-v5-chat-capture.timer",
    "memory-v1-v5-local-auto-resolution.timer",
    "memory-v1-v5-local-auto-stage.timer",
    "memory-v1-v5-local-claim-projection.timer",
    "memory-v1-v5-local-legacy-reintake-audit.timer",
    "memory-v1-v5-local-packet-router.timer",
}

HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
OBSERVATION_KEYS = {
    "schema_version",
    "stage",
    "hostname",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "prior_decision_receipt_sha256",
    "target_states",
    "port_states",
    "service_account_state",
    "unit_states",
    "source_cluster_states",
    "successor_store_states",
    "pilot_ever_started",
    "successor_user_memory_row_count",
    "successor_projection_queue_row_count",
    "qdrant_point_count",
    "active_client_count",
    "legacy_import_count",
    "source_application_row_read_count",
}
STAGES = {
    "install_preflight",
    "install_postflight",
    "rollback_preflight",
    "rollback_postflight",
}
TARGET_STATE_PROFILES = {
    "install_preflight": {
        "postgres_container": "absent",
        "qdrant_container": "absent",
        "postgres_volume": "absent",
        "qdrant_volume": "absent",
        "network": "absent",
        "database": "absent",
        "collection": "absent",
        "alias": "absent",
        "install_root": "absent",
        "environment_root": "present_exact_root_0700",
        "http_unit": "absent",
        "worker_unit": "absent",
    },
    "install_postflight": {
        "postgres_container": "present_exact_owned",
        "qdrant_container": "present_exact_owned",
        "postgres_volume": "present_exact_owned",
        "qdrant_volume": "present_exact_owned",
        "network": "present_exact_owned",
        "database": "present_exact_empty",
        "collection": "present_exact_empty",
        "alias": "present_exact_binding",
        "install_root": "present_exact_owned",
        "environment_root": "present_exact_root_0700",
        "http_unit": "installed_disabled_inactive",
        "worker_unit": "installed_disabled_inactive",
    },
    "rollback_preflight": {
        "postgres_container": "present_exact_owned",
        "qdrant_container": "present_exact_owned",
        "postgres_volume": "present_exact_owned",
        "qdrant_volume": "present_exact_owned",
        "network": "present_exact_owned",
        "database": "present_exact_empty",
        "collection": "present_exact_empty",
        "alias": "present_exact_binding",
        "install_root": "present_exact_owned",
        "environment_root": "present_exact_root_0700",
        "http_unit": "installed_disabled_inactive",
        "worker_unit": "installed_disabled_inactive",
    },
    "rollback_postflight": {
        "postgres_container": "absent",
        "qdrant_container": "absent",
        "postgres_volume": "absent",
        "qdrant_volume": "absent",
        "network": "absent",
        "database": "absent",
        "collection": "absent",
        "alias": "absent",
        "install_root": "absent",
        "environment_root": "present_exact_root_0700",
        "http_unit": "absent",
        "worker_unit": "absent",
    },
}
PORT_STATE_PROFILES = {
    "install_preflight": {
        "http": "available",
        "postgresql": "available",
        "qdrant": "available",
    },
    "install_postflight": {
        "http": "available",
        "postgresql": "owned_by_exact_target",
        "qdrant": "owned_by_exact_target",
    },
    "rollback_preflight": {
        "http": "available",
        "postgresql": "owned_by_exact_target",
        "qdrant": "owned_by_exact_target",
    },
    "rollback_postflight": {
        "http": "available",
        "postgresql": "available",
        "qdrant": "available",
    },
}
ACCOUNT_STATE_PROFILES = {
    "install_preflight": "absent",
    "install_postflight": "present_exact_unprivileged",
    "rollback_preflight": "present_exact_unprivileged",
    "rollback_postflight": "retained_exact_unprivileged",
}
UNIT_STATE_PROFILES = {
    "install_preflight": "absent",
    "install_postflight": "installed_disabled_inactive",
    "rollback_preflight": "installed_disabled_inactive",
    "rollback_postflight": "absent",
}
SOURCE_CLUSTER_STATE_PROFILES = {
    "install_preflight": {
        "database": "memory_exact_catalog_only_preflight",
        "runtime_role_set": "all_four_absent_or_all_four_retained_exact_nologin_noinherit_no_partial_set",
        "conversation_bridge": "absent",
        "inactive_source_runtime_membership_graph": [],
        "brains_app_ingest_membership": "absent",
        "brains_app_erasure_membership": "absent",
        "api_ingest_membership": "absent",
        "api_erasure_membership": "absent",
        "worker_ingest_membership": "absent",
        "worker_erasure_membership": "absent",
        "direct_login_function_execute": "absent",
        "structured_lifeswitch_relation_privileges": "absent",
        "account_relation_privileges": "absent",
        "source_log_duration": "off",
        "source_log_parameter_max_length": "0_bind_logging_disabled",
        "source_logging_policy": "exact_bind_and_statement_logging_disabled_pgaudit_not_preloaded",
    },
    "install_postflight": {
        "database": "memory_exact_catalog_only_postflight",
        "governed_memory_api_role": "present_exact_nologin_noinherit",
        "governed_memory_worker_role": "present_exact_nologin_noinherit",
        "memory_ingest_writer_role": "present_exact_nologin_noinherit",
        "memory_erasure_requester_role": "present_exact_nologin_noinherit",
        "conversation_bridge": "installed_exact_inactive",
        "inactive_source_runtime_membership_graph": [],
        "brains_app_ingest_membership": "absent",
        "brains_app_erasure_membership": "absent",
        "api_ingest_membership": "absent",
        "api_erasure_membership": "absent",
        "worker_ingest_membership": "absent",
        "worker_erasure_membership": "absent",
        "direct_login_function_execute": "absent",
        "structured_lifeswitch_relation_privileges": "absent",
        "account_relation_privileges": "absent",
        "source_log_duration": "off",
        "source_log_parameter_max_length": "0_bind_logging_disabled",
        "source_logging_policy": "exact_bind_and_statement_logging_disabled_pgaudit_not_preloaded",
    },
    "rollback_preflight": {
        "database": "memory_exact_catalog_only_preflight",
        "governed_memory_api_role": "present_exact_nologin_noinherit",
        "governed_memory_worker_role": "present_exact_nologin_noinherit",
        "memory_ingest_writer_role": "present_exact_nologin_noinherit",
        "memory_erasure_requester_role": "present_exact_nologin_noinherit",
        "conversation_bridge": "installed_exact_inactive",
        "inactive_source_runtime_membership_graph": [],
        "brains_app_ingest_membership": "absent",
        "brains_app_erasure_membership": "absent",
        "api_ingest_membership": "absent",
        "api_erasure_membership": "absent",
        "worker_ingest_membership": "absent",
        "worker_erasure_membership": "absent",
        "direct_login_function_execute": "absent",
        "structured_lifeswitch_relation_privileges": "absent",
        "account_relation_privileges": "absent",
        "source_log_duration": "off",
        "source_log_parameter_max_length": "0_bind_logging_disabled",
        "source_logging_policy": "exact_bind_and_statement_logging_disabled_pgaudit_not_preloaded",
    },
    "rollback_postflight": {
        "database": "memory_exact_catalog_only_postflight",
        "governed_memory_api_role": "retained_exact_nologin_noinherit",
        "governed_memory_worker_role": "retained_exact_nologin_noinherit",
        "memory_ingest_writer_role": "retained_exact_nologin_noinherit",
        "memory_erasure_requester_role": "retained_exact_nologin_noinherit",
        "conversation_bridge": "absent_after_empty_only_rollback",
        "inactive_source_runtime_membership_graph": [],
        "brains_app_ingest_membership": "absent",
        "brains_app_erasure_membership": "absent",
        "api_ingest_membership": "absent",
        "api_erasure_membership": "absent",
        "worker_ingest_membership": "absent",
        "worker_erasure_membership": "absent",
        "direct_login_function_execute": "absent",
        "structured_lifeswitch_relation_privileges": "absent",
        "account_relation_privileges": "absent",
        "source_log_duration": "off",
        "source_log_parameter_max_length": "0_bind_logging_disabled",
        "source_logging_policy": "exact_bind_and_statement_logging_disabled_pgaudit_not_preloaded",
    },
}
SUCCESSOR_STORE_STATE_PROFILES = {
    "install_preflight": {
        "postgresql": "absent",
        "qdrant": "absent",
    },
    "install_postflight": {
        "postgresql": "present_exact_migrations_applied_no_user_memory_rows",
        "qdrant": "present_exact_collection_and_alias_zero_points",
    },
    "rollback_preflight": {
        "postgresql": "present_exact_migrations_applied_no_user_memory_rows",
        "qdrant": "present_exact_collection_and_alias_zero_points",
    },
    "rollback_postflight": {
        "postgresql": "absent",
        "qdrant": "absent",
    },
}
PRIOR_STAGE = {
    "install_preflight": None,
    "install_postflight": "install_preflight",
    "rollback_preflight": "install_postflight",
    "rollback_postflight": "rollback_preflight",
}
RECEIPT_CHAIN_STAGES = {
    "install_preflight": [],
    "install_postflight": ["install_preflight"],
    "rollback_preflight": ["install_preflight", "install_postflight"],
    "rollback_postflight": [
        "install_preflight",
        "install_postflight",
        "rollback_preflight",
    ],
}
STRUCTURAL_DECISION = "structurally_valid_candidate_not_authorization"
BASE_AUTHORITY_BLOCKERS = {
    "install_preflight": {
        "separate_future_installation_approval_required",
        "separate_sealed_release_receipt_required",
        "candidate_commit_and_tree_not_sealed_for_installation",
        "credential_rotation_authority_not_evaluated",
        "persistent_qdrant_digest_authority_not_evaluated",
        "observation_tool_cannot_authorize_execution",
    },
    "install_postflight": {
        "installation_authority_not_attested_by_observation_tool",
        "separate_sealed_release_receipt_required",
        "candidate_commit_and_tree_not_sealed_for_installation",
        "observation_tool_cannot_authorize_execution",
    },
    "rollback_preflight": {
        "separate_future_rollback_approval_required",
        "separate_sealed_release_receipt_required",
        "candidate_commit_and_tree_not_sealed_for_installation",
        "observation_tool_cannot_authorize_execution",
    },
    "rollback_postflight": {
        "rollback_authority_not_attested_by_observation_tool",
        "separate_sealed_release_receipt_required",
        "candidate_commit_and_tree_not_sealed_for_installation",
        "observation_tool_cannot_authorize_execution",
    },
}
ZERO_FRESH_STORE_COUNTS = {
    "successor_user_memory_row_count": 0,
    "successor_projection_queue_row_count": 0,
    "qdrant_point_count": 0,
    "active_client_count": 0,
    "legacy_import_count": 0,
    "source_application_row_read_count": 0,
}
INACTIVE_MIGRATION_EXECUTION_CONTRACT = {
    "psql_variable_name": "governed_memory_inactive_installation",
    "psql_variable_value": "on",
    "roles_preflight_pgsql_requires_variable": True,
    "conversation_bridge_forward_pgsql_requires_variable": True,
    "omission_defaults_to_active_mode_and_invalidates_inactive_installation": True,
    "evaluator_verifies_execution": False,
}
EXPECTED_SERVICE_ACCOUNT = {
    "schema_version": "governed-memory-service-account-contract-v1",
    "state": "candidate_only_account_not_created",
    "user": "governed-memory",
    "group": "governed-memory",
    "system_account": True,
    "home": "/var/lib/governed-memory",
    "home_mode": "0700",
    "shell": "/usr/sbin/nologin",
    "supplementary_groups": [],
    "docker_group_member": False,
    "sudo_rule": False,
    "environment_root": "/etc/governed-memory",
    "environment_root_owner": "root:root",
    "environment_root_mode": "0700",
    "secret_file_profiles": {
        "bootstrap.env": {
            "owner": "root:root",
            "mode": "0600",
            "service_account_direct_read": False,
        },
        "http.env": {
            "owner": "root:governed-memory",
            "mode": "0640",
            "loaded_by_unit": "governed-memory-http.service",
        },
        "worker.env": {
            "owner": "root:governed-memory",
            "mode": "0640",
            "loaded_by_unit": "governed-memory-worker.service",
        },
        "pilot.env": {
            "owner": "root:governed-memory",
            "mode": "0640",
            "loaded_by_unit": "governed-memory-worker.service",
        },
    },
    "install_root": "/opt/governed-memory",
    "release_owner": "root:root",
    "release_mode": "0755",
    "evaluator_state_changed": False,
}
DECISION_RECEIPT_KEYS = {
    "schema_version",
    "stage",
    "decision",
    "refusal_codes",
    "blockers",
    "authorization_inferred",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "prior_decision_receipt_sha256",
    "receipt_chain",
    "observation_canonical_sha256",
    "exact_target_states",
    "exact_source_cluster_states",
    "exact_successor_store_states",
    "inactive_migration_execution_contract",
    "fresh_store_counts",
    "pilot_ever_started",
    "evaluator_mutating_commands_executed",
    "evaluator_provider_calls",
    "evaluator_state_changed",
}


class InstallationPackageError(RuntimeError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstallationPackageError("installation_artifact_duplicate_key")
        result[key] = value
    return result


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise InstallationPackageError("installation_artifact_invalid") from error


def _parse_json_bytes(value: bytes) -> Any:
    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except InstallationPackageError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallationPackageError("installation_artifact_invalid") from error


def _load_json(path: Path) -> Any:
    return _parse_json_bytes(_read_bytes(path))


def _manifest_artifact_path(relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise InstallationPackageError("installation_manifest_path_invalid")
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or "\\" in relative
        or "//" in relative
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise InstallationPackageError("installation_manifest_path_invalid")
    path = ROOT / candidate
    try:
        resolved = path.resolve(strict=True)
        expected = ROOT.resolve(strict=True) / candidate
    except OSError as error:
        raise InstallationPackageError("installation_artifact_missing") from error
    if resolved != expected or not resolved.is_file():
        raise InstallationPackageError("installation_manifest_path_invalid")
    return resolved


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def verify_package() -> dict[str, object]:
    manifest_bytes = _read_bytes(PACKAGE_MANIFEST)
    manifest = _parse_json_bytes(manifest_bytes)
    if not isinstance(manifest, dict):
        raise InstallationPackageError("installation_artifact_invalid")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema_version")
        != "governed-memory-inactive-installation-package-manifest-v1"
        or manifest.get("state") != "candidate_only_not_installed_not_authorized"
        or set(manifest) != {"schema_version", "state", "artifacts"}
        or not isinstance(artifacts, dict)
        or set(artifacts) != PACKAGE_ARTIFACTS
        or any(not _valid_hash(value) for value in artifacts.values())
    ):
        raise InstallationPackageError("installation_manifest_invalid")
    observed: dict[str, str] = {}
    artifact_bytes: dict[str, bytes] = {}
    for relative, expected in sorted(artifacts.items()):
        path = _manifest_artifact_path(relative)
        content = _read_bytes(path)
        actual = _sha256_bytes(content)
        if actual != expected:
            raise InstallationPackageError("installation_artifact_hash_mismatch")
        observed[relative] = actual
        artifact_bytes[relative] = content

    contract = _parse_json_bytes(
        artifact_bytes["ops/governed_memory/installation/contract.json"]
    )
    quiescence = _parse_json_bytes(
        artifact_bytes[
            "ops/governed_memory/installation/legacy_quiescence_manifest.json"
        ]
    )
    account = _parse_json_bytes(
        artifact_bytes["ops/governed_memory/installation/service-account.json"]
    )
    if not all(
        isinstance(value, dict) for value in (contract, quiescence, account)
    ):
        raise InstallationPackageError("installation_artifact_invalid")
    if (
        contract.get("schema_version")
        != "governed-memory-inactive-installation-contract-v1"
        or contract.get("state") != "candidate_only_not_installed_not_authorized"
        or contract.get("evaluator_state_changed") is not False
        or contract.get("inactive_migration_execution_contract")
        != INACTIVE_MIGRATION_EXECUTION_CONTRACT
        or contract.get("tooling")
        != {
            "mode": "offline_package_and_observation_validation_only",
            "executes_commands": False,
            "creates_resources": False,
            "changes_services": False,
            "changes_databases": False,
            "changes_qdrant": False,
            "can_authorize_installation": False,
            "can_authorize_rollback": False,
        }
        or contract.get("images", {}).get("postgresql", {}).get(
            "installation_authorized"
        )
        is not False
        or contract.get("images", {}).get("qdrant", {}).get(
            "approved_for_persistent_installation"
        )
        is not False
        or contract.get("images", {}).get("qdrant", {}).get("typed_blocker")
        != "persistent_qdrant_digest_not_authorized"
        or contract.get("source_cluster_policy", {}).get(
            "inactive_source_runtime_membership_graph"
        )
        != []
    ):
        raise InstallationPackageError("installation_contract_invalid")

    try:
        compose = artifact_bytes[
            "ops/governed_memory/installation/compose.persistent.in.yaml"
        ].decode("utf-8")
    except UnicodeError as error:
        raise InstallationPackageError("installation_compose_invalid") from error
    for image in (
        contract["images"]["postgresql"]["reference"],
        contract["images"]["qdrant"]["reference"],
    ):
        if image not in compose or "@sha256:" not in image:
            raise InstallationPackageError("installation_image_not_pinned")
    if ":latest" in compose or "restart: \"no\"" not in compose:
        raise InstallationPackageError("installation_compose_invalid")

    try:
        secret_paths = sorted(SECRET_ROOT.iterdir())
    except OSError as error:
        raise InstallationPackageError(
            "installation_secret_template_set_invalid"
        ) from error
    if (
        {path.name for path in secret_paths} != set(SECRET_TEMPLATES)
        or any(not path.is_file() or path.is_symlink() for path in secret_paths)
    ):
        raise InstallationPackageError("installation_secret_template_set_invalid")
    for path in secret_paths:
        assignments: list[str] = []
        relative = "ops/governed_memory/installation/secrets/" + path.name
        try:
            lines = artifact_bytes[relative].decode("utf-8").splitlines()
        except UnicodeError as error:
            raise InstallationPackageError(
                "installation_secret_template_keys_invalid"
            ) from error
        for line in lines:
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
            if match is None or match.group(2):
                raise InstallationPackageError("installation_secret_template_not_empty")
            assignments.append(match.group(1))
        if (
            set(assignments) != SECRET_TEMPLATES[path.name]
            or len(assignments) != len(SECRET_TEMPLATES[path.name])
        ):
            raise InstallationPackageError("installation_secret_template_keys_invalid")

    timers = quiescence.get("memory_timer_stop_set")
    if (
        not isinstance(timers, list)
        or len(timers) != len(MEMORY_TIMER_STOP_SET)
        or any(not isinstance(item, dict) for item in timers)
        or {item.get("timer") for item in timers} != MEMORY_TIMER_STOP_SET
        or quiescence.get("quiescence_performed") is not False
        or quiescence.get("production_state_changed") is not False
    ):
        raise InstallationPackageError("installation_quiescence_manifest_invalid")
    preserve = quiescence.get("inventory_preserve_set")
    if (
        not isinstance(preserve, list)
        or len(preserve) != 1
        or preserve[0].get("timer") != "chat-memory-git-sync.timer"
        or preserve[0].get("action") != "preserve_unless_separately_authorized"
    ):
        raise InstallationPackageError("installation_quiescence_manifest_invalid")
    if account != EXPECTED_SERVICE_ACCOUNT:
        raise InstallationPackageError("installation_service_account_invalid")
    return {
        "schema_version": "governed-memory-installation-package-verification-v1",
        "artifact_sha256": observed,
        "package_manifest_sha256": _sha256_bytes(manifest_bytes),
        "evaluator_mutating_commands_executed": 0,
        "evaluator_provider_calls": 0,
        "evaluator_state_changed": False,
    }


def _validate_prior_receipts(
    *,
    stage: str,
    document: Mapping[str, object],
    prior_receipt_paths: list[Path],
    candidate_git_commit: str,
    candidate_git_tree: str,
    package_manifest_sha256: str,
) -> tuple[list[str], list[dict[str, str]]]:
    expected_stages = RECEIPT_CHAIN_STAGES[stage]
    declared_hash = document.get("prior_decision_receipt_sha256")
    if not expected_stages:
        if declared_hash is not None or prior_receipt_paths:
            return ["unexpected_prior_decision_receipt"], []
        return [], []
    if not _valid_hash(declared_hash) or not prior_receipt_paths:
        return ["prior_decision_receipt_missing"], []
    if len(prior_receipt_paths) != len(expected_stages):
        return ["prior_decision_receipt_chain_incomplete"], []
    try:
        contents = [_read_bytes(path) for path in prior_receipt_paths]
        prior_receipts = [_parse_json_bytes(value) for value in contents]
        actual_hashes = [
            _sha256_bytes(_canonical_bytes(value)) for value in prior_receipts
        ]
    except InstallationPackageError:
        return ["prior_decision_receipt_invalid"], []
    if actual_hashes[-1] != declared_hash:
        return ["prior_decision_receipt_hash_mismatch"], []
    chain: list[dict[str, str]] = []
    for index, (expected_stage, prior) in enumerate(
        zip(expected_stages, prior_receipts, strict=True)
    ):
        expected_prior_hash = None if index == 0 else actual_hashes[index - 1]
        if (
            not isinstance(prior, dict)
            or set(prior) != DECISION_RECEIPT_KEYS
            or prior.get("schema_version")
            != "governed-memory-installation-decision-receipt-v1"
            or prior.get("stage") != expected_stage
            or prior.get("decision") != STRUCTURAL_DECISION
            or prior.get("refusal_codes") != []
            or prior.get("blockers")
            != sorted(BASE_AUTHORITY_BLOCKERS[expected_stage])
            or prior.get("authorization_inferred") is not False
            or prior.get("candidate_git_commit") != candidate_git_commit
            or prior.get("candidate_git_tree") != candidate_git_tree
            or prior.get("package_manifest_sha256") != package_manifest_sha256
            or prior.get("prior_decision_receipt_sha256")
            != expected_prior_hash
            or prior.get("receipt_chain") != chain
            or not _valid_hash(prior.get("observation_canonical_sha256"))
            or prior.get("exact_target_states")
            != TARGET_STATE_PROFILES[expected_stage]
            or prior.get("exact_source_cluster_states")
            != SOURCE_CLUSTER_STATE_PROFILES[expected_stage]
            or prior.get("exact_successor_store_states")
            != SUCCESSOR_STORE_STATE_PROFILES[expected_stage]
            or prior.get("inactive_migration_execution_contract")
            != INACTIVE_MIGRATION_EXECUTION_CONTRACT
            or prior.get("fresh_store_counts") != ZERO_FRESH_STORE_COUNTS
            or prior.get("pilot_ever_started") is not False
            or prior.get("evaluator_mutating_commands_executed") != 0
            or prior.get("evaluator_provider_calls") != 0
            or prior.get("evaluator_state_changed") is not False
        ):
            return ["prior_decision_receipt_invalid"], []
        chain = [
            *chain,
            {"stage": expected_stage, "receipt_sha256": actual_hashes[index]},
        ]
    return [], chain


def evaluate_observation(
    document: object,
    *,
    expected_candidate_git_commit: str,
    expected_candidate_git_tree: str,
    prior_receipt_paths: list[Path] | None = None,
) -> dict[str, object]:
    verification = verify_package()
    if not isinstance(document, dict) or set(document) != OBSERVATION_KEYS:
        raise InstallationPackageError("installation_observation_invalid")
    stage = document.get("stage")
    if (
        document.get("schema_version")
        != "governed-memory-installation-observation-v1"
        or stage not in STAGES
        or document.get("hostname") != "ip-172-31-32-171"
        or not isinstance(expected_candidate_git_commit, str)
        or not isinstance(expected_candidate_git_tree, str)
        or COMMIT_RE.fullmatch(expected_candidate_git_commit) is None
        or COMMIT_RE.fullmatch(expected_candidate_git_tree) is None
    ):
        raise InstallationPackageError("installation_observation_invalid")
    assert isinstance(stage, str)
    refusals: list[str] = []
    if document.get("package_manifest_sha256") != verification[
        "package_manifest_sha256"
    ]:
        refusals.append("package_manifest_mismatch")
    if document.get("candidate_git_commit") != expected_candidate_git_commit:
        refusals.append("candidate_git_commit_mismatch")
    if document.get("candidate_git_tree") != expected_candidate_git_tree:
        refusals.append("candidate_git_tree_mismatch")
    if document.get("target_states") != TARGET_STATE_PROFILES[stage]:
        refusals.append("exact_target_state_mismatch")
    if document.get("port_states") != PORT_STATE_PROFILES[stage]:
        refusals.append("exact_port_state_mismatch")
    if document.get("service_account_state") != ACCOUNT_STATE_PROFILES[stage]:
        refusals.append("service_account_state_mismatch")
    if document.get("unit_states") != UNIT_STATE_PROFILES[stage]:
        refusals.append("unit_state_mismatch")
    if document.get("source_cluster_states") != SOURCE_CLUSTER_STATE_PROFILES[stage]:
        refusals.append("source_cluster_state_mismatch")
    if document.get("successor_store_states") != SUCCESSOR_STORE_STATE_PROFILES[stage]:
        refusals.append("successor_store_state_mismatch")
    for field in (
        "successor_user_memory_row_count",
        "successor_projection_queue_row_count",
        "qdrant_point_count",
        "active_client_count",
        "legacy_import_count",
        "source_application_row_read_count",
    ):
        if type(document.get(field)) is not int or document[field] != 0:
            refusals.append(field + "_not_zero")
    if document.get("pilot_ever_started") is not False:
        refusals.append("pilot_already_started")
    prior_refusals, receipt_chain = _validate_prior_receipts(
        stage=stage,
        document=document,
        prior_receipt_paths=prior_receipt_paths or [],
        candidate_git_commit=expected_candidate_git_commit,
        candidate_git_tree=expected_candidate_git_tree,
        package_manifest_sha256=verification["package_manifest_sha256"],
    )
    refusals.extend(prior_refusals)
    refusal_codes = sorted(set(refusals))
    decision = "refuse" if refusal_codes else STRUCTURAL_DECISION
    blockers = sorted(BASE_AUTHORITY_BLOCKERS[stage] | set(refusal_codes))
    return {
        "schema_version": "governed-memory-installation-decision-receipt-v1",
        "stage": stage,
        "decision": decision,
        "refusal_codes": refusal_codes,
        "blockers": blockers,
        "authorization_inferred": False,
        "candidate_git_commit": expected_candidate_git_commit,
        "candidate_git_tree": expected_candidate_git_tree,
        "package_manifest_sha256": verification["package_manifest_sha256"],
        "prior_decision_receipt_sha256": document[
            "prior_decision_receipt_sha256"
        ],
        "receipt_chain": receipt_chain,
        "observation_canonical_sha256": hashlib.sha256(
            _canonical_bytes(document)
        ).hexdigest(),
        "exact_target_states": document["target_states"],
        "exact_source_cluster_states": document["source_cluster_states"],
        "exact_successor_store_states": document["successor_store_states"],
        "inactive_migration_execution_contract": (
            INACTIVE_MIGRATION_EXECUTION_CONTRACT
        ),
        "fresh_store_counts": {
            field: document[field] for field in ZERO_FRESH_STORE_COUNTS
        },
        "pilot_ever_started": document["pilot_ever_started"],
        "evaluator_mutating_commands_executed": 0,
        "evaluator_provider_calls": 0,
        "evaluator_state_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="governed-memory-inactive-installation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-package")
    evaluate = subparsers.add_parser("evaluate-observation")
    evaluate.add_argument("observation", type=Path)
    evaluate.add_argument("--expected-candidate-git-commit", required=True)
    evaluate.add_argument("--expected-candidate-git-tree", required=True)
    evaluate.add_argument(
        "--prior-decision-receipt", type=Path, action="append", default=[]
    )
    args = parser.parse_args(argv)
    if args.command == "verify-package":
        result = verify_package()
        exit_code = 0
    else:
        result = evaluate_observation(
            _load_json(args.observation),
            expected_candidate_git_commit=args.expected_candidate_git_commit,
            expected_candidate_git_tree=args.expected_candidate_git_tree,
            prior_receipt_paths=args.prior_decision_receipt,
        )
        exit_code = 2 if result.get("decision") == "refuse" else 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
