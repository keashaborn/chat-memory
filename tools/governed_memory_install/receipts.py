from __future__ import annotations

"""Closed, content-free receipts for dormant store install and empty rollback.

This module is serialization only.  It has no command, filesystem, secret,
network, database, Qdrant, service, installation, or activation surface.
"""

import hashlib
import json
import re
from typing import Final, Mapping

from .rollback import ROLLBACK_RESOURCE_KEYS


INSTALL_RECEIPT_SCHEMA: Final = "governed-memory-dormant-store-install-receipt-v1"
ROLLBACK_RECEIPT_SCHEMA: Final = "governed-memory-empty-store-rollback-receipt-v3"
INSTALL_OPERATION: Final = "dormant_store_install"
ROLLBACK_OPERATION: Final = "empty_store_rollback"
INSTALL_RESULT: Final = "dormant_store_installation_complete_inactive"
ROLLBACK_RESULT: Final = "empty_store_rollback_complete"

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_EXECUTION_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_INSTALL_ATTEMPT_RE = re.compile(r"install-[0-9a-f]{40}\Z", re.ASCII)
_ROLLBACK_ATTEMPT_RE = re.compile(r"rollback-[0-9a-f]{40}\Z", re.ASCII)

_COMMON_KEYS = {
    "schema_version",
    "operation",
    "result",
    "execution_id",
    "attempt_id",
    "candidate_git_commit",
    "candidate_git_tree",
    "package_manifest_sha256",
    "authorization_sha256",
    "plan_sha256",
    "journal_head_sha256",
    "journal_sequence",
    "resource_ledger_head_sha256",
    "resource_ledger_sequence",
    "source_postgres_read_count",
    "source_postgres_write_count",
    "production_data_read",
    "provider_calls",
    "application_services_installed",
    "activation_performed",
    "receipt_sha256",
}
_RUNTIME_IDENTITY_KEYS = {
    "controller_runtime_receipt_sha256",
    "controller_runtime_root",
    "controller_runtime_tree_sha256",
    "controller_release_root",
    "controller_release_tree_sha256",
    "controller_release_package_manifest_path",
    "controller_runtime_interpreter_path",
    "controller_runtime_interpreter_sha256",
    "controller_runtime_inventory_path",
    "controller_runtime_inventory_sha256",
    "controller_requirements_lock_sha256",
    "supervisor_launcher_path",
    "supervisor_launcher_sha256",
}
_INSTALL_KEYS = _COMMON_KEYS | _RUNTIME_IDENTITY_KEYS | {
    "terminal_store_readiness_sha256",
    "image_identity_set_sha256",
    "postflight_receipt_sha256",
    "fresh_store_secrets_generated",
    "stores_installed",
    "stores_supervisor_installed",
}
_ROLLBACK_KEYS = _COMMON_KEYS | _RUNTIME_IDENTITY_KEYS | {
    "installation_receipt_sha256",
    "eligibility_receipt_sha256",
    "retained_audit_set_sha256",
    "exact_targets_absent_count",
    "exact_resources_absent",
    "stores_installed",
    "stores_supervisor_installed",
}


