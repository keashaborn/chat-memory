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
    "0003_owner_claim_detail/package.json",
    "0004_pilot_marker/package.json",
    "0002_conversation_bridge/package.json",
)
EXPECTED_FILES = {
    "roles_preflight.pgsql",
    "schema_contract.json",
    "predicate_catalog.json",
    "0001_foundation/package.json",
    "0001_foundation/forward.pgsql",
    "0001_foundation/rollback.pgsql",
    "0003_owner_claim_detail/package.json",
    "0003_owner_claim_detail/forward.pgsql",
    "0003_owner_claim_detail/rollback.pgsql",
    "0004_pilot_marker/package.json",
    "0004_pilot_marker/forward.pgsql",
    "0004_pilot_marker/rollback.pgsql",
    "0002_conversation_bridge/package.json",
    "0002_conversation_bridge/forward.pgsql",
    "0002_conversation_bridge/rollback.pgsql",
}
EXPECTED_EXECUTION_ORDER = [
    "roles_preflight.pgsql",
    "0001_foundation/forward.pgsql",
    "0003_owner_claim_detail/forward.pgsql",
    "0004_pilot_marker/forward.pgsql",
    "0002_conversation_bridge/forward.pgsql",
]
EXPECTED_ROLLBACK_ORDER = [
    "0002_conversation_bridge/rollback.pgsql",
    "0004_pilot_marker/rollback.pgsql",
    "0003_owner_claim_detail/rollback.pgsql",
    "0001_foundation/rollback.pgsql",
]
VALIDATED_STATUS = "isolated_candidate_disposable_validated_not_production_applied"
NOT_VALIDATED_STATUS = (
    "isolated_candidate_not_yet_disposable_validated_not_production_applied"
)
EXPECTED_PACKAGE_CONTRACTS = {
    "0001_foundation/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "production_qdrant_changed": False,
            "disposable_database_validated": True,
            "disposable_qdrant_validated": True,
        },
    },
    "0003_owner_claim_detail/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": False,
        "rollback_data_mutation": False,
        "activation": {
            "production_authorized": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "disposable_database_validated": True,
        },
    },
    "0004_pilot_marker/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_database_applied": False,
            "production_services_changed": False,
            "disposable_database_validated": True,
        },
    },
    "0002_conversation_bridge/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_writer_membership_granted": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "disposable_writer_membership_validated": True,
            "disposable_database_validated": True,
        },
    },
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def expected_package_contracts(*, phase6e_proof: bool) -> dict[str, dict[str, object]]:
    contracts = {
        relative: {
            **contract,
            "activation": dict(contract["activation"]),
        }
        for relative, contract in EXPECTED_PACKAGE_CONTRACTS.items()
    }
    if phase6e_proof:
        for relative in (
            "0001_foundation/package.json",
            "0003_owner_claim_detail/package.json",
            "0004_pilot_marker/package.json",
            "0002_conversation_bridge/package.json",
        ):
            contracts[relative]["status"] = NOT_VALIDATED_STATUS
            contracts[relative]["activation"][
                "disposable_database_validated"
            ] = False
        contracts["0001_foundation/package.json"]["activation"][
            "disposable_qdrant_validated"
        ] = False
        contracts["0002_conversation_bridge/package.json"]["activation"][
            "disposable_writer_membership_validated"
        ] = False
    return contracts


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


