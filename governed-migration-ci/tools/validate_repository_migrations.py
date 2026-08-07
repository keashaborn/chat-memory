#!/usr/bin/env python3
"""Reconcile frozen legacy SQL and future governed migration packages."""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

from governed_migration import (
    MigrationError,
    canonical_bytes,
    load_external_deployed_sql_registry,
    load_legacy_sql_path_alias_registry,
    load_package,
    load_registry,
    parse_canonical,
    parse_external_deployed_sql_registry,
    parse_legacy_sql_path_alias_registry,
    parse_registry,
    sha256,
    validate_dependency_graph,
    validate_external_deployed_sql_commit_bindings,
    validate_external_deployed_sql_registry_append_only,
    validate_legacy_sql_path_alias_commit_bindings,
    validate_legacy_sql_path_alias_registry_append_only,
    validate_registry,
    validate_registry_append_only,
    validate_repository_inventory,
)


def ledger_inputs(schema_ledger_root: pathlib.Path) -> tuple[dict[str, str], list[dict[str, object]], dict[str, int]]:
    root = schema_ledger_root.resolve(strict=True)
    ledger_raw = (root / "ledger/governed-memory-schema-ledger-v1.json").read_bytes()
    ledger = parse_canonical(ledger_raw, maximum=64 * 1024 * 1024)
    if not isinstance(ledger, dict) or ledger.get("schema_version") != "governed-memory-schema-ledger-v1":
        raise MigrationError("schema ledger is invalid")
    baseline = ledger.get("baseline")
    if not isinstance(baseline, dict):
        raise MigrationError("schema ledger baseline is invalid")
    source_path = root / str(baseline["source_evidence_path"])
    source_raw = source_path.read_bytes()
    if sha256(source_raw) != baseline["source_evidence_sha256"]:
        raise MigrationError("schema ledger source evidence changed")
    source = parse_canonical(source_raw, maximum=64 * 1024 * 1024)
    if not isinstance(source, dict) or not isinstance(source.get("sources"), list):
        raise MigrationError("schema ledger source evidence is invalid")
    classifications: dict[str, int] = {}
    for item in ledger.get("migrations", []):
        if not isinstance(item, dict) or not isinstance(item.get("classification"), str):
            raise MigrationError("schema ledger migration record is invalid")
        name = item["classification"]
        classifications[name] = classifications.get(name, 0) + 1
    identity = {
        "ledger_id": str(ledger["ledger_id"]),
        "ledger_sha256": sha256(ledger_raw),
        "catalog_evidence_sha256": str(baseline["catalog_evidence_sha256"]),
    }
    return identity, list(source["sources"]), classifications


def package_directories(root: pathlib.Path) -> list[pathlib.Path]:
    locations = [root / "fixtures", root.parent / "governed-migrations"]
    output: list[pathlib.Path] = []
    for location in locations:
        if not location.exists():
            continue
        if location.is_symlink() or not location.is_dir():
            raise MigrationError("package root is unsafe")
        for child in sorted(location.iterdir(), key=lambda item: item.name):
            if child.is_symlink() or not child.is_dir() or not (child / "package.json").is_file():
                raise MigrationError("migration package entry is unsafe")
            output.append(child)
    return output


def current_legacy_projection(schema_ledger_root: pathlib.Path, repository: pathlib.Path, commit: str) -> list[dict[str, object]]:
    tools = (schema_ledger_root.resolve(strict=True) / "tools").resolve(strict=True)
    if tools.parent != schema_ledger_root.resolve(strict=True) or tools.is_symlink():
        raise MigrationError("schema ledger tools path is unsafe")
    if tools.as_posix() not in sys.path:
        sys.path.insert(0, tools.as_posix())
    module = importlib.import_module("schema_ledger")
    resolved = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "rev-parse", "--verify", commit + "^{commit}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    oid = resolved.stdout.rstrip(b"\n")
    if resolved.returncode != 0 or len(oid) != 40:
        raise MigrationError("repository commit resolution failed")
    resolved_commit = oid.decode("ascii", "strict")
    blobs = module.git_sql_blobs(repository, resolved_commit)
    value = module.build_source_inventory(blobs, resolved_commit)
    records = value.get("sources")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise MigrationError("shared source projector returned malformed records")
    return list(records)


def previous_registry(repository: pathlib.Path, commit: str) -> dict[str, dict[str, object]]:
    relative = "governed-migration-ci/registry/governed-migrations-v1.json"
    parent = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "rev-parse", "--verify", commit + "^{commit}^"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    parent_oid = parent.stdout.rstrip(b"\n")
    if parent.returncode != 0 or len(parent_oid) != 40:
        raise MigrationError("previous commit resolution failed")
    tree = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "ls-tree", "-z", parent_oid.decode("ascii", "strict"), "--", relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if tree.returncode != 0 or len(tree.stdout) > 1024 * 1024:
        raise MigrationError("previous registry presence check failed")
    if not tree.stdout:
        return {}
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1 or not records[0].endswith(b"\t" + relative.encode("ascii")):
        raise MigrationError("previous registry tree identity is malformed")
    result = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "show", parent_oid.decode("ascii", "strict") + ":" + relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 8 * 1024 * 1024:
        raise MigrationError("previous registry read failed")
    return parse_registry(result.stdout)