class ReceiptError(ValueError):
    """Content-free receipt refusal."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ReceiptError("installation_receipt_json_invalid") from error


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _unsigned(receipt: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in receipt.items() if key != "receipt_sha256"}


def receipt_sha256(receipt: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(_unsigned(receipt))).hexdigest()


def _validate_common(
    receipt: Mapping[str, object],
    *,
    attempt_re: re.Pattern[str],
) -> None:
    if (
        not isinstance(receipt.get("execution_id"), str)
        or _EXECUTION_RE.fullmatch(str(receipt["execution_id"])) is None
        or not isinstance(receipt.get("attempt_id"), str)
        or attempt_re.fullmatch(str(receipt["attempt_id"])) is None
        or not isinstance(receipt.get("candidate_git_commit"), str)
        or _COMMIT_RE.fullmatch(str(receipt["candidate_git_commit"])) is None
        or not isinstance(receipt.get("candidate_git_tree"), str)
        or _COMMIT_RE.fullmatch(str(receipt["candidate_git_tree"])) is None
    ):
        raise ReceiptError("installation_receipt_identity_invalid")
    for key in (
        "package_manifest_sha256",
        "authorization_sha256",
        "plan_sha256",
        "journal_head_sha256",
        "resource_ledger_head_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise ReceiptError("installation_receipt_hash_invalid")
    for key in ("journal_sequence", "resource_ledger_sequence"):
        value = receipt.get(key)
        if type(value) is not int or value < 1:
            raise ReceiptError("installation_receipt_sequence_invalid")
    exact_zero = (
        "source_postgres_read_count",
        "source_postgres_write_count",
        "provider_calls",
    )
    if any(
        type(receipt.get(key)) is not int or receipt.get(key) != 0
        for key in exact_zero
    ):
        raise ReceiptError("installation_receipt_forbidden_effect")
    exact_false = (
        "production_data_read",
        "application_services_installed",
        "activation_performed",
    )
    if any(receipt.get(key) is not False for key in exact_false):
        raise ReceiptError("installation_receipt_forbidden_effect")
    if (
        not _is_hash(receipt.get("receipt_sha256"))
        or receipt["receipt_sha256"] != receipt_sha256(receipt)
    ):
        raise ReceiptError("installation_receipt_digest_invalid")


def _validate_runtime_identity(receipt: Mapping[str, object]) -> None:
    for key in (
        "controller_runtime_receipt_sha256",
        "controller_runtime_tree_sha256",
        "controller_release_tree_sha256",
        "controller_runtime_interpreter_sha256",
        "controller_runtime_inventory_sha256",
        "controller_requirements_lock_sha256",
        "supervisor_launcher_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise ReceiptError("installation_receipt_hash_invalid")
    runtime_receipt_sha256 = str(
        receipt["controller_runtime_receipt_sha256"]
    )
    package_manifest_sha256 = str(receipt["package_manifest_sha256"])
    runtime_root = (
        "/opt/governed-memory-controller/runtimes/"
        + runtime_receipt_sha256
    )
    release_root = (
        "/opt/governed-memory-controller/releases/"
        + package_manifest_sha256
    )
    if (
        receipt.get("controller_runtime_root") != runtime_root
        or receipt.get("controller_release_root") != release_root
        or receipt.get("controller_release_package_manifest_path")
        != release_root
        + "/ops/governed_memory/installation/current/package_manifest.json"
        or receipt.get("controller_runtime_interpreter_path")
        != runtime_root + "/bin/python"
        or receipt.get("controller_runtime_inventory_path")
        != runtime_root + "/controller-distributions.json"
        or receipt.get("supervisor_launcher_path")
        != release_root
        + "/tools/governed_memory_install/store_supervisor_launcher.py"
    ):
        raise ReceiptError("installation_receipt_runtime_identity_invalid")


def build_install_receipt(
    *,
    execution_id: str,
    attempt_id: str,
    candidate_git_commit: str,
    candidate_git_tree: str,
    package_manifest_sha256: str,
    controller_runtime_receipt_sha256: str,
    controller_runtime_root: str,
    controller_runtime_tree_sha256: str,
    controller_release_root: str,
    controller_release_tree_sha256: str,
    controller_release_package_manifest_path: str,
    controller_runtime_interpreter_path: str,
    controller_runtime_interpreter_sha256: str,
    controller_runtime_inventory_path: str,
    controller_runtime_inventory_sha256: str,
    controller_requirements_lock_sha256: str,
    supervisor_launcher_path: str,
    supervisor_launcher_sha256: str,
    authorization_sha256: str,
    plan_sha256: str,
    journal_head_sha256: str,
    journal_sequence: int,
    resource_ledger_head_sha256: str,
    resource_ledger_sequence: int,
    image_identity_set_sha256: str,
    postflight_receipt_sha256: str,
    terminal_store_readiness_sha256: str,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": INSTALL_RECEIPT_SCHEMA,
        "operation": INSTALL_OPERATION,
        "result": INSTALL_RESULT,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "candidate_git_commit": candidate_git_commit,
        "candidate_git_tree": candidate_git_tree,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_receipt_sha256": controller_runtime_receipt_sha256,
        "controller_runtime_root": controller_runtime_root,
        "controller_runtime_tree_sha256": controller_runtime_tree_sha256,
        "controller_release_root": controller_release_root,
        "controller_release_tree_sha256": controller_release_tree_sha256,
        "controller_release_package_manifest_path": (
            controller_release_package_manifest_path
        ),
        "controller_runtime_interpreter_path": (
            controller_runtime_interpreter_path
        ),
        "controller_runtime_interpreter_sha256": (
            controller_runtime_interpreter_sha256
        ),
        "controller_runtime_inventory_path": controller_runtime_inventory_path,
        "controller_runtime_inventory_sha256": (
            controller_runtime_inventory_sha256
        ),
        "controller_requirements_lock_sha256": (
            controller_requirements_lock_sha256
        ),
        "supervisor_launcher_path": supervisor_launcher_path,
        "supervisor_launcher_sha256": supervisor_launcher_sha256,
        "authorization_sha256": authorization_sha256,
        "plan_sha256": plan_sha256,
        "journal_head_sha256": journal_head_sha256,
        "journal_sequence": journal_sequence,
        "resource_ledger_head_sha256": resource_ledger_head_sha256,
        "resource_ledger_sequence": resource_ledger_sequence,
        "image_identity_set_sha256": image_identity_set_sha256,
        "postflight_receipt_sha256": postflight_receipt_sha256,
        "terminal_store_readiness_sha256": terminal_store_readiness_sha256,
        "fresh_store_secrets_generated": True,
        "stores_installed": True,
        "stores_supervisor_installed": True,
        "source_postgres_read_count": 0,
        "source_postgres_write_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "application_services_installed": False,
        "activation_performed": False,
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    return verify_install_receipt(receipt)


def verify_install_receipt(value: Mapping[str, object]) -> dict[str, object]:
    receipt = dict(value)
    if (
        set(receipt) != _INSTALL_KEYS
        or receipt.get("schema_version") != INSTALL_RECEIPT_SCHEMA
        or receipt.get("operation") != INSTALL_OPERATION
        or receipt.get("result") != INSTALL_RESULT
        or any(
            receipt.get(key) is not True
            for key in (
                "fresh_store_secrets_generated",
                "stores_installed",
                "stores_supervisor_installed",
            )
        )
    ):
        raise ReceiptError("installation_receipt_shape_invalid")
    for key in (
        "terminal_store_readiness_sha256",
        "image_identity_set_sha256",
        "postflight_receipt_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise ReceiptError("installation_receipt_hash_invalid")
    _validate_runtime_identity(receipt)
    _validate_common(receipt, attempt_re=_INSTALL_ATTEMPT_RE)
    return receipt


def build_empty_rollback_receipt(
    *,
    execution_id: str,
    attempt_id: str,
    candidate_git_commit: str,
    candidate_git_tree: str,
    package_manifest_sha256: str,
    controller_runtime_receipt_sha256: str,
    controller_runtime_root: str,
    controller_runtime_tree_sha256: str,
    controller_release_root: str,
    controller_release_tree_sha256: str,
    controller_release_package_manifest_path: str,
    controller_runtime_interpreter_path: str,
    controller_runtime_interpreter_sha256: str,
    controller_runtime_inventory_path: str,
    controller_runtime_inventory_sha256: str,
    controller_requirements_lock_sha256: str,
    supervisor_launcher_path: str,
    supervisor_launcher_sha256: str,
    installation_receipt_sha256: str,
    authorization_sha256: str,
    plan_sha256: str,
    journal_head_sha256: str,
    journal_sequence: int,
    resource_ledger_head_sha256: str,
    resource_ledger_sequence: int,
    eligibility_receipt_sha256: str,
    retained_audit_set_sha256: str,
    exact_targets_absent_count: int,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": ROLLBACK_RECEIPT_SCHEMA,
        "operation": ROLLBACK_OPERATION,
        "result": ROLLBACK_RESULT,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "candidate_git_commit": candidate_git_commit,
        "candidate_git_tree": candidate_git_tree,
        "package_manifest_sha256": package_manifest_sha256,
        "controller_runtime_receipt_sha256": controller_runtime_receipt_sha256,
        "controller_runtime_root": controller_runtime_root,
        "controller_runtime_tree_sha256": controller_runtime_tree_sha256,
        "controller_release_root": controller_release_root,
        "controller_release_tree_sha256": controller_release_tree_sha256,
        "controller_release_package_manifest_path": (
            controller_release_package_manifest_path
        ),
        "controller_runtime_interpreter_path": (
            controller_runtime_interpreter_path
        ),
        "controller_runtime_interpreter_sha256": (
            controller_runtime_interpreter_sha256
        ),
        "controller_runtime_inventory_path": controller_runtime_inventory_path,
        "controller_runtime_inventory_sha256": (
            controller_runtime_inventory_sha256
        ),
        "controller_requirements_lock_sha256": (
            controller_requirements_lock_sha256
        ),
        "supervisor_launcher_path": supervisor_launcher_path,
        "supervisor_launcher_sha256": supervisor_launcher_sha256,
        "installation_receipt_sha256": installation_receipt_sha256,
        "authorization_sha256": authorization_sha256,
        "plan_sha256": plan_sha256,
        "journal_head_sha256": journal_head_sha256,
        "journal_sequence": journal_sequence,
        "resource_ledger_head_sha256": resource_ledger_head_sha256,
        "resource_ledger_sequence": resource_ledger_sequence,
        "eligibility_receipt_sha256": eligibility_receipt_sha256,
        "retained_audit_set_sha256": retained_audit_set_sha256,
        "exact_targets_absent_count": exact_targets_absent_count,
        "exact_resources_absent": True,
        "stores_installed": False,
        "stores_supervisor_installed": False,
        "source_postgres_read_count": 0,
        "source_postgres_write_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "application_services_installed": False,
        "activation_performed": False,
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    return verify_empty_rollback_receipt(receipt)


def verify_empty_rollback_receipt(
    value: Mapping[str, object],
) -> dict[str, object]:
    receipt = dict(value)
    if (
        set(receipt) != _ROLLBACK_KEYS
        or receipt.get("schema_version") != ROLLBACK_RECEIPT_SCHEMA
        or receipt.get("operation") != ROLLBACK_OPERATION
        or receipt.get("result") != ROLLBACK_RESULT
        or receipt.get("exact_resources_absent") is not True
        or receipt.get("stores_installed") is not False
        or receipt.get("stores_supervisor_installed") is not False
        or type(receipt.get("exact_targets_absent_count")) is not int
        or receipt["exact_targets_absent_count"] != len(ROLLBACK_RESOURCE_KEYS)
    ):
        raise ReceiptError("rollback_receipt_shape_invalid")
    for key in (
        "installation_receipt_sha256",
        "eligibility_receipt_sha256",
        "retained_audit_set_sha256",
    ):
        if not _is_hash(receipt.get(key)):
            raise ReceiptError("installation_receipt_hash_invalid")
    _validate_runtime_identity(receipt)
    _validate_common(receipt, attempt_re=_ROLLBACK_ATTEMPT_RE)
    return receipt


__all__ = [
    "INSTALL_OPERATION",
    "INSTALL_RECEIPT_SCHEMA",
    "ROLLBACK_OPERATION",
    "ROLLBACK_RECEIPT_SCHEMA",
    "ReceiptError",
    "build_empty_rollback_receipt",
    "build_install_receipt",
    "canonical_json_bytes",
    "receipt_sha256",
    "verify_empty_rollback_receipt",
    "verify_install_receipt",
]
