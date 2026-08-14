#!/usr/bin/env python3
"""Verify the exact stores-only dormant-store installation migration subset."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "governed-memory-migrations"
MANIFEST_RELATIVE = "ops/governed_memory/installation/current/migration_manifest.json"
BINDINGS_RELATIVE = (
    "ops/governed_memory/installation/current/postgres/migration_bindings.json"
)
PREFLIGHT_RELATIVE = (
    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
)
MANIFEST = ROOT / MANIFEST_RELATIVE
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
EXPECTED_BINDINGS_CANONICAL_SHA256 = (
    "f6229283ff196e7355294f99321b92f350c4ec7909744544e9b1941e13100b42"
)
EXPECTED_FILES = frozenset(
    {
        BINDINGS_RELATIVE,
        PREFLIGHT_RELATIVE,
        "predicate_catalog.json",
        "0001_foundation/forward.pgsql",
        "0001_foundation/rollback.pgsql",
        "0003_owner_claim_detail/forward.pgsql",
        "0003_owner_claim_detail/rollback.pgsql",
        "0004_pilot_marker/forward.pgsql",
        "0004_pilot_marker/rollback.pgsql",
    }
)
HISTORICAL_PACKAGE_DESCRIPTORS = frozenset(
    {
        "0001_foundation/package.json",
        "0003_owner_claim_detail/package.json",
        "0004_pilot_marker/package.json",
    }
)
EXECUTION_ORDER = [
    PREFLIGHT_RELATIVE,
    "0001_foundation/forward.pgsql",
    "0003_owner_claim_detail/forward.pgsql",
    "0004_pilot_marker/forward.pgsql",
]
ROLLBACK_ORDER = [
    "0004_pilot_marker/rollback.pgsql",
    "0003_owner_claim_detail/rollback.pgsql",
    "0001_foundation/rollback.pgsql",
]


class StoreMigrationManifestError(ValueError):
    pass


class _NonFiniteJsonValue(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StoreMigrationManifestError("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _relative_parts(relative: object) -> tuple[str, ...]:
    if type(relative) is not str or not relative or "\\" in relative:
        raise StoreMigrationManifestError("manifest_path_invalid")
    pure = PurePosixPath(relative)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or str(pure) != relative
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise StoreMigrationManifestError("manifest_path_invalid")
    return parts


def _nofollow() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if flag == 0:
        raise StoreMigrationManifestError("manifest_nofollow_unavailable")
    return flag


def _read_checked(relative: str) -> bytes:
    parts = _relative_parts(relative)
    base = ROOT if relative.startswith("ops/") else MIGRATIONS
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    descriptors: list[int] = []
    try:
        directory_fd = os.open(base, directory_flags)
        descriptors.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise StoreMigrationManifestError("manifest_path_invalid")
        chunks: list[bytes] = []
        while True:
            block = os.read(file_fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    except OSError as error:
        raise StoreMigrationManifestError("manifest_path_invalid") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _parse_json(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _NonFiniteJsonValue,
    ) as error:
        raise StoreMigrationManifestError("json_invalid") from error
    if type(value) is not dict:
        raise StoreMigrationManifestError("json_root_invalid")
    return value


def _load(relative: str) -> dict[str, object]:
    return _parse_json(_read_checked(relative))


def _sha256(relative: str) -> str:
    return hashlib.sha256(_read_checked(relative)).hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise StoreMigrationManifestError("json_invalid") from error
    return hashlib.sha256(raw).hexdigest()


def _verify_bindings(
    bindings: dict[str, object], observed: dict[str, str]
) -> None:
    if set(bindings) != {
        "schema_version",
        "state",
        "historical_package_descriptors_are_execution_authority",
        "roles_preflight",
        "migrations",
    }:
        raise StoreMigrationManifestError("migration_bindings_shape_invalid")
    if (
        bindings.get("schema_version")
        != "governed-memory-dormant-store-install-store-migration-bindings-v2"
        or bindings.get("state")
        != "repository-only-current-store-migration-bindings-not-installed-not-authorized"
        or bindings.get("historical_package_descriptors_are_execution_authority")
        is not False
        or _canonical_sha256(bindings) != EXPECTED_BINDINGS_CANONICAL_SHA256
    ):
        raise StoreMigrationManifestError("migration_bindings_semantics_invalid")

    roles = bindings.get("roles_preflight")
    migrations = bindings.get("migrations")
    if (
        type(roles) is not dict
        or set(roles) != {"path", "sha256"}
        or roles.get("path") != PREFLIGHT_RELATIVE
        or roles.get("sha256") != observed.get(PREFLIGHT_RELATIVE)
        or type(migrations) is not list
        or len(migrations) != 3
    ):
        raise StoreMigrationManifestError("migration_bindings_semantics_invalid")

    expected_ids = (
        "governed_memory_foundation_0001",
        "governed_memory_owner_claim_detail_0003",
        "governed_memory_pilot_marker_0004",
    )
    expected_dependencies = (
        ["dormant_store_install_roles_preflight"],
        ["dormant_store_install_roles_preflight", "governed_memory_foundation_0001"],
        ["governed_memory_foundation_0001"],
    )
    expected_rollback_metadata = (
        {"empty_only": True},
        {"empty_only": False, "data_mutation": False},
        {"empty_only": True},
    )
    expected_forward = tuple(EXECUTION_ORDER[1:])
    expected_rollback = tuple(reversed(ROLLBACK_ORDER))
    for index, migration in enumerate(migrations):
        if (
            type(migration) is not dict
            or set(migration) != {
                "migration_id",
                "dependencies",
                "forward",
                "rollback",
            }
            or migration.get("migration_id") != expected_ids[index]
            or migration.get("dependencies") != expected_dependencies[index]
        ):
            raise StoreMigrationManifestError("migration_bindings_semantics_invalid")
        forward = migration.get("forward")
        rollback = migration.get("rollback")
        rollback_metadata = expected_rollback_metadata[index]
        if (
            type(forward) is not dict
            or set(forward) != {"path", "sha256"}
            or forward.get("path") != expected_forward[index]
            or forward.get("sha256") != observed.get(expected_forward[index])
            or type(rollback) is not dict
            or set(rollback) != {"path", "sha256", *rollback_metadata}
            or rollback.get("path") != expected_rollback[index]
            or rollback.get("sha256") != observed.get(expected_rollback[index])
            or any(
                rollback.get(key) is not value
                for key, value in rollback_metadata.items()
            )
        ):
            raise StoreMigrationManifestError("migration_bindings_semantics_invalid")
    if any(path in observed for path in HISTORICAL_PACKAGE_DESCRIPTORS):
        raise StoreMigrationManifestError("historical_package_descriptor_in_manifest")


def verify() -> dict[str, object]:
    manifest_raw = _read_checked(MANIFEST_RELATIVE)
    manifest = _parse_json(manifest_raw)
    if set(manifest) != {
        "schema_version",
        "candidate_id",
        "state",
        "execution_order",
        "rollback_order",
        "files",
    }:
        raise StoreMigrationManifestError("manifest_shape_invalid")
    if (
        manifest.get("schema_version")
        != "governed-memory-dormant-store-install-store-migration-manifest-v2"
        or manifest.get("candidate_id") != "governed_memory_9a54cf123493_000006"
        or manifest.get("state")
        != "repository-only-current-stores-only-migration-set-not-installed-not-authorized"
        or manifest.get("execution_order") != EXECUTION_ORDER
        or manifest.get("rollback_order") != ROLLBACK_ORDER
    ):
        raise StoreMigrationManifestError("manifest_contract_invalid")
    entries = manifest.get("files")
    if type(entries) is not list:
        raise StoreMigrationManifestError("manifest_files_invalid")
    declared: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"path", "sha256"}:
            raise StoreMigrationManifestError("manifest_entry_invalid")
        relative = entry.get("path")
        wanted = entry.get("sha256")
        if (
            type(relative) is not str
            or relative in declared
            or type(wanted) is not str
            or HEX_SHA256.fullmatch(wanted) is None
        ):
            raise StoreMigrationManifestError("manifest_entry_invalid")
        declared[relative] = wanted
    if set(declared) != EXPECTED_FILES:
        raise StoreMigrationManifestError("manifest_file_set_invalid")
    if any(path.startswith("0002_") for path in declared):
        raise StoreMigrationManifestError("source_bridge_in_store_manifest")
    if any(path in declared for path in HISTORICAL_PACKAGE_DESCRIPTORS):
        raise StoreMigrationManifestError("historical_package_descriptor_in_manifest")

    observed: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    for relative in sorted(declared):
        raw = _read_checked(relative)
        actual = hashlib.sha256(raw).hexdigest()
        wanted = declared[relative]
        if actual != wanted:
            raise StoreMigrationManifestError("manifest_hash_mismatch:" + relative)
        observed[relative] = actual
        contents[relative] = raw

    bindings = _parse_json(contents[BINDINGS_RELATIVE])
    _verify_bindings(bindings, observed)
    try:
        preflight = contents[PREFLIGHT_RELATIVE].decode("utf-8")
    except UnicodeDecodeError as error:
        raise StoreMigrationManifestError("roles_preflight_not_utf8") from error
    if (
        "\\set ON_ERROR_STOP on" not in preflight
        or "ERRCODE = '22023'" not in preflight
        or re.search(r"^[ \t]*\\(?:q|quit)(?:[ \t]|$)", preflight, re.MULTILINE)
    ):
        raise StoreMigrationManifestError("roles_preflight_not_fail_closed")
    return {
        "schema_version": "governed-memory-dormant-store-install-store-migration-verification-v3",
        "state": str(manifest["state"]),
        "file_count": len(observed),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "migration_bindings_sha256": observed[BINDINGS_RELATIVE],
        "migration_bindings_canonical_sha256": _canonical_sha256(bindings),
        "artifact_sha256": dict(sorted(observed.items())),
        "source_bridge_artifact_count": 0,
        "historical_package_descriptor_count": 0,
        "production_state_changed": False,
    }


def main() -> int:
    if sys.argv[1:]:
        print("DORMANT_STORE_INSTALL_STORE_MIGRATION_MANIFEST_INVALID=arguments", file=sys.stderr)
        return 2
    try:
        receipt = verify()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            "DORMANT_STORE_INSTALL_STORE_MIGRATION_MANIFEST_INVALID=" + str(error),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
