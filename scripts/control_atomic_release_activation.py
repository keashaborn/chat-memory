#!/usr/bin/env python3
from __future__ import annotations

"""Fail-closed authorization and plan gate for paired production activation.

This controller does not mutate either server.  It converts a verified release
package plus a separate root-owned authorization receipt into one exact,
ordered activation and rollback plan for the production executor.
"""

import argparse
import hashlib
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from scripts.verify_atomic_release_package import (
        ReleasePackageError,
        verify_release_package,
    )
else:
    from verify_atomic_release_package import (  # type: ignore[no-redef]
        ReleasePackageError,
        verify_release_package,
    )


AUTHORIZATION_SCHEMA = "seebx-atomic-release-authorization-v1"
PLAN_SCHEMA = "seebx-atomic-release-activation-plan-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

APPROVED_OPERATIONS = (
    "quiesce_frontend_backend",
    "apply_bound_database_migrations",
    "install_bound_backend_source_runtime_and_service",
    "install_bound_frontend_source_build_and_service",
    "restart_and_verify_paired_services",
    "automatic_reverse_order_rollback_on_failure",
)

FORWARD_STEPS = (
    ("verify_live_baseline", "control", "read_only"),
    ("capture_fresh_rollback_evidence", "control", "read_only"),
    ("stop_frontend_service", "verbalsage", "write"),
    ("stop_backend_service", "seebx", "write"),
    ("prove_paired_quiescence", "control", "read_only"),
    ("apply_bound_forward_migrations", "seebx", "write"),
    ("install_bound_backend_source", "seebx", "write"),
    ("install_bound_backend_runtime", "seebx", "write"),
    ("install_and_preflight_backend_service", "seebx", "write"),
    ("install_bound_frontend_source_and_build", "verbalsage", "write"),
    ("install_and_preflight_frontend_service", "verbalsage", "write"),
    ("start_and_verify_backend", "seebx", "write"),
    ("verify_migration_and_empty_initial_outbox", "seebx", "read_only"),
    ("start_and_verify_frontend", "verbalsage", "write"),
    ("verify_authenticated_full_stack_behavior", "control", "read_only"),
    ("write_activation_receipt", "control", "write"),
)

ROLLBACK_STEPS = (
    ("stop_frontend_service", "verbalsage", "write"),
    ("stop_backend_service", "seebx", "write"),
    ("restore_frontend_service_build_and_source", "verbalsage", "write"),
    ("restore_backend_service_runtime_and_source", "seebx", "write"),
    ("prove_zep_outbox_empty", "seebx", "read_only"),
    ("apply_bound_database_rollback", "seebx", "write"),
    ("start_and_verify_backend_baseline", "seebx", "write"),
    ("start_and_verify_frontend_baseline", "verbalsage", "write"),
    ("write_rollback_receipt", "control", "write"),
)


class ActivationControlError(RuntimeError):
    pass


