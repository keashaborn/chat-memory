#!/usr/bin/env python3
"""Recheck production metadata using only the fixed read-only helpers."""

from __future__ import annotations

import argparse
import copy
import os
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

from schema_ledger import (
    CATALOG_SCHEMA,
    QDRANT_SCHEMA,
    SOURCE_SCHEMA,
    canonical_bytes,
    parse_canonical,
    sha256,
    validate_catalog_snapshot,
    validate_qdrant_metadata,
    validate_source_inventory,
    validate_ledger,
)


def catalog_stable_identity(value: dict[str, object]) -> str:
    stable = copy.deepcopy(value)
    del stable["observed_at_utc"]
    return sha256(canonical_bytes(stable))


def run_helper(argv: list[str]) -> dict[str, object]:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, check=False, timeout=600, env={"LANG": "C", "LC_ALL": "C"})
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 128 * 1024 * 1024:
        raise RuntimeError("read-only compatibility helper failed")
    value = parse_canonical(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("read-only compatibility result is malformed")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--host", choices=["seebx"], required=True)
    parser.add_argument("--source", choices=["/opt/chat-memory"], required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    audit = pathlib.Path(__file__).resolve().parent / "run_readonly_production_audit.py"
    ledger = parse_canonical((root / "ledger/governed-memory-schema-ledger-v1.json").read_bytes())
    if not isinstance(ledger, dict) or not isinstance(ledger.get("baseline"), dict):
        raise RuntimeError("ledger baseline is malformed")
    validate_ledger(ledger)
    baseline = ledger["baseline"]
    if baseline.get("production_commit") != args.expected_commit:
        raise RuntimeError("requested production commit differs from the ledger")
    evidence_root = (root / "evidence").resolve(strict=True)
    evidence_paths = [(root / baseline[name + "_evidence_path"]).resolve(strict=True) for name in ("catalog", "source", "qdrant")]
    if any(path.parent != evidence_root for path in evidence_paths):
        raise RuntimeError("ledger evidence path escaped")
    baseline_catalog = parse_canonical(evidence_paths[0].read_bytes())
    baseline_source = parse_canonical(evidence_paths[1].read_bytes())
    baseline_qdrant = parse_canonical(evidence_paths[2].read_bytes())
    if not all(isinstance(value, dict) for value in (baseline_catalog, baseline_source, baseline_qdrant)):
        raise RuntimeError("baseline evidence is malformed")
    prefix = [os.path.realpath(sys.executable), "-B", audit.as_posix()]
    current_catalog = run_helper(prefix + ["catalog", "--host", args.host, "--container", args.container, "--database", args.database, "--user", args.user])
    current_source = run_helper(prefix + ["sources", "--host", args.host, "--source", args.source, "--expected-commit", args.expected_commit])
    current_qdrant = run_helper(prefix + ["qdrant", "--host", args.host])
    validate_catalog_snapshot(current_catalog)
    validate_source_inventory(current_source)
    validate_qdrant_metadata(current_qdrant)
    if current_catalog.get("schema_version") != CATALOG_SCHEMA or current_source.get("schema_version") != SOURCE_SCHEMA or current_qdrant.get("schema_version") != QDRANT_SCHEMA:
        raise RuntimeError("production compatibility schema diverged")
    catalog_identity = catalog_stable_identity(current_catalog)
    if catalog_identity != catalog_stable_identity(baseline_catalog):
        raise RuntimeError("PostgreSQL catalog drifted from the candidate baseline")
    if canonical_bytes(current_source) != canonical_bytes(baseline_source):
        raise RuntimeError("repository/source/runtime metadata drifted from the candidate baseline")
    if canonical_bytes(current_qdrant) != canonical_bytes(baseline_qdrant):
        raise RuntimeError("Qdrant configuration drifted from the candidate baseline")
    report = {
        "schema_version": "schema-ledger-production-compatibility-v1",
        "production_commit": current_source["production_commit"],
        "production_tree": current_source["production_tree"],
        "production_clean": current_source["production_clean"],
        "production_ref_count": current_source["git_inventory"]["ref_count"],
        "production_ref_sha256": current_source["git_inventory"]["ref_sha256"],
        "production_worktree_count": current_source["git_inventory"]["worktree_count"],
        "production_worktree_sha256": current_source["git_inventory"]["worktree_sha256"],
        "postgres_catalog_stable_sha256": catalog_identity,
        "postgres_object_count": sum(len(items) for items in current_catalog["objects"].values()),
        "repository_source_sha256": sha256(canonical_bytes(current_source)),
        "qdrant_metadata_sha256": sha256(canonical_bytes(current_qdrant)),
        "read_only_catalog_proven": True,
        "row_data_read": False,
        "vector_payload_read": False,
        "status": "compatible",
    }
    sys.stdout.buffer.write(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("production compatibility failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
