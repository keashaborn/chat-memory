#!/usr/bin/env python3
"""Build the proposed initial ledger from sanitized, canonical evidence."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

from schema_ledger import (
    CANONICALIZATION,
    CATALOG_SCHEMA,
    LEDGER_SCHEMA,
    RECONCILIATION_SCHEMA,
    SOURCE_SCHEMA,
    QDRANT_SCHEMA,
    assert_read_only_catalog,
    canonical_bytes,
    catalog_object_records,
    migration_id,
    parse_canonical,
    reconcile,
    sha256,
    validate_ledger,
    validate_catalog_snapshot,
    validate_source_inventory,
    validate_qdrant_metadata,
)


def load(path: pathlib.Path, schema: str) -> tuple[bytes, dict[str, object]]:
    raw = path.read_bytes()
    value = parse_canonical(raw)
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise RuntimeError(path.name + " has the wrong schema")
    return raw, value


def lanes() -> list[dict[str, object]]:
    return [
        {"id": "personal_memory", "authority": "postgres", "schemas": ["memory"], "object_prefixes": ["memory.claim", "memory.evidence", "memory.observation"], "status": "active"},
        {"id": "response_preferences", "authority": "postgres", "schemas": ["user_settings"], "object_prefixes": ["memory.assistant_response_preference"], "status": "active"},
        {"id": "life_preferences", "authority": "postgres", "schemas": ["memory"], "object_prefixes": ["memory.preference"], "status": "active"},
        {"id": "projects", "authority": "postgres", "schemas": ["memory"], "object_prefixes": ["memory.project"], "status": "active"},
        {"id": "lifeswitch_live_context", "authority": "postgres", "schemas": ["lifeswitch_agentic", "lifeswitch_chat", "lifeswitch_nutrition", "lifeswitch_people", "lifeswitch_plan", "lifeswitch_training", "lifeswitch_usage"], "object_prefixes": ["memory.lifeswitch_measurement"], "status": "separate"},
        {"id": "fractal_monism_policy", "authority": "policy_only", "schemas": [], "object_prefixes": ["qdrant.fm_"], "status": "separate"},
        {"id": "audit_review", "authority": "postgres", "schemas": ["memory"], "object_prefixes": ["memory.audit", "memory.review", "memory.trace"], "status": "active"},
        {"id": "derived_index_coordination", "authority": "qdrant_derived", "schemas": ["memory"], "object_prefixes": ["memory.projection", "qdrant.memory_"], "status": "separate"},
        {"id": "other_application", "authority": "postgres", "schemas": ["ai_operations", "catalog_dev", "public", "trusted_web", "vantage_card", "vantage_fact", "vantage_identity", "vantage_initiator", "vantage_profile"], "object_prefixes": [], "status": "separate"},
        {"id": "schema_governance", "authority": "absent_observed", "schemas": [], "object_prefixes": ["schema_ledger"], "status": "absent_observed"},
    ]


def migration_records(source: dict[str, object]) -> list[dict[str, object]]:
    sources = source.get("sources")
    if not isinstance(sources, list):
        raise RuntimeError("source inventory is malformed")
    by_path: dict[str, dict[str, object]] = {}
    hashes: dict[str, list[str]] = collections.defaultdict(list)
    for item in sources:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in by_path:
            raise RuntimeError("source path is missing or duplicated")
        by_path[item["path"]] = item
        hashes[str(item.get("sha256"))].append(item["path"])
    primary_by_hash = {digest: min(paths) for digest, paths in hashes.items() if len(paths) > 1}
    id_by_path = {path: migration_id(path) for path in by_path}
    rollback_targets = {str(item["rollback_target"]) for item in sources if isinstance(item, dict) and isinstance(item.get("rollback_target"), str)}
    if len(id_by_path) != len(set(id_by_path.values())):
        raise RuntimeError("migration identifiers collide")
    output: list[dict[str, object]] = []
    for order, path in enumerate(sorted(by_path), 1):
        item = by_path[path]
        digest = str(item["sha256"])
        primary_path = primary_by_hash.get(digest)
        duplicate_of = None
        classification = "source-only" if item["kind"] == "test_fixture" else "unverifiable"
        if primary_path is not None and path != primary_path:
            duplicate_of = id_by_path[primary_path]
            classification = "duplicated"
        rollback_target = item.get("rollback_target")
        rollback_for = id_by_path.get(rollback_target) if isinstance(rollback_target, str) else None
        evidence = "test-only source; not production migration evidence" if item["kind"] == "test_fixture" else "no production migration-history relation; apply state is unverifiable"
        output.append(
            {
                "id": id_by_path[path],
                "path": path,
                "sha256": digest,
                "git_blob": item["git_blob"],
                "size": item["size"],
                "mode": item["mode"],
                "kind": item["kind"],
                "lane": item["lane"],
                "order": order,
                "dependencies": [],
                "classification": classification,
                "duplicate_of": duplicate_of,
                "rollback_for": rollback_for,
                "execution_owner": "unverifiable",
                "transaction_mode": item["transaction_mode"],
                "recovery": "rollback_script" if item["kind"] == "rollback" else ("test_only" if item["kind"] == "test_fixture" else ("paired_rollback" if path in rollback_targets else "none_declared")),
                "unsafe_categories": item["unsafe_categories"],
                "declared_objects": item["declared_objects"],
                "apply_evidence": evidence,
            }
        )
    return output


def object_expectations(catalog: dict[str, object]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in catalog_object_records(catalog):
        output.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "schema": item["schema"],
                "name": item["name"],
                "lane": item["lane"],
                "fingerprint_sha256": item["fingerprint_sha256"],
                "classification": "matched",
                "source_migrations": [],
            }
        )
    return output


def build_reconciliation(commit: str, migrations: list[dict[str, object]], expectations: list[dict[str, object]], catalog: dict[str, object], source: dict[str, object], qdrant: dict[str, object]) -> dict[str, object]:
    observed = catalog_object_records(catalog)
    catalog_result = reconcile(expectations, observed)
    migration_counts = collections.Counter(str(item["classification"]) for item in migrations)
    object_counts = {name: len(values) for name, values in sorted(catalog["objects"].items())}
    qdrant_relationships = []
    for item in qdrant.get("collections", []):
        fields = [field["name"] + ":" + field["data_type"] for field in item.get("payload_schema", [])]
        qdrant_relationships.append({"collection": item["name"], "payload_schema": fields, "authority": "derived_rebuildable"})
    return {
        "schema_version": RECONCILIATION_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "production_commit": commit,
        "catalog_object_counts": object_counts,
        "catalog_reconciliation": catalog_result,
        "migration_classifications": dict(sorted(migration_counts.items())),
        "migration_history_evidence": {"relation_count": len(catalog.get("migration_history_relations", [])), "row_data_read": False, "historical_apply_state": "unverifiable"},
        "repository_source_summary": source["summary"],
        "qdrant_relationships": qdrant_relationships,
        "discrepancies": [
            "production has no migration-history relation",
            "repository SQL files have no unique catalog-object provenance",
            "current catalog can seed an intended baseline but cannot prove historical apply order",
            "Qdrant is a separate derived and rebuildable index, not schema authority",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=pathlib.Path, required=True)
    parser.add_argument("--sources", type=pathlib.Path, required=True)
    parser.add_argument("--qdrant", type=pathlib.Path, required=True)
    parser.add_argument("--production-tree", required=True)
    parser.add_argument("--ledger-output", type=pathlib.Path, required=True)
    parser.add_argument("--reconciliation-output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    catalog_raw, catalog = load(args.catalog, CATALOG_SCHEMA)
    source_raw, source = load(args.sources, SOURCE_SCHEMA)
    qdrant_raw, qdrant = load(args.qdrant, QDRANT_SCHEMA)
    assert_read_only_catalog(catalog)
    validate_catalog_snapshot(catalog)
    validate_source_inventory(source)
    validate_qdrant_metadata(qdrant)
    if qdrant.get("authority") != "derived_rebuildable" or qdrant.get("point_payload_read") is not False or qdrant.get("point_search_or_scroll") is not False:
        raise RuntimeError("Qdrant evidence crossed the metadata-only boundary")
    commit = source.get("production_commit")
    if not isinstance(commit, str):
        raise RuntimeError("production commit is missing")
    if source.get("production_tree") != args.production_tree:
        raise RuntimeError("production tree evidence diverged")
    migrations = migration_records(source)
    expectations = object_expectations(catalog)
    reconciliation = build_reconciliation(commit, migrations, expectations, catalog, source, qdrant)
    reconciliation_raw = canonical_bytes(reconciliation)
    ledger = {
        "schema_version": LEDGER_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "ledger_id": "governed-memory-v1-initial-production-baseline",
        "authority": {"live_schema": "postgres", "intended_history": "schema_ledger", "derived_index": "qdrant_rebuildable", "extraction": "proposal_only"},
        "baseline": {
            "production_commit": commit,
            "production_tree": args.production_tree,
            "catalog_evidence_path": "evidence/" + args.catalog.name,
            "catalog_evidence_sha256": sha256(catalog_raw),
            "source_evidence_path": "evidence/" + args.sources.name,
            "source_evidence_sha256": sha256(source_raw),
            "qdrant_evidence_path": "evidence/" + args.qdrant.name,
            "qdrant_evidence_sha256": sha256(qdrant_raw),
            "reconciliation_evidence_path": "evidence/" + args.reconciliation_output.name,
            "reconciliation_evidence_sha256": sha256(reconciliation_raw),
            "production_ref_count": source["git_inventory"]["ref_count"],
            "production_ref_sha256": source["git_inventory"]["ref_sha256"],
            "production_worktree_count": source["git_inventory"]["worktree_count"],
            "production_worktree_sha256": source["git_inventory"]["worktree_sha256"],
        },
        "lanes": lanes(),
        "migrations": migrations,
        "object_expectations": expectations,
    }
    validate_ledger(ledger)
    args.reconciliation_output.write_bytes(reconciliation_raw)
    args.ledger_output.write_bytes(canonical_bytes(ledger))
    print(json.dumps({"ledger_sha256": sha256(canonical_bytes(ledger)), "migration_count": len(migrations), "object_count": len(expectations)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("ledger build failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