def _canonical_sha256(value: Any) -> str:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_object(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ActivationControlError(reason) from error
    if not isinstance(value, dict):
        raise ActivationControlError(reason)
    return value


def _parse_utc(value: object, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ActivationControlError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ActivationControlError(reason) from error
    if parsed.tzinfo != timezone.utc:
        raise ActivationControlError(reason)
    return parsed


def verify_authorization_file(path: Path) -> None:
    try:
        item = path.lstat()
    except OSError as error:
        raise ActivationControlError("authorization_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise ActivationControlError("authorization_type_invalid")
    if item.st_uid != 0:
        raise ActivationControlError("authorization_owner_invalid")
    if stat.S_IMODE(item.st_mode) not in {0o400, 0o600}:
        raise ActivationControlError("authorization_mode_invalid")


def validate_authorization(
    authorization: Mapping[str, Any],
    *,
    package: Mapping[str, Any],
    package_sha256: str,
    now: datetime | None = None,
) -> None:
    expected_keys = {
        "schema_version",
        "authorization_id",
        "decision",
        "package_id",
        "package_sha256",
        "backend_commit",
        "frontend_commit",
        "approved_operations",
        "quiescence_approved",
        "issued_at_utc",
        "expires_at_utc",
    }
    if set(authorization) != expected_keys:
        raise ActivationControlError("authorization_fields_invalid")
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA:
        raise ActivationControlError("authorization_schema_invalid")
    if authorization["decision"] != "approved":
        raise ActivationControlError("authorization_not_approved")
    identifier = authorization["authorization_id"]
    if not isinstance(identifier, str) or not AUTHORIZATION_ID.fullmatch(identifier):
        raise ActivationControlError("authorization_id_invalid")
    if authorization["package_id"] != package.get("package_id"):
        raise ActivationControlError("authorization_package_id_mismatch")
    if not HEX64.fullmatch(str(authorization["package_sha256"])):
        raise ActivationControlError("authorization_package_sha_invalid")
    if authorization["package_sha256"] != package_sha256:
        raise ActivationControlError("authorization_package_sha_mismatch")
    sources = package.get("sources") or {}
    backend = (sources.get("backend") or {}).get("candidate_commit")
    frontend = (sources.get("frontend") or {}).get("candidate_commit")
    if not isinstance(backend, str) or not HEX40.fullmatch(backend):
        raise ActivationControlError("package_backend_commit_invalid")
    if not isinstance(frontend, str) or not HEX40.fullmatch(frontend):
        raise ActivationControlError("package_frontend_commit_invalid")
    if authorization["backend_commit"] != backend:
        raise ActivationControlError("authorization_backend_commit_mismatch")
    if authorization["frontend_commit"] != frontend:
        raise ActivationControlError("authorization_frontend_commit_mismatch")
    if authorization["approved_operations"] != list(APPROVED_OPERATIONS):
        raise ActivationControlError("authorization_operations_invalid")
    if authorization["quiescence_approved"] is not True:
        raise ActivationControlError("authorization_quiescence_missing")
    issued = _parse_utc(authorization["issued_at_utc"], "authorization_issued_at_invalid")
    expires = _parse_utc(authorization["expires_at_utc"], "authorization_expires_at_invalid")
    current = now or datetime.now(timezone.utc)
    if issued > current or expires <= issued or current >= expires:
        raise ActivationControlError("authorization_time_window_invalid")


def build_activation_plan(
    package: Mapping[str, Any],
    verification: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    if verification.get("status") != "pass" or verification.get("package_integrity_ready") is not True:
        raise ActivationControlError("release_verification_not_ready")
    if verification.get("production_activation_authorized") is not False:
        raise ActivationControlError("release_package_self_authorized")
    if package.get("authority") != {
        "production_activation_authorized": False,
        "authorization_id": None,
    }:
        raise ActivationControlError("release_package_authority_boundary_invalid")
    if package.get("quiescence", {}).get("approved") is not False:
        raise ActivationControlError("release_package_quiescence_boundary_invalid")
    bindings = {
        "package_id": package["package_id"],
        "package_sha256": verification["package_sha256"],
        "authorization_id": authorization["authorization_id"],
        "backend_commit": verification["backend_commit"],
        "frontend_commit": verification["frontend_commit"],
        "runtime_archive_sha256": verification["runtime_archive_sha256"],
        "database_backup_sha256": verification["database_backup_sha256"],
        "migration_package_sha256": verification["migration_package_sha256"],
    }
    forward = [
        {"order": index, "action": action, "server": server, "effect": effect}
        for index, (action, server, effect) in enumerate(FORWARD_STEPS, start=1)
    ]
    rollback = [
        {"order": index, "action": action, "server": server, "effect": effect}
        for index, (action, server, effect) in enumerate(ROLLBACK_STEPS, start=1)
    ]
    plan = {
        "schema_version": PLAN_SCHEMA,
        "status": "authorized_not_executed",
        "production_mutated": False,
        "bindings": bindings,
        "forward": forward,
        "rollback_on_any_failure": rollback,
        "database_rollback_gate": "zep_outbox_empty",
    }
    plan["plan_sha256"] = _canonical_sha256(plan)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        verify_authorization_file(arguments.authorization)
        package = _read_object(arguments.package, "package_invalid")
        schema = _read_object(arguments.schema, "schema_invalid")
        authorization = _read_object(arguments.authorization, "authorization_invalid")
        verification = verify_release_package(
            package,
            artifact_root=arguments.artifact_root,
            schema=schema,
        )
        validate_authorization(
            authorization,
            package=package,
            package_sha256=verification["package_sha256"],
        )
        result = build_activation_plan(package, verification, authorization)
        exit_code = 0
    except (ActivationControlError, ReleasePackageError) as error:
        result = {
            "schema_version": PLAN_SCHEMA,
            "status": "error",
            "production_mutated": False,
            "error": "activation_not_authorized",
            "reason": str(error),
        }
        exit_code = 2
    except Exception:
        result = {
            "schema_version": PLAN_SCHEMA,
            "status": "error",
            "production_mutated": False,
            "error": "activation_control_unexpected_error",
        }
        exit_code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
