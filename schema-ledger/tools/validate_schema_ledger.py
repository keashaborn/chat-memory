#!/usr/bin/env python3
"""Offline validator for the proposed schema ledger and sanitized evidence."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

from build_schema_ledger import migration_records
from schema_ledger import (
    CATALOG_SCHEMA,
    RECONCILIATION_SCHEMA,
    SOURCE_SCHEMA,
    QDRANT_SCHEMA,
    assert_read_only_catalog,
    canonical_bytes,
    catalog_object_records,
    parse_canonical,
    reconcile,
    sha256,
    validate_ledger,
    validate_catalog_snapshot,
    validate_source_inventory,
    validate_qdrant_metadata,
    git_sql_blobs,
    build_source_inventory,
)


def load(path: pathlib.Path, schema: str | None = None) -> tuple[bytes, dict[str, object]]:
    raw = path.read_bytes()
    value = parse_canonical(raw)
    if not isinstance(value, dict) or (schema is not None and value.get("schema_version") != schema):
        raise RuntimeError(path.name + " failed schema validation")
    return raw, value


def resolve_repository_commit(repository: pathlib.Path, commit: str) -> str:
    resolved_repository = repository.resolve(strict=True)
    if commit != "HEAD":
        return commit
    result = subprocess.run(
        [
            "/usr/bin/git",
            "--no-optional-locks",
            "--no-pager",
            "-C",
            resolved_repository.as_posix(),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    resolved = result.stdout.rstrip(b"\n")
    if result.returncode != 0 or len(resolved) != 40:
        raise RuntimeError("repository HEAD resolution failed")
    try:
        value = resolved.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("repository HEAD resolution failed") from error
    if any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("repository HEAD resolution failed")
    return value


def compare_source_records(expected: object, actual: object) -> dict[str, object]:
    if not isinstance(expected, list) or not isinstance(actual, list) or any(not isinstance(item, dict) for item in expected + actual):
        raise RuntimeError("source-record comparison input is malformed")
    mismatch_by_field: collections.Counter[str] = collections.Counter()
    mismatched_records = abs(len(expected) - len(actual))
    for left, right in zip(expected, actual):
        changed = False
        for field in sorted(set(left) | set(right)):
            if left.get(field) != right.get(field):
                mismatch_by_field[field] += 1
                changed = True
        mismatched_records += int(changed)
    return {
        "record_count": len(actual),
        "canonical_sha256": sha256(canonical_bytes(actual)),
        "mismatched_record_count": mismatched_records,
        "mismatch_by_field": dict(sorted(mismatch_by_field.items())),
    }


def validate_repository_sql(repository: pathlib.Path, commit: str, source: dict[str, object]) -> dict[str, object]:
    resolved_repository = repository.resolve(strict=True)
    resolved_commit = resolve_repository_commit(resolved_repository, commit)
    current = build_source_inventory(git_sql_blobs(resolved_repository, resolved_commit), resolved_commit)
    comparison = compare_source_records(source.get("sources"), current["sources"])
    if comparison["mismatched_record_count"] != 0:
        fields = ",".join(f"{name}:{count}" for name, count in comparison["mismatch_by_field"].items())
        raise RuntimeError(
            f"current repository SQL sources diverged: records={comparison['mismatched_record_count']} fields={fields}"
        )
    expected_hash = sha256(canonical_bytes(source["sources"]))
    if comparison["canonical_sha256"] != expected_hash:
        raise RuntimeError("current repository SQL source canonical hash diverged")
    return comparison


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--ledger", default="ledger/governed-memory-schema-ledger-v1.json")
    parser.add_argument("--repository", type=pathlib.Path)
    parser.add_argument("--repository-commit")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    ledger_path = root / args.ledger
    ledger_raw, ledger = load(ledger_path)
    validate_ledger(ledger)
    baseline = ledger["baseline"]
    evidence: dict[str, tuple[bytes, dict[str, object]]] = {}
    for name, schema in (("catalog", CATALOG_SCHEMA), ("source", SOURCE_SCHEMA), ("qdrant", QDRANT_SCHEMA), ("reconciliation", RECONCILIATION_SCHEMA)):
        relative = baseline[name + "_evidence_path"]
        path = root / relative
        if path.resolve(strict=True).parent != (root / "evidence").resolve(strict=True):
            raise RuntimeError("evidence path escaped the approved directory")
        raw, value = load(path, schema)
        if sha256(raw) != baseline[name + "_evidence_sha256"]:
            raise RuntimeError(name + " evidence hash diverged")
        evidence[name] = (raw, value)
    catalog = evidence["catalog"][1]
    source = evidence["source"][1]
    qdrant = evidence["qdrant"][1]
    reconciliation_evidence = evidence["reconciliation"][1]
    assert_read_only_catalog(catalog)
    validate_catalog_snapshot(catalog)
    validate_source_inventory(source)
    validate_qdrant_metadata(qdrant)
    if source.get("production_commit") != baseline["production_commit"]:
        raise RuntimeError("repository evidence commit diverged")
    if source.get("production_tree") != baseline["production_tree"]:
        raise RuntimeError("repository evidence tree diverged")
    git_inventory = source["git_inventory"]
    for source_name, baseline_name in (("ref_count", "production_ref_count"), ("ref_sha256", "production_ref_sha256"), ("worktree_count", "production_worktree_count"), ("worktree_sha256", "production_worktree_sha256")):
        if git_inventory[source_name] != baseline[baseline_name]:
            raise RuntimeError("repository Git inventory diverged from ledger baseline")
    if qdrant.get("point_payload_read") is not False or qdrant.get("point_search_or_scroll") is not False or qdrant.get("authority") != "derived_rebuildable":
        raise RuntimeError("Qdrant evidence is not metadata-only")
    expected_migrations = migration_records(source)
    if ledger["migrations"] != expected_migrations:
        raise RuntimeError("migration ledger diverged from Git source evidence")
    repository_sql_verified = False
    repository_source_comparison: dict[str, object] | None = None
    if args.repository is not None or args.repository_commit is not None:
        if args.repository is None or args.repository_commit is None:
            raise RuntimeError("repository and repository commit must be supplied together")
        repository_source_comparison = validate_repository_sql(args.repository, args.repository_commit, source)
        repository_sql_verified = True
    observed = catalog_object_records(catalog)
    result = reconcile(ledger["object_expectations"], observed)
    if any(item["classification"] != "matched" for item in result["objects"]):
        raise RuntimeError("PostgreSQL catalog diverged from the proposed baseline")
    if result != reconciliation_evidence.get("catalog_reconciliation"):
        raise RuntimeError("reconciliation evidence diverged")
    report = {
        "schema_version": "schema-ledger-validation-report-v1",
        "ledger_sha256": sha256(ledger_raw),
        "catalog_evidence_sha256": sha256(evidence["catalog"][0]),
        "source_evidence_sha256": sha256(evidence["source"][0]),
        "qdrant_evidence_sha256": sha256(evidence["qdrant"][0]),
        "reconciliation_evidence_sha256": sha256(evidence["reconciliation"][0]),
        "migration_count": len(expected_migrations),
        "catalog_object_count": len(observed),
        "catalog_matches": len(result["objects"]),
        "read_only_catalog_proven": True,
        "row_data_read": False,
        "vector_payload_read": False,
        "repository_sql_verified": repository_sql_verified,
        "repository_source_comparison": repository_source_comparison,
        "status": "valid_candidate",
    }
    sys.stdout.buffer.write(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("schema ledger validation failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