def verify(root: Path, *, phase6e_proof: bool = False) -> dict[str, object]:
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != "governed-memory-migration-manifest-v2":
        raise ValueError("unexpected migration manifest schema")
    candidate_id = manifest.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("missing migration candidate id")
    expected_manifest_status = (
        NOT_VALIDATED_STATUS if phase6e_proof else VALIDATED_STATUS
    )
    if manifest.get("status") != expected_manifest_status:
        raise ValueError("unexpected migration candidate status")
    expected_authority = {
        "production_apply_authorized": False,
        "production_service_change_authorized": False,
        "production_provider_call_authorized": False,
        "production_qdrant_change_authorized": False,
        "legacy_import_authorized": False,
        "disposable_validation_authorized": not phase6e_proof,
    }
    if manifest.get("authority") != expected_authority:
        raise ValueError("unexpected migration authority contract")
    if manifest.get("execution_order") != EXPECTED_EXECUTION_ORDER:
        raise ValueError("unexpected migration execution order")
    if manifest.get("rollback_order") != EXPECTED_ROLLBACK_ORDER:
        raise ValueError("unexpected migration rollback order")
    expected_safety = {
        "migration_runner_transaction_required": True,
        "migration_runner_timeouts_required": True,
        "migration_runner_advisory_lock_required": True,
        "rollback_empty_only": False,
        "claim_detail_rollback_data_mutation": False,
        "pilot_marker_rollback_empty_only": True,
        "cascade_ddl_allowed": False,
        "disposable_database_execution_performed": not phase6e_proof,
        "production_database_execution_performed": False,
        "production_checkout_files_changed": False,
        "production_data_read": False,
        "provider_external_calls": 0,
    }
    if manifest.get("safety") != expected_safety:
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

    package_contracts = expected_package_contracts(phase6e_proof=phase6e_proof)
    for package_relative in EXPECTED_PACKAGES:
        if package_relative not in expected:
            raise ValueError(f"package missing from manifest: {package_relative}")
        package_path = checked_path(root, package_relative)
        package = load_json(package_path)
        if package.get("schema_version") != "governed-memory-migration-package-v2":
            raise ValueError(f"unexpected package schema: {package_relative}")
        expected_contract = package_contracts[package_relative]
        if package.get("status") != expected_contract["status"]:
            raise ValueError(f"unexpected package status: {package_relative}")
        if package.get("transaction") != {
            "required": True,
            "runner_supplies_begin_commit": True,
            "runner_supplies_timeouts": True,
            "runner_supplies_advisory_lock": True,
        }:
            raise ValueError(f"unsafe transaction contract: {package_relative}")
        rollback = package.get("rollback")
        if not isinstance(rollback, dict) or rollback.get("empty_only") is not (
            expected_contract["rollback_empty_only"]
        ):
            raise ValueError(f"unexpected rollback contract: {package_relative}")
        if "rollback_data_mutation" in expected_contract and rollback.get(
            "data_mutation"
        ) is not expected_contract["rollback_data_mutation"]:
            raise ValueError(
                f"unexpected rollback data-mutation contract: {package_relative}"
            )
        object_contract = package.get("object_contract")
        if not isinstance(object_contract, dict) or object_contract.get(
            "cascade_ddl"
        ) is not False:
            raise ValueError(f"CASCADE is not prohibited: {package_relative}")
        if package.get("activation") != expected_contract["activation"]:
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
        "file_count": len(expected),
        "manifest_sha256": sha256_file(manifest_path),
        "migration_package_id_sha256": hashlib.sha256(
            candidate_id.encode("utf-8")
        ).hexdigest(),
        "result": "verified",
        "schema_version": "governed-memory-migration-verification-v4",
        "validation_state": (
            "phase6e_disposable_deletion_proof_candidate"
            if phase6e_proof
            else "disposable_validated"
        ),
    }


def main() -> int:
    arguments = list(sys.argv[1:])
    phase6e_proof = False
    if arguments[:1] == ["--phase6e-disposable-deletion-proof"]:
        phase6e_proof = True
        arguments.pop(0)
    if len(arguments) > 1:
        raise SystemExit(
            "usage: verify_migration_manifest.py "
            "[--phase6e-disposable-deletion-proof] [migration_root]"
        )
    default_root = Path(__file__).resolve().parents[2] / "governed-memory-migrations"
    root = Path(arguments[0]) if arguments else default_root
    try:
        receipt = verify(root, phase6e_proof=phase6e_proof)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"MIGRATION_MANIFEST_INVALID={error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
