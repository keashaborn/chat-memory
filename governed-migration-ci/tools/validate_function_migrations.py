#!/usr/bin/env python3
"""Validate the separate governed function-migration registry and packages."""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

from governed_function_migration import (
    MigrationError,
    load_function_package,
    parse_function_registry,
    validate_function_registry,
    validate_function_registry_append_only,
)
from governed_migration import canonical_bytes, sha256
from validate_repository_migrations import ledger_inputs


REGISTRY_RELATIVE = "governed-migration-ci/registry/governed-function-migrations-v1.json"


def _git(repository: pathlib.Path, arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024 or len(result.stderr) > 1024 * 1024:
        raise MigrationError("read-only function-registry Git operation failed")
    return result.stdout


def previous_registry(repository: pathlib.Path, commit: str) -> dict[str, dict[str, object]]:
    parent = _git(repository, ["rev-parse", "--verify", commit + "^{commit}^"]).rstrip(b"\n")
    if len(parent) != 40:
        raise MigrationError("function registry parent commit is malformed")
    tree = _git(repository, ["ls-tree", "-z", parent.decode("ascii", "strict"), "--", REGISTRY_RELATIVE])
    if not tree:
        return {}
    records = [record for record in tree.split(b"\0") if record]
    if len(records) != 1 or not records[0].endswith(b"\t" + REGISTRY_RELATIVE.encode("ascii")):
        raise MigrationError("previous function registry tree identity is malformed")
    return parse_function_registry(
        _git(repository, ["show", parent.decode("ascii", "strict") + ":" + REGISTRY_RELATIVE])
    )


def package_directories(repository: pathlib.Path) -> list[pathlib.Path]:
    root = repository / "governed-function-migrations"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("governed function package root is unsafe")
    output: list[pathlib.Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir() or not (child / "package.json").is_file():
            raise MigrationError("governed function package entry is unsafe")
        output.append(child)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--schema-ledger-root", type=pathlib.Path, required=True)
    parser.add_argument("--repository", type=pathlib.Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    repository = args.repository.resolve(strict=True)
    ledger, _, _ = ledger_inputs(args.schema_ledger_root)
    packages = [load_function_package(path, ledger) for path in package_directories(repository)]
    registry_path = root / "registry/governed-function-migrations-v1.json"
    registry = parse_function_registry(registry_path.read_bytes())
    validate_function_registry_append_only(previous_registry(repository, args.repository_commit), registry)
    validate_function_registry(packages, registry)
    report = {
        "schema_version": "governed-function-migration-repository-validation-v1",
        "schema_ledger_sha256": ledger["ledger_sha256"],
        "catalog_evidence_sha256": ledger["catalog_evidence_sha256"],
        "package_ids": sorted(package.migration_id for package in packages),
        "package_sha256": {
            package.migration_id: package.package_sha256
            for package in sorted(packages, key=lambda item: item.migration_id)
        },
        "registry_sha256": sha256(registry_path.read_bytes()),
        "v1_table_migration_contract_modified": False,
        "status": "valid",
    }
    sys.stdout.buffer.write(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("governed function migration validation failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
