#!/usr/bin/env python3
"""Canonical schema-ledger primitives.

The module is intentionally offline by default.  It accepts only sanitized
catalog metadata and Git object bytes; it never opens a database connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from source_record_projection import analyze_sql, project_source_record, sanitize_text, source_kind, source_lane


LEDGER_SCHEMA = "governed-memory-schema-ledger-v1"
CATALOG_SCHEMA = "postgres-catalog-snapshot-v1"
SOURCE_SCHEMA = "repository-schema-source-inventory-v1"
RECONCILIATION_SCHEMA = "schema-ledger-reconciliation-v1"
QDRANT_SCHEMA = "qdrant-derived-index-metadata-v1"
CANONICALIZATION = "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,159}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

LANES = (
    "personal_memory",
    "response_preferences",
    "life_preferences",
    "projects",
    "lifeswitch_live_context",
    "fractal_monism_policy",
    "audit_review",
    "derived_index_coordination",
    "other_application",
    "schema_governance",
)

CLASSIFICATIONS = {
    "matched",
    "pending",
    "production-only/orphaned",
    "source-only",
    "divergent",
    "duplicated",
    "superseded",
    "unverifiable",
}

LEDGER_FIELDS = {
    "schema_version",
    "canonicalization",
    "ledger_id",
    "authority",
    "baseline",
    "lanes",
    "migrations",
    "object_expectations",
}
AUTHORITY_FIELDS = {"live_schema", "intended_history", "derived_index", "extraction"}
BASELINE_FIELDS = {
    "production_commit",
    "production_tree",
    "catalog_evidence_path",
    "catalog_evidence_sha256",
    "source_evidence_path",
    "source_evidence_sha256",
    "qdrant_evidence_path",
    "qdrant_evidence_sha256",
    "reconciliation_evidence_path",
    "reconciliation_evidence_sha256",
    "production_ref_count",
    "production_ref_sha256",
    "production_worktree_count",
    "production_worktree_sha256",
}
LANE_FIELDS = {"id", "authority", "schemas", "object_prefixes", "status"}
MIGRATION_FIELDS = {
    "id",
    "path",
    "sha256",
    "git_blob",
    "size",
    "mode",
    "kind",
    "lane",
    "order",
    "dependencies",
    "classification",
    "duplicate_of",
    "rollback_for",
    "execution_owner",
    "transaction_mode",
    "recovery",
    "unsafe_categories",
    "declared_objects",
    "apply_evidence",
}
OBJECT_FIELDS = {
    "id",
    "kind",
    "schema",
    "name",
    "lane",
    "fingerprint_sha256",
    "classification",
    "source_migrations",
}
CATALOG_FIELDS = {"schema_version", "canonicalization", "source", "read_only_proof", "observed_at_utc", "database", "objects", "scheduler_capabilities", "migration_history_relations", "schema_only_dump"}
CATALOG_OBJECT_KINDS = {"schema", "relation", "column", "constraint", "index", "sequence", "view", "function", "trigger", "policy", "type", "extension", "grant", "scheduled_job"}
SOURCE_FIELDS = {"schema_version", "canonicalization", "production_commit", "production_tree", "production_clean", "git_inventory", "tracked_blob_count", "sources", "runtime_paths", "systemd_units", "scheduled_jobs", "cron_jobs", "duplicate_hashes", "summary"}
SOURCE_RECORD_FIELDS = {"path", "mode", "git_blob", "sha256", "size", "kind", "lane", "unsafe_categories", "declared_objects", "rollback_target", "transaction_mode"}


class LedgerError(RuntimeError):
    pass


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_scalar(value: object, location: str) -> None:
    if isinstance(value, float):
        raise LedgerError(location + " contains a floating-point value")
    if isinstance(value, str) and CONTROL_RE.search(value):
        raise LedgerError(location + " contains a control character")


def _validate_json_value(value: object, location: str = "root") -> None:
    _validate_scalar(value, location)
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise LedgerError(location + " contains a non-string key")
            _validate_scalar(key, location + ".key")
            _validate_json_value(child, location + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{location}[{index}]")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise LedgerError(location + " contains an unsupported value")


def canonical_bytes(value: object) -> bytes:
    _validate_json_value(value)
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise LedgerError("duplicate JSON field: " + key)
        output[key] = value
    return output


def parse_canonical(payload: bytes, *, maximum: int = 64 * 1024 * 1024) -> object:
    if not payload or len(payload) > maximum or b"\0" in payload or b"\r" in payload:
        raise LedgerError("canonical payload size or encoding is invalid")
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerError("canonical payload cannot be parsed") from error
    if payload != canonical_bytes(value):
        raise LedgerError("payload is not the exact canonical encoding")
    return value


def require_sha256(value: object, location: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise LedgerError(location + " is not a lowercase SHA-256")
    return value


def require_id(value: object, location: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise LedgerError(location + " is malformed")
    return value


def require_relative_path(value: object, location: str) -> str:
    if not isinstance(value, str) or not value or CONTROL_RE.search(value):
        raise LedgerError(location + " is malformed")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or ".." in path.parts or "." in path.parts:
        raise LedgerError(location + " is not a canonical relative path")
    return value


def _require_exact_fields(value: object, expected: set[str], location: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise LedgerError(location + " fields are invalid")
    return value


def validate_ledger(value: object) -> dict[str, object]:
    ledger = _require_exact_fields(value, LEDGER_FIELDS, "ledger")
    if ledger["schema_version"] != LEDGER_SCHEMA or ledger["canonicalization"] != CANONICALIZATION:
        raise LedgerError("ledger schema or canonicalization is unsupported")
    require_id(ledger["ledger_id"], "ledger_id")
    authority = _require_exact_fields(ledger["authority"], AUTHORITY_FIELDS, "authority")
    if authority != {
        "live_schema": "postgres",
        "intended_history": "schema_ledger",
        "derived_index": "qdrant_rebuildable",
        "extraction": "proposal_only",
    }:
        raise LedgerError("authority boundary is invalid")
    baseline = _require_exact_fields(ledger["baseline"], BASELINE_FIELDS, "baseline")
    for name in ("production_commit", "production_tree"):
        if not isinstance(baseline[name], str) or not COMMIT_RE.fullmatch(baseline[name]):
            raise LedgerError("baseline " + name + " is invalid")
    for name in ("catalog", "source", "qdrant", "reconciliation"):
        require_relative_path(baseline[name + "_evidence_path"], "baseline evidence path")
        require_sha256(baseline[name + "_evidence_sha256"], "baseline evidence hash")
    for count_name in ("production_ref_count", "production_worktree_count"):
        if not isinstance(baseline[count_name], int) or isinstance(baseline[count_name], bool) or baseline[count_name] < 1:
            raise LedgerError("baseline Git count is invalid")
    for hash_name in ("production_ref_sha256", "production_worktree_sha256"):
        require_sha256(baseline[hash_name], "baseline Git hash")

    lanes = ledger["lanes"]
    if not isinstance(lanes, list) or any(not isinstance(item, dict) for item in lanes):
        raise LedgerError("lane inventory is invalid")
    lane_ids: list[str] = []
    for index, raw in enumerate(lanes):
        item = _require_exact_fields(raw, LANE_FIELDS, f"lanes[{index}]")
        lane_id = require_id(item["id"], "lane id")
        lane_ids.append(lane_id)
        if item["authority"] not in {"postgres", "qdrant_derived", "policy_only", "absent_observed"}:
            raise LedgerError("lane authority is invalid")
        if item["status"] not in {"active", "separate", "absent_observed"}:
            raise LedgerError("lane status is invalid")
        for field in ("schemas", "object_prefixes"):
            if not isinstance(item[field], list) or item[field] != sorted(set(item[field])) or any(not isinstance(part, str) for part in item[field]):
                raise LedgerError("lane mapping is not canonical")
    if tuple(lane_ids) != LANES:
        raise LedgerError("lane order or coverage is invalid")

    migrations = ledger["migrations"]
    if not isinstance(migrations, list) or any(not isinstance(item, dict) for item in migrations):
        raise LedgerError("migration inventory is invalid")
    migration_ids: set[str] = set()
    paths: set[str] = set()
    hashes: dict[str, list[str]] = defaultdict(list)
    graph: dict[str, tuple[str, ...]] = {}
    previous_order = -1
    for index, raw in enumerate(migrations):
        item = _require_exact_fields(raw, MIGRATION_FIELDS, f"migrations[{index}]")
        migration_id = require_id(item["id"], "migration id")
        path = require_relative_path(item["path"], "migration path")
        digest = require_sha256(item["sha256"], "migration hash")
        if migration_id in migration_ids or path in paths:
            raise LedgerError("duplicate migration id or path")
        migration_ids.add(migration_id)
        paths.add(path)
        hashes[digest].append(migration_id)
        if not isinstance(item["git_blob"], str) or not COMMIT_RE.fullmatch(item["git_blob"]):
            raise LedgerError("migration Git blob is invalid")
        if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
            raise LedgerError("migration size is invalid")
        if item["mode"] not in {"100644", "100755"}:
            raise LedgerError("migration mode is invalid")
        if item["kind"] not in {"forward", "rollback", "bootstrap", "test_fixture", "schema_source"}:
            raise LedgerError("migration kind is invalid")
        if item["execution_owner"] != "unverifiable":
            raise LedgerError("migration execution ownership is unsupported")
        if item["transaction_mode"] not in {"explicit", "nontransactional", "unspecified"}:
            raise LedgerError("migration transaction mode is invalid")
        if item["recovery"] not in {"paired_rollback", "rollback_script", "test_only", "none_declared"}:
            raise LedgerError("migration recovery classification is invalid")
        if item["lane"] not in LANES or item["classification"] not in CLASSIFICATIONS:
            raise LedgerError("migration lane or classification is invalid")
        order = item["order"]
        if not isinstance(order, int) or isinstance(order, bool) or order <= previous_order:
            raise LedgerError("migration ordering is not strictly increasing")
        previous_order = order
        for list_field in ("dependencies", "unsafe_categories", "declared_objects"):
            if not isinstance(item[list_field], list) or item[list_field] != sorted(set(item[list_field])) or any(not isinstance(part, str) for part in item[list_field]):
                raise LedgerError("migration list field is not canonical")
        graph[migration_id] = tuple(item["dependencies"])
        for nullable in ("duplicate_of", "rollback_for"):
            if item[nullable] is not None:
                require_id(item[nullable], nullable)
        evidence = item["apply_evidence"]
        if not isinstance(evidence, str) or CONTROL_RE.search(evidence) or len(evidence) > 240:
            raise LedgerError("migration apply evidence is invalid")
        if item["classification"] == "matched" and evidence in {"", "none", "unverifiable"}:
            raise LedgerError("matched migration lacks evidence")

    for digest, ids in hashes.items():
        if len(ids) <= 1:
            continue
        primary = min(ids)
        by_id = {item["id"]: item for item in migrations}
        for migration_id in ids:
            item = by_id[migration_id]
            if migration_id == primary:
                continue
            if item["classification"] != "duplicated" or item["duplicate_of"] != primary:
                raise LedgerError("duplicate migration hash is not acknowledged")
    for migration_id, dependencies in graph.items():
        if any(value not in graph for value in dependencies):
            raise LedgerError("migration dependency is missing")
        if migration_id in dependencies:
            raise LedgerError("migration self-dependency")
    _validate_acyclic(graph)

    objects = ledger["object_expectations"]
    if not isinstance(objects, list) or any(not isinstance(item, dict) for item in objects):
        raise LedgerError("object expectation inventory is invalid")
    object_ids: set[str] = set()
    previous_id = ""
    for index, raw in enumerate(objects):
        item = _require_exact_fields(raw, OBJECT_FIELDS, f"object_expectations[{index}]")
        object_id = require_id(item["id"], "object id")
        if object_id in object_ids or object_id <= previous_id:
            raise LedgerError("object ids are duplicated or unsorted")
        object_ids.add(object_id)
        previous_id = object_id
        if item["kind"] not in {"schema", "relation", "column", "constraint", "index", "sequence", "view", "function", "trigger", "policy", "type", "extension", "grant", "scheduled_job"}:
            raise LedgerError("object kind is invalid")
        if item["lane"] not in LANES or item["classification"] not in CLASSIFICATIONS:
            raise LedgerError("object lane or classification is invalid")
        for name in ("schema", "name"):
            if not isinstance(item[name], str) or not item[name] or CONTROL_RE.search(item[name]) or len(item[name]) > 512:
                raise LedgerError("object identifier is invalid")
        require_sha256(item["fingerprint_sha256"], "object fingerprint")
        if not isinstance(item["source_migrations"], list) or item["source_migrations"] != sorted(set(item["source_migrations"])):
            raise LedgerError("object source migrations are not canonical")
        if any(value not in migration_ids for value in item["source_migrations"]):
            raise LedgerError("object references an unknown migration")
    return ledger


def _validate_acyclic(graph: Mapping[str, Sequence[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise LedgerError("migration dependency cycle")
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)


def lane_for(schema: str, name: str, path: str = "") -> str:
    value = (schema + "." + name + " " + path).lower()
    if not schema and not name:
        return source_lane(path)
    if "fractal" in value or "monism" in value:
        return "fractal_monism_policy"
    if schema.startswith("lifeswitch_") or "lifeswitch" in value or name == "lifeswitch_measurement_entries":
        return "lifeswitch_live_context"
    if schema == "user_settings" or "response_preference" in value or "assistant_preference" in value:
        return "response_preferences"
    if "project" in value:
        return "projects"
    if "preference" in value:
        return "life_preferences"
    if schema == "memory":
        if any(token in value for token in ("review", "audit", "trace", "assessment", "reconciliation")):
            return "audit_review"
        if any(token in value for token in ("projection", "outbox", "retrieval")):
            return "derived_index_coordination"
        return "personal_memory"
    if "schema_ledger" in value or "migration_ledger" in value:
        return "schema_governance"
    return "other_application"


def migration_id(path: str) -> str:
    stem = pathlib.PurePosixPath(path).with_suffix("").as_posix().lower()
    value = re.sub(r"[^a-z0-9_.:-]+", "-", stem.replace("/", ":")).strip("-")
    if not value or not value[0].isalpha():
        value = "m:" + value
    if len(value) > 160:
        value = value[:95] + ":" + sha256(path.encode("utf-8"))[:64]
    return value


@dataclass(frozen=True)
class GitBlob:
    path: str
    mode: str
    oid: str
    payload: bytes


def git_sql_blobs(repository: pathlib.Path, commit: str, runner: object = subprocess) -> list[GitBlob]:
    if not repository.is_absolute() or pathlib.Path(os.path.realpath(repository)) != repository:
        raise LedgerError("repository path must be absolute and real")
    if not COMMIT_RE.fullmatch(commit):
        raise LedgerError("commit is malformed")
    environment = {"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
    tree = runner.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "ls-tree", "-r", "-z", commit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=120,
        env=environment,
    )
    if tree.returncode != 0 or len(tree.stdout) > 64 * 1024 * 1024:
        raise LedgerError("Git tree inventory failed")
    entries: list[tuple[str, str, str]] = []
    for token in tree.stdout.split(b"\0"):
        if not token:
            continue
        metadata, separator, raw_path = token.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3 or parts[1] != b"blob":
            continue
        path = os.fsdecode(raw_path)
        require_relative_path(path, "Git path")
        if path.lower().endswith(".sql"):
            entries.append((path, parts[0].decode("ascii"), parts[2].decode("ascii")))
    process = runner.Popen(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    blobs: list[GitBlob] = []
    try:
        for path, mode, oid in entries:
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", "strict").strip().split()
            if len(header) != 3 or header[1] != "blob":
                raise LedgerError("Git cat-file header is invalid")
            size = int(header[2])
            if size < 0 or size > 64 * 1024 * 1024:
                raise LedgerError("SQL source exceeds the bounded size")
            payload = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if len(payload) != size or terminator != b"\n" or b"\0" in payload:
                raise LedgerError("Git cat-file payload is invalid")
            blobs.append(GitBlob(path, mode, oid, payload))
    finally:
        process.stdin.close()
        returncode = process.wait(timeout=30)
        process.stdout.close()
    if returncode != 0:
        raise LedgerError("Git cat-file inventory failed")
    return blobs


def build_source_inventory(blobs: Iterable[GitBlob], commit: str) -> dict[str, object]:
    records = [
        project_source_record(blob.path, blob.mode, blob.oid, blob.payload)
        for blob in sorted(blobs, key=lambda item: item.path)
    ]
    return {
        "schema_version": SOURCE_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "production_commit": commit,
        "sources": records,
    }


def fingerprint_record(value: Mapping[str, object]) -> str:
    return sha256(canonical_bytes(dict(value)))


def catalog_object_records(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    if snapshot.get("schema_version") != CATALOG_SCHEMA or snapshot.get("canonicalization") != CANONICALIZATION:
        raise LedgerError("catalog snapshot schema is invalid")
    arrays = snapshot.get("objects")
    if not isinstance(arrays, dict):
        raise LedgerError("catalog object inventory is invalid")
    records: list[dict[str, object]] = []
    for kind in sorted(arrays):
        values = arrays[kind]
        if not isinstance(values, list):
            raise LedgerError("catalog object category is invalid")
        for item in values:
            if not isinstance(item, dict):
                raise LedgerError("catalog object is invalid")
            schema = str(item.get("schema", "global"))
            name = str(item.get("name", ""))
            if not name:
                raise LedgerError("catalog object name is missing")
            identity = str(item.get("identity", name))
            object_id = re.sub(r"[^a-z0-9_.:-]+", "-", f"{kind}:{schema}.{identity}".lower()).strip("-")
            if len(object_id) > 160:
                object_id = object_id[:95] + ":" + sha256(object_id.encode())[:64]
            records.append(
                {
                    "id": object_id,
                    "kind": kind,
                    "schema": schema,
                    "name": name,
                    "lane": lane_for(schema, name),
                    "fingerprint_sha256": fingerprint_record(item),
                    "raw": item,
                }
            )
    records.sort(key=lambda item: item["id"])
    ids = [item["id"] for item in records]
    if len(ids) != len(set(ids)):
        raise LedgerError("catalog object identities collide")
    return records


def reconcile(expected: Iterable[Mapping[str, object]], observed: Iterable[Mapping[str, object]]) -> dict[str, object]:
    expected_items = list(expected)
    observed_items = list(observed)
    expected_map = {str(item["id"]): item for item in expected_items}
    observed_map = {str(item["id"]): item for item in observed_items}
    if len(expected_map) != len(expected_items) or len(observed_map) != len(observed_items):
        raise LedgerError("duplicate reconciliation object id")
    results: list[dict[str, str]] = []
    for object_id in sorted(set(expected_map) | set(observed_map)):
        left = expected_map.get(object_id)
        right = observed_map.get(object_id)
        if left is None:
            status = "production-only/orphaned"
        elif right is None:
            status = "source-only"
        elif left["fingerprint_sha256"] == right["fingerprint_sha256"]:
            status = "matched"
        else:
            status = "divergent"
        results.append({"id": object_id, "classification": status})
    counts: dict[str, int] = defaultdict(int)
    for item in results:
        counts[item["classification"]] += 1
    return {
        "schema_version": RECONCILIATION_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "counts": dict(sorted(counts.items())),
        "objects": results,
    }


def assert_read_only_catalog(snapshot: Mapping[str, object]) -> None:
    proof = snapshot.get("read_only_proof")
    if not isinstance(proof, dict) or proof.get("transaction_read_only") != "on" or proof.get("transaction_isolation") != "repeatable read":
        raise LedgerError("catalog snapshot lacks read-only transaction proof")
    if proof.get("row_data_read") is not False or proof.get("vector_payload_read") is not False:
        raise LedgerError("catalog snapshot crossed the metadata-only boundary")


def validate_catalog_snapshot(snapshot: object) -> dict[str, object]:
    value = _require_exact_fields(snapshot, CATALOG_FIELDS, "catalog snapshot")
    if value["schema_version"] != CATALOG_SCHEMA or value["canonicalization"] != CANONICALIZATION:
        raise LedgerError("catalog snapshot version is unsupported")
    assert_read_only_catalog(value)
    if not isinstance(value["observed_at_utc"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value["observed_at_utc"]):
        raise LedgerError("catalog timestamp is malformed")
    source = _require_exact_fields(value["source"], {"container", "database", "user"}, "catalog source")
    if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item) for item in source.values()):
        raise LedgerError("catalog source coordinate is malformed")
    database = _require_exact_fields(value["database"], {"name", "owner", "server_version", "server_version_num", "encoding", "collation", "ctype", "acl"}, "database")
    if any(not isinstance(database[name], str) or not database[name] for name in ("name", "owner", "server_version", "server_version_num", "encoding", "collation", "ctype")):
        raise LedgerError("database metadata is malformed")
    if not isinstance(database["acl"], list) or len(database["acl"]) != len(set(database["acl"])) or any(not isinstance(item, str) for item in database["acl"]):
        raise LedgerError("database grants are not canonical")
    objects = value["objects"]
    if not isinstance(objects, dict) or set(objects) != CATALOG_OBJECT_KINDS:
        raise LedgerError("catalog object categories are incomplete")
    catalog_object_records(value)
    scheduler = _require_exact_fields(value["scheduler_capabilities"], {"pg_cron_installed", "timescaledb_installed", "database_job_rows_read"}, "scheduler capabilities")
    if any(not isinstance(item, bool) for item in scheduler.values()) or scheduler["database_job_rows_read"] is not False:
        raise LedgerError("scheduler metadata crossed the row boundary")
    history = value["migration_history_relations"]
    if not isinstance(history, list) or any(not isinstance(item, dict) or set(item) != {"schema", "name", "kind"} for item in history):
        raise LedgerError("migration-history relation inventory is malformed")
    dump = _require_exact_fields(value["schema_only_dump"], {"sha256", "raw_size", "canonical_size", "canonicalization", "contains_row_data", "retained"}, "schema-only dump")
    require_sha256(dump["sha256"], "schema-only dump hash")
    if dump["canonicalization"] != "strip-matched-pg-restrict-token-lf-v1" or not isinstance(dump["raw_size"], int) or isinstance(dump["raw_size"], bool) or not isinstance(dump["canonical_size"], int) or isinstance(dump["canonical_size"], bool) or dump["canonical_size"] <= 0 or dump["raw_size"] < dump["canonical_size"] or dump["contains_row_data"] is not False or dump["retained"] is not False:
        raise LedgerError("schema-only dump boundary is invalid")
    return value


def validate_source_inventory(snapshot: object) -> dict[str, object]:
    value = _require_exact_fields(snapshot, SOURCE_FIELDS, "source inventory")
    if value["schema_version"] != SOURCE_SCHEMA or value["canonicalization"] != CANONICALIZATION or value["production_clean"] is not True:
        raise LedgerError("source inventory header is invalid")
    if not isinstance(value["production_commit"], str) or not COMMIT_RE.fullmatch(value["production_commit"]):
        raise LedgerError("source inventory commit is invalid")
    if not isinstance(value["production_tree"], str) or not COMMIT_RE.fullmatch(value["production_tree"]):
        raise LedgerError("source inventory tree is invalid")
    git_inventory = _require_exact_fields(value["git_inventory"], {"ref_count", "ref_sha256", "worktree_count", "worktree_sha256"}, "Git inventory")
    for count_name in ("ref_count", "worktree_count"):
        if not isinstance(git_inventory[count_name], int) or isinstance(git_inventory[count_name], bool) or git_inventory[count_name] < 1:
            raise LedgerError("Git inventory count is invalid")
    for hash_name in ("ref_sha256", "worktree_sha256"):
        require_sha256(git_inventory[hash_name], "Git inventory hash")
    sources = value["sources"]
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise LedgerError("source records are invalid")
    paths: list[str] = []
    computed_hashes: dict[str, list[str]] = defaultdict(list)
    for index, raw in enumerate(sources):
        item = _require_exact_fields(raw, SOURCE_RECORD_FIELDS, f"sources[{index}]")
        path = require_relative_path(item["path"], "source path")
        paths.append(path)
        require_sha256(item["sha256"], "source hash")
        computed_hashes[item["sha256"]].append(path)
        if not isinstance(item["git_blob"], str) or not COMMIT_RE.fullmatch(item["git_blob"]):
            raise LedgerError("source Git blob is invalid")
        if item["mode"] not in {"100644", "100755"} or not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
            raise LedgerError("source file metadata is invalid")
        if item["kind"] not in {"forward", "rollback", "bootstrap", "test_fixture", "schema_source"} or item["lane"] not in LANES:
            raise LedgerError("source classification is invalid")
        if item["transaction_mode"] not in {"explicit", "nontransactional", "unspecified"}:
            raise LedgerError("source transaction classification is invalid")
        if item["rollback_target"] is not None:
            require_relative_path(item["rollback_target"], "rollback target")
        for field in ("unsafe_categories", "declared_objects"):
            if not isinstance(item[field], list) or item[field] != sorted(set(item[field])) or any(not isinstance(entry, str) for entry in item[field]):
                raise LedgerError("source list field is not canonical")
    if paths != sorted(set(paths)):
        raise LedgerError("source paths are duplicated or unsorted")
    duplicates = [{"sha256": digest, "paths": sorted(items)} for digest, items in sorted(computed_hashes.items()) if len(items) > 1]
    if value["duplicate_hashes"] != duplicates:
        raise LedgerError("duplicate-source inventory is invalid")
    if not isinstance(value["tracked_blob_count"], int) or value["tracked_blob_count"] < len(sources):
        raise LedgerError("tracked-blob count is invalid")
    runtime_paths = value["runtime_paths"]
    systemd_units = value["systemd_units"]
    scheduled_jobs = value["scheduled_jobs"]
    cron_jobs = value["cron_jobs"]
    if not isinstance(runtime_paths, list) or not isinstance(systemd_units, list) or not isinstance(scheduled_jobs, list) or not isinstance(cron_jobs, list) or not isinstance(value["summary"], dict):
        raise LedgerError("runtime source metadata is invalid")
    runtime_names: list[str] = []
    for index, raw in enumerate(runtime_paths):
        item = _require_exact_fields(raw, {"path", "mode", "git_blob", "sha256", "size", "signals", "referenced_sql_paths"}, f"runtime_paths[{index}]")
        runtime_names.append(require_relative_path(item["path"], "runtime path"))
        require_sha256(item["sha256"], "runtime source hash")
        if item["mode"] not in {"100644", "100755"} or not isinstance(item["git_blob"], str) or not COMMIT_RE.fullmatch(item["git_blob"]) or not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
            raise LedgerError("runtime source file metadata is invalid")
        for field in ("signals", "referenced_sql_paths"):
            if not isinstance(item[field], list) or item[field] != sorted(set(item[field])) or any(not isinstance(entry, str) for entry in item[field]):
                raise LedgerError("runtime source list field is invalid")
        for reference in item["referenced_sql_paths"]:
            require_relative_path(reference, "runtime SQL reference")
    if runtime_names != sorted(set(runtime_names)):
        raise LedgerError("runtime paths are duplicated or unsorted")
    unit_names: list[str] = []
    for index, raw in enumerate(systemd_units):
        item = _require_exact_fields(raw, {"unit", "unit_type", "sha256", "size", "signals"}, f"systemd_units[{index}]")
        unit_names.append(item["unit"])
        require_sha256(item["sha256"], "systemd unit hash")
        if not isinstance(item["unit"], str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,200}", item["unit"]) or item["unit_type"] not in {"service", "timer", "path", "socket", "target", "mount", "automount", "slice", "scope", "other"} or not isinstance(item["size"], int) or item["size"] < 0 or item["signals"] != sorted(set(item["signals"])):
            raise LedgerError("systemd unit metadata is invalid")
    if unit_names != sorted(set(unit_names)):
        raise LedgerError("systemd units are duplicated or unsorted")
    timer_names: list[str] = []
    for index, raw in enumerate(scheduled_jobs):
        item = _require_exact_fields(raw, {"unit", "activates", "sha256", "size", "schedule_sha256", "schedule_field_count"}, f"scheduled_jobs[{index}]")
        timer_names.append(item["unit"])
        require_sha256(item["sha256"], "timer unit hash")
        require_sha256(item["schedule_sha256"], "timer schedule hash")
        if not isinstance(item["unit"], str) or not isinstance(item["activates"], str) or item["activates"] not in unit_names or not item["unit"].endswith(".timer") or not item["activates"].endswith(".service") or not isinstance(item["size"], int) or item["size"] < 0 or not isinstance(item["schedule_field_count"], int) or item["schedule_field_count"] < 0:
            raise LedgerError("scheduled-job metadata is invalid")
    if timer_names != sorted(set(timer_names)):
        raise LedgerError("scheduled jobs are duplicated or unsorted")
    cron_names: list[str] = []
    for index, raw in enumerate(cron_jobs):
        item = _require_exact_fields(raw, {"name", "sha256", "size"}, f"cron_jobs[{index}]")
        cron_names.append(item["name"])
        require_sha256(item["sha256"], "cron job hash")
        if not isinstance(item["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", item["name"]) or not isinstance(item["size"], int) or item["size"] < 0:
            raise LedgerError("cron job metadata is invalid")
    if cron_names != sorted(set(cron_names)):
        raise LedgerError("cron jobs are duplicated or unsorted")
    summary = _require_exact_fields(value["summary"], {"source_count", "runtime_path_count", "scheduled_job_count", "systemd_unit_count", "cron_job_count", "source_kind_counts", "lane_counts"}, "source summary")
    expected_counts = {"source_count": len(sources), "runtime_path_count": len(runtime_paths), "scheduled_job_count": len(scheduled_jobs), "systemd_unit_count": len(systemd_units), "cron_job_count": len(cron_jobs)}
    if any(summary[name] != count for name, count in expected_counts.items()):
        raise LedgerError("source summary counts diverged")
    kind_counts: dict[str, int] = defaultdict(int)
    lane_counts: dict[str, int] = defaultdict(int)
    for item in sources:
        kind_counts[item["kind"]] += 1
        lane_counts[item["lane"]] += 1
    if summary["source_kind_counts"] != dict(sorted(kind_counts.items())) or summary["lane_counts"] != dict(sorted(lane_counts.items())):
        raise LedgerError("source summary classifications diverged")
    return value


def validate_qdrant_metadata(snapshot: object) -> dict[str, object]:
    expected = {"schema_version", "canonicalization", "authority", "endpoint", "version", "point_payload_read", "point_search_or_scroll", "collections", "metadata_sha256"}
    value = _require_exact_fields(snapshot, expected, "Qdrant metadata")
    if value["schema_version"] != QDRANT_SCHEMA or value["canonicalization"] != CANONICALIZATION or value["authority"] != "derived_rebuildable" or value["endpoint"] != "loopback":
        raise LedgerError("Qdrant authority boundary is invalid")
    if value["point_payload_read"] is not False or value["point_search_or_scroll"] is not False:
        raise LedgerError("Qdrant point-data boundary was crossed")
    if not isinstance(value["version"], str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}", value["version"]):
        raise LedgerError("Qdrant version is malformed")
    collections = value["collections"]
    if not isinstance(collections, list) or any(not isinstance(item, dict) for item in collections):
        raise LedgerError("Qdrant collections are malformed")
    names: list[str] = []
    for item in collections:
        if set(item) != {"name", "status", "parameters", "hnsw_config", "optimizer_config", "wal_config", "payload_schema"}:
            raise LedgerError("Qdrant collection fields are invalid")
        name = item["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", name):
            raise LedgerError("Qdrant collection name is malformed")
        names.append(name)
        fields = item["payload_schema"]
        if not isinstance(fields, list) or any(not isinstance(field, dict) or set(field) != {"name", "data_type"} for field in fields):
            raise LedgerError("Qdrant payload-schema metadata is malformed")
        field_names = [field["name"] for field in fields]
        if field_names != sorted(set(field_names)):
            raise LedgerError("Qdrant payload-schema fields are duplicated or unsorted")
        for mapping in ("parameters", "hnsw_config", "optimizer_config", "wal_config"):
            if not isinstance(item[mapping], dict):
                raise LedgerError("Qdrant configuration is malformed")
    if names != sorted(set(names)):
        raise LedgerError("Qdrant collections are duplicated or unsorted")
    expected_hash = value["metadata_sha256"]
    require_sha256(expected_hash, "Qdrant metadata hash")
    without_hash = dict(value)
    del without_hash["metadata_sha256"]
    if sha256(canonical_bytes(without_hash)) != expected_hash:
        raise LedgerError("Qdrant metadata hash diverged")
    return value