def previous_optional_registry(
    repository: pathlib.Path,
    commit: str,
    relative: str,
    parser,
    label: str,
) -> dict[str, dict[str, object]]:
    parent = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "rev-parse", "--verify", commit + "^{commit}^"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    parent_oid = parent.stdout.rstrip(b"\n")
    if parent.returncode != 0 or len(parent_oid) != 40:
        raise MigrationError("previous " + label + " registry commit resolution failed")
    tree = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "ls-tree", "-z", parent_oid.decode("ascii", "strict"), "--", relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if tree.returncode != 0 or len(tree.stdout) > 1024 * 1024:
        raise MigrationError("previous " + label + " registry presence check failed")
    if not tree.stdout:
        return {}
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1 or not records[0].endswith(b"\t" + relative.encode("ascii")):
        raise MigrationError("previous " + label + " registry tree identity is malformed")
    result = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "show", parent_oid.decode("ascii", "strict") + ":" + relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 1024 * 1024:
        raise MigrationError("previous " + label + " registry read failed")
    return parser(result.stdout)


def previous_external_registry(
    repository: pathlib.Path,
    commit: str,
) -> dict[str, dict[str, object]]:
    return previous_optional_registry(
        repository,
        commit,
        "governed-migration-ci/registry/external-deployed-sql-v1.json",
        parse_external_deployed_sql_registry,
        "external deployed SQL",
    )


def previous_legacy_alias_registry(
    repository: pathlib.Path,
    commit: str,
) -> dict[str, dict[str, object]]:
    return previous_optional_registry(
        repository,
        commit,
        "governed-migration-ci/registry/legacy-sql-path-aliases-v1.json",
        parse_legacy_sql_path_alias_registry,
        "legacy SQL path alias",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--schema-ledger-root", type=pathlib.Path, required=True)
    parser.add_argument("--repository", type=pathlib.Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    repository = args.repository.resolve(strict=True)
    identity, baseline_sources, classifications = ledger_inputs(args.schema_ledger_root)
    packages = [load_package(directory, identity) for directory in package_directories(root)]
    validate_dependency_graph(packages)
    registry = load_registry(root / "registry/governed-migrations-v1.json")
    validate_registry_append_only(previous_registry(repository, args.repository_commit), registry)
    validate_registry(packages, registry)
    external_registry = load_external_deployed_sql_registry(
        root / "registry/external-deployed-sql-v1.json"
    )
    validate_external_deployed_sql_registry_append_only(
        previous_external_registry(repository, args.repository_commit),
        external_registry,
    )
    external_commit_binding = validate_external_deployed_sql_commit_bindings(
        repository,
        args.repository_commit,
        external_registry,
    )
    legacy_alias_registry = load_legacy_sql_path_alias_registry(
        root / "registry/legacy-sql-path-aliases-v1.json"
    )
    validate_legacy_sql_path_alias_registry_append_only(
        previous_legacy_alias_registry(repository, args.repository_commit),
        legacy_alias_registry,
    )
    legacy_alias_commit_binding = validate_legacy_sql_path_alias_commit_bindings(
        repository,
        args.repository_commit,
        legacy_alias_registry,
    )
    projected = current_legacy_projection(args.schema_ledger_root, repository, args.repository_commit)
    inventory = validate_repository_inventory(
        repository,
        args.repository_commit,
        baseline_sources,
        packages,
        current_legacy_sources=projected,
        external_deployed_sources=external_registry,
        legacy_path_aliases=legacy_alias_registry,
    )
    if classifications != {"duplicated": 3, "source-only": 173, "unverifiable": 491}:
        raise MigrationError("legacy classifications changed")
    report = {
        "schema_version": "governed-migration-repository-validation-v1",
        "schema_ledger_sha256": identity["ledger_sha256"],
        "catalog_evidence_sha256": identity["catalog_evidence_sha256"],
        "legacy_classifications": classifications,
        "legacy_execution_authorized": False,
        "external_deployed_sql_execution_authorized": False,
        "external_deployed_sql_commit_binding": external_commit_binding,
        "external_deployed_sql_record_ids": sorted(
            str(record["record_id"]) for record in external_registry.values()
        ),
        "legacy_sql_path_alias_execution_authorized": False,
        "legacy_sql_path_alias_commit_binding": legacy_alias_commit_binding,
        "legacy_sql_path_alias_record_ids": sorted(
            str(record["record_id"]) for record in legacy_alias_registry.values()
        ),
        "inventory": inventory,
        "package_ids": sorted(package.migration_id for package in packages),
        "package_sha256": {package.migration_id: package.package_sha256 for package in sorted(packages, key=lambda item: item.migration_id)},
        "status": "valid",
    }
    sys.stdout.buffer.write(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("governed migration validation failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
