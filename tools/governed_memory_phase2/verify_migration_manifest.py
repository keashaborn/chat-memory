#!/usr/bin/env python3
"""Fail closed unless the governed Memory migration package bytes match."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


EXPECTED_PACKAGES = (
    "0001_foundation/package.json",
    "0002_conversation_bridge/package.json",
)
EXPECTED_FILES = {
    "roles_preflight.pgsql",
    "schema_contract.json",
    "predicate_catalog.json",
    "0001_foundation/package.json",
    "0001_foundation/forward.pgsql",
    "0001_foundation/rollback.pgsql",
    "0002_conversation_bridge/package.json",
    "0002_conversation_bridge/forward.pgsql",
    "0002_conversation_bridge/rollback.pgsql",
}
EXPECTED_EXECUTION_ORDER = [
    "roles_preflight.pgsql",
    "0001_foundation/forward.pgsql",
    "0002_conversation_bridge/forward.pgsql",
]
EXPECTED_ROLLBACK_ORDER = [
    "0002_conversation_bridge/rollback.pgsql",
    "0001_foundation/rollback.pgsql",
]
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError(f"unsafe manifest path: {relative!r}")
    candidate = (root / relative).resolve(strict=True)
    candidate.relative_to(root)
    if not candidate.is_file():
        raise ValueError(f"manifest entry is not a file: {relative}")
    return candidate


def verify(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != "governed-memory-migration-manifest-v2":
        raise ValueError("unexpected migration manifest schema")
    if manifest.get("candidate_id") != "clean-governed-memory-phase2-2026-08-10":
        raise ValueError("unexpected migration candidate id")
    if manifest.get("status") != (
        "isolated_candidate_disposable_validated_not_production_applied"
    ):
        raise ValueError("unexpected migration candidate status")
    if manifest.get("authority") != {
        "production_apply_authorized": False,
        "production_service_change_authorized": False,
        "production_provider_call_authorized": False,
        "production_qdrant_change_authorized": False,
        "legacy_import_authorized": False,
        "disposable_validation_authorized": True,
    }:
        raise ValueError("unexpected migration authority contract")
    if manifest.get("execution_order") != EXPECTED_EXECUTION_ORDER:
        raise ValueError("unexpected migration execution order")
    if manifest.get("rollback_order") != EXPECTED_ROLLBACK_ORDER:
        raise ValueError("unexpected migration rollback order")
    if manifest.get("safety") != {
        "migration_runner_transaction_required": True,
        "migration_runner_timeouts_required": True,
        "migration_runner_advisory_lock_required": True,
        "rollback_empty_only": True,
        "cascade_ddl_allowed": False,
        "disposable_database_execution_performed": True,
        "production_database_execution_performed": False,
        "production_checkout_files_changed": False,
        "production_data_read": False,
        "provider_external_calls": 0,
    }:
        raise ValueError("unexpected migration safety contract")

    expected: dict[str, str] = {}
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("migration manifest files must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("invalid migration manifest file entry")
        relative = entry.get("path")
        wanted = entry.get("sha256")
        if not isinstance(relative, str):
            raise ValueError("invalid migration manifest path")
        if relative in expected:
            raise ValueError(f"duplicate manifest path: {relative}")
        if not isinstance(wanted, str) or HEX_SHA256.fullmatch(wanted) is None:
            raise ValueError(f"invalid manifest digest: {relative}")
        expected[relative] = wanted
        actual = sha256_file(checked_path(root, relative))
        if actual != wanted:
            raise ValueError(
                f"migration byte mismatch: {relative}: expected {wanted}, got {actual}"
            )
    if set(expected) != EXPECTED_FILES:
        raise ValueError("migration manifest file set is not exact")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if observed != EXPECTED_FILES:
        raise ValueError("migration directory contains an undeclared file")

    for package_relative in EXPECTED_PACKAGES:
        if package_relative not in expected:
            raise ValueError(f"package missing from manifest: {package_relative}")
        package_path = checked_path(root, package_relative)
        package = load_json(package_path)
        if package.get("schema_version") != "governed-memory-migration-package-v2":
            raise ValueError(f"unexpected package schema: {package_relative}")
        if package.get("status") != (
            "isolated_candidate_disposable_validated_not_production_applied"
        ):
            raise ValueError(f"unexpected package status: {package_relative}")
        if package.get("transaction") != {
            "required": True,
            "runner_supplies_begin_commit": True,
            "runner_supplies_timeouts": True,
            "runner_supplies_advisory_lock": True,
        }:
            raise ValueError(f"unsafe transaction contract: {package_relative}")
        rollback = package.get("rollback")
        if not isinstance(rollback, dict) or rollback.get("empty_only") is not True:
            raise ValueError(f"rollback is not empty-only: {package_relative}")
        object_contract = package.get("object_contract")
        if not isinstance(object_contract, dict) or object_contract.get(
            "cascade_ddl"
        ) is not False:
            raise ValueError(f"CASCADE is not prohibited: {package_relative}")
        activation = package.get("activation")
        if package_relative == "0001_foundation/package.json":
            expected_activation = {
                "production_authorized": False,
                "production_services_changed": False,
                "production_database_applied": False,
                "production_qdrant_changed": False,
                "disposable_database_validated": True,
                "disposable_qdrant_validated": True,
            }
        else:
            expected_activation = {
                "production_authorized": False,
                "production_writer_membership_granted": False,
                "production_services_changed": False,
                "production_database_applied": False,
                "disposable_writer_membership_validated": True,
                "disposable_database_validated": True,
            }
        if activation != expected_activation:
            raise ValueError(f"unexpected activation contract: {package_relative}")
        package_dir = Path(package_relative).parent
        for direction in ("forward", "rollback"):
            contract = package.get(direction, {})
            if not isinstance(contract, dict) or contract.get("path") != (
                f"{direction}.pgsql"
            ):
                raise ValueError(
                    f"unexpected package path: {package_relative}:{direction}"
                )
            relative = (package_dir / contract["path"]).as_posix()
            wanted = contract.get("sha256")
            if expected.get(relative) != wanted:
                raise ValueError(
                    f"package/manifest digest disagreement: {package_relative}:{direction}"
                )
            actual = sha256_file(checked_path(root, relative))
            if actual != wanted:
                raise ValueError(
                    f"package byte mismatch: {package_relative}:{direction}"
                )
            sql = checked_path(root, relative).read_text(encoding="utf-8")
            if re.search(r"\bCASCADE\b", sql, flags=re.IGNORECASE):
                raise ValueError(f"CASCADE token present: {relative}")

    return {
        "candidate_id": manifest.get("candidate_id"),
        "file_count": len(expected),
        "manifest_sha256": sha256_file(manifest_path),
        "result": "verified",
        "schema_version": "governed-memory-migration-verification-v2",
    }


def main() -> int:
    if len(sys.argv) > 2:
        raise SystemExit("usage: verify_migration_manifest.py [migration_root]")
    default_root = Path(__file__).resolve().parents[2] / "governed-memory-migrations"
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else default_root
    try:
        receipt = verify(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"MIGRATION_MANIFEST_INVALID={error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
