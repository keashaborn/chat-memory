#!/usr/bin/env python3
"""Governed migration package validation and repository reconciliation.

This module never connects to PostgreSQL.  It validates canonical package
metadata, immutable source bytes, frozen legacy SQL, dependency topology, and
the deny-by-default SQL policy used by the disposable full-chain harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
from dataclasses import dataclass
from typing import Iterable, Mapping


CANONICALIZATION = "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1"
PACKAGE_SCHEMA = "governed-migration-package-v1"
REGISTRY_SCHEMA = "governed-migration-registry-v1"
EXTERNAL_DEPLOYED_SQL_REGISTRY_SCHEMA = "governed-external-deployed-sql-registry-v1"
LEGACY_SQL_PATH_ALIAS_REGISTRY_SCHEMA = "governed-legacy-sql-path-alias-registry-v1"
EXECUTION_EVENT_SCHEMA = "governed-migration-execution-event-v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_OID_RE = re.compile(r"[0-9a-f]{40}")
ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
LANES = {
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
}

PACKAGE_FIELDS = {
    "schema_version",
    "canonicalization",
    "migration_id",
    "ci_only",
    "lane",
    "execution_owner",
    "dependencies",
    "baseline",
    "transaction",
    "forward",
    "verification",
    "recovery",
    "policy",
    "audit",
}
BASELINE_FIELDS = {
    "ledger_id",
    "ledger_sha256",
    "catalog_evidence_sha256",
    "minimum_schema_baseline",
    "expected_schema_baseline",
}
TRANSACTION_FIELDS = {
    "mode",
    "statement_timeout_ms",
    "lock_timeout_ms",
    "advisory_lock_key",
}
ACTION_FIELDS = {"path", "sha256"}
VERIFICATION_FIELDS = {"expected_state_sha256", "object_expectations"}
POLICY_FIELDS = {
    "destructive_forward_allowed",
    "data_operations_allowed",
    "nontransactional_allowed",
    "rls_weakening_allowed",
    "grant_widening_allowed",
}
AUDIT_FIELDS = {"schema_version", "append_only"}
REGISTRY_FIELDS = {"schema_version", "canonicalization", "packages"}
REGISTRY_RECORD_FIELDS = {"migration_id", "package_sha256", "lane", "status"}
EXTERNAL_REGISTRY_FIELDS = {
    "schema_version",
    "canonicalization",
    "closed",
    "records",
}
EXTERNAL_RECORD_FIELDS = {
    "record_id",
    "path",
    "git_blob",
    "sha256",
    "source_projection_sha256",
    "repository_commit",
    "source_role",
    "deployment_evidence",
    "execution_authorized",
    "future_source_policy",
    "status",
}
EXTERNAL_DEPLOYMENT_FIELDS = {
    "event_sequence",
    "event_sha256",
    "outcome",
}
LEGACY_ALIAS_REGISTRY_FIELDS = {
    "schema_version",
    "canonicalization",
    "closed",
    "records",
}
LEGACY_ALIAS_RECORD_FIELDS = {
    "record_id",
    "ledger_path",
    "repository_path",
    "git_blob",
    "sha256",
    "ledger_source_projection_sha256",
    "repository_commit",
    "execution_authorized",
    "status",
}

# This is a closed, one-time reconciliation for two SQL sources that were
# independently reviewed, deployed, and committed before the governed Memory
# candidate reached its repository-inventory gate.  Duplicating the authorized
# binding in code and canonical registry data prevents that registry from
# becoming a general exception lane for future unmanaged .sql files.
EXPECTED_EXTERNAL_DEPLOYED_SQL_V1 = (
    {
        "record_id": "chat_attachments_v1_forward_20260806",
        "path": "ops/sql/20260806_chat_attachments_v1.sql",
        "git_blob": "5f0d4c701bbb5b86856cbf3f262a7c7ffca3827b",
        "sha256": "f862fbb467d534979a8af029f3da73e45b5a421c1714ac83cde132dcc93e4ea8",
        "source_projection_sha256": "51e37c474b53c43e6244e956b4be2aecae28f89241ed758cbd3533716202155d",
        "repository_commit": "1f06e32942d01152d3f3fb5ffc5c7781d04b9c57",
        "source_role": "forward",
        "deployment_evidence": {
            "event_sequence": 655,
            "event_sha256": "77efc95b35f7b78094bc7228be574a03540a8957899bbe851f4d510d346f1685",
            "outcome": "verified_current_forward_apply",
        },
        "execution_authorized": False,
        "future_source_policy": "governed_pgsql_required",
        "status": "adopted_read_only",
    },
    {
        "record_id": "chat_attachments_v1_rollback_20260806",
        "path": "ops/sql/20260806_chat_attachments_v1.rollback.sql",
        "git_blob": "2bc550e870cb3dd036e256c9cd79a631c8ccf6a9",
        "sha256": "f5a6f9340b38c24d0424b758bff284afb393c061bd2c7d774f95b9f1c1fdb040",
        "source_projection_sha256": "fad5baeb16fb0c658e22cb25ca9b5ae7f3ad8ed4f37ff7974ef373890e0b5a02",
        "repository_commit": "1f06e32942d01152d3f3fb5ffc5c7781d04b9c57",
        "source_role": "rollback",
        "deployment_evidence": {
            "event_sequence": 647,
            "event_sha256": "64f84a616ee4b4de52471e2fd6113598e2145661ff901015c74fc14f22ba9515",
            "outcome": "verified_rollback_then_superseded_by_forward",
        },
        "execution_authorized": False,
        "future_source_policy": "governed_pgsql_required",
        "status": "adopted_read_only",
    },
)

EXPECTED_LEGACY_SQL_PATH_ALIASES_V1 = (
    {
        "record_id": "semantic_compiler_v11_subject_forward_path_alias",
        "ledger_path": "ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_subject_3a5a251294939911_62d4610fd348.sql",
        "repository_path": "ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_jerry.sql",
        "git_blob": "078467ee85e38b9dc015d95e746fdfd02e3c1271",
        "sha256": "338dc1bf8c70945eed4e1a5dd4cb015cf0d304dbc279b59a85a36d7bd2ca16e5",
        "ledger_source_projection_sha256": "cf1cde7ab97b41f954a59c005c0a02628985ab4a4e6b9ce005e71015bb0c2903",
        "repository_commit": "1f06e32942d01152d3f3fb5ffc5c7781d04b9c57",
        "execution_authorized": False,
        "status": "identity_only",
    },
    {
        "record_id": "semantic_compiler_v11_subject_rollback_path_alias",
        "ledger_path": "ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_subject_3a5a251294939911_c2e0db9b89ac_rollback.sql",
        "repository_path": "ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_jerry_rollback.sql",
        "git_blob": "9a2485e8827db80ab15e7edb85336e860a56cbc1",
        "sha256": "47044d45b7db38349d93cb0663ef219290ae07495f606ddd7002c984aa2a7dea",
        "ledger_source_projection_sha256": "874e876154aabc22f55f887be1b18d0fb7ec848d10d23944cbd45198b7b66856",
        "repository_commit": "1f06e32942d01152d3f3fb5ffc5c7781d04b9c57",
        "execution_authorized": False,
        "status": "identity_only",
    },
)


class MigrationError(RuntimeError):
    pass


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_json(value: object, location: str = "root") -> None:
    if isinstance(value, float):
        raise MigrationError(location + " contains a float")
    if isinstance(value, str) and CONTROL_RE.search(value):
        raise MigrationError(location + " contains a control character")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or CONTROL_RE.search(key):
                raise MigrationError(location + " contains an invalid key")
            _validate_json(child, location + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json(child, f"{location}[{index}]")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise MigrationError(location + " contains an unsupported value")


def canonical_bytes(value: object) -> bytes:
    _validate_json(value)
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError("duplicate JSON field: " + key)
        result[key] = value
    return result


def parse_canonical(payload: bytes, maximum: int = 8 * 1024 * 1024) -> object:
    if not payload or len(payload) > maximum or b"\0" in payload or b"\r" in payload:
        raise MigrationError("canonical payload encoding or size is invalid")
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError("canonical payload cannot be parsed") from error
    if payload != canonical_bytes(value):
        raise MigrationError("payload is not exact canonical JSON")
    return value


def require_exact(value: object, fields: set[str], location: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MigrationError(location + " fields are invalid")
    return value


def require_id(value: object, location: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise MigrationError(location + " is malformed")
    return value


def require_hash(value: object, location: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise MigrationError(location + " is not a lowercase SHA-256")
    return value


def require_relative_path(value: object, location: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or CONTROL_RE.search(value):
        raise MigrationError(location + " is malformed")
    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or ".." in pure.parts or "." in pure.parts:
        raise MigrationError(location + " is not canonical")
    if suffix is not None and not value.endswith(suffix):
        raise MigrationError(location + " has the wrong suffix")
    return value


FORBIDDEN_SQL = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\\",
        r"\bCOPY\b",
        r"\bPROGRAM\b",
        r"\bCREATE\s+EXTENSION\b",
        r"\bALTER\s+SYSTEM\b",
        r"\b(?:CREATE|DROP|ALTER)\s+(?:DATABASE|ROLE|USER|TABLESPACE)\b",
        r"\bSET\s+(?:ROLE|SESSION\s+AUTHORIZATION)\b",
        r"\bSECURITY\s+DEFINER\b",
        r"\b(?:CREATE|IMPORT)\s+FOREIGN\b",
        r"\bCREATE\s+(?:SERVER|SUBSCRIPTION|PUBLICATION)\b",
        r"\b(?:dblink|postgres_fdw|file_fdw|lo_import|lo_export)\b",
        r"\b(?:pg_read_file|pg_write_file|pg_ls_dir|pg_stat_file|pg_execute_server_program)\b",
        r"\b(?:LOAD|VACUUM|ANALYZE|LISTEN|NOTIFY|DO|CALL)\b",
        r"\b(?:UPDATE|DELETE|MERGE|TRUNCATE)\b",
        r"\bINSERT\s+INTO\b",
        r"\bDISABLE\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bNO\s+FORCE\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bGRANT\b[^;]*\bALL\b",
        r"\bGRANT\b[^;]*\bTO\s+PUBLIC\b",
        r"\bWITH\s+GRANT\s+OPTION\b",
        r"\b(?:BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK|SAVEPOINT|PREPARE\s+TRANSACTION)\b",
        r"\$[A-Za-z0-9_]*\$",
        r"--[^\n]*[\\]",
        r"/\*",
    )
)
NARROW_INSERT_PRIVILEGE_RE = re.compile(
    r"^(?:GRANT (?:SELECT\s*,\s*INSERT|INSERT\s*,\s*SELECT|INSERT) "
    r"ON (?:TABLE )?[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?"
    r"(?:\s*,\s*[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?)* "
    r"TO [A-Z_][A-Z0-9_]*(?:\s*,\s*[A-Z_][A-Z0-9_]*)*"
    r"|REVOKE (?:SELECT\s*,\s*INSERT|INSERT\s*,\s*SELECT|INSERT) "
    r"ON (?:TABLE )?[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?"
    r"(?:\s*,\s*[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?)* "
    r"FROM [A-Z_][A-Z0-9_]*(?:\s*,\s*[A-Z_][A-Z0-9_]*)*)$"
)
FORWARD_PREFIXES = (
    "CREATE SCHEMA ",
    "CREATE TABLE ",
    "CREATE INDEX ",
    "CREATE UNIQUE INDEX ",
    "CREATE VIEW ",
    "CREATE TYPE ",
    "CREATE POLICY ",
    "ALTER TABLE ",
    "ALTER SCHEMA ",
    "GRANT ",
)
ROLLBACK_PREFIXES = FORWARD_PREFIXES + (
    "DROP POLICY ",
    "DROP TABLE ",
    "DROP INDEX ",
    "DROP VIEW ",
    "DROP TYPE ",
    "DROP SCHEMA ",
    "REVOKE ",
)


def classify_sql(payload: bytes, action: str) -> tuple[str, ...]:
    if action not in {"forward", "rollback"}:
        raise MigrationError("SQL action is invalid")
    if not payload or len(payload) > 4 * 1024 * 1024 or b"\0" in payload or b"\r" in payload:
        raise MigrationError(action + " SQL encoding or size is invalid")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise MigrationError(action + " SQL is not UTF-8") from error
    if not text.endswith("\n"):
        raise MigrationError(action + " SQL lacks final newline")
    for pattern in FORBIDDEN_SQL:
        if pattern.search(text):
            raise MigrationError(action + " SQL contains a forbidden operation")
    statements = tuple(part.strip() for part in text.split(";") if part.strip())
    if not statements:
        raise MigrationError(action + " SQL has no statements")
    allowed = FORWARD_PREFIXES if action == "forward" else ROLLBACK_PREFIXES
    for statement in statements:
        normalized = re.sub(r"\s+", " ", statement).strip().upper()
        if re.search(r"\bINSERT\b", normalized) and not NARROW_INSERT_PRIVILEGE_RE.fullmatch(
            normalized
        ):
            raise MigrationError(
                action + " SQL uses INSERT outside a narrow table privilege statement"
            )
        if not normalized.startswith(allowed):
            raise MigrationError(action + " SQL statement is not allowlisted")
        if action == "forward" and normalized.startswith(("DROP ", "REVOKE ")):
            raise MigrationError("destructive forward SQL is forbidden")
    return statements


@dataclass(frozen=True)
class LoadedPackage:
    directory: pathlib.Path
    manifest: Mapping[str, object]
    manifest_bytes: bytes
    forward_bytes: bytes
    rollback_bytes: bytes | None
    package_sha256: str

    @property
    def migration_id(self) -> str:
        return str(self.manifest["migration_id"])


def _safe_regular_child(directory: pathlib.Path, relative: str) -> pathlib.Path:
    root = directory.resolve(strict=True)
    candidate = root / relative
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root or candidate.is_symlink():
        raise MigrationError("package file escaped or is a symlink")
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("package child is not a regular file")
    return candidate


def _validate_object_expectations(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise MigrationError("object expectations are missing")
    result: list[dict[str, object]] = []
    identities: set[str] = set()
    for index, raw in enumerate(value):
        item = require_exact(raw, {"id", "kind", "owner", "fingerprint_sha256"}, f"object_expectations[{index}]")
        identity = require_id(item["id"], "object expectation id")
        if identity in identities or item["kind"] not in {"schema", "relation", "column", "policy", "grant"}:
            raise MigrationError("object expectations collide or have invalid kind")
        if not isinstance(item["owner"], str) or not ROLE_RE.fullmatch(item["owner"]):
            raise MigrationError("object expectation owner is invalid")
        require_hash(item["fingerprint_sha256"], "object expectation fingerprint")
        identities.add(identity)
        result.append(dict(item))
    if [item["id"] for item in result] != sorted(identities):
        raise MigrationError("object expectations are not sorted")
    return result


def load_package(directory: pathlib.Path, ledger_identity: Mapping[str, str]) -> LoadedPackage:
    root = directory.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("package directory is unsafe")
    manifest_path = _safe_regular_child(root, "package.json")
    manifest_bytes = manifest_path.read_bytes()
    value = parse_canonical(manifest_bytes)
    manifest = require_exact(value, PACKAGE_FIELDS, "package")
    if manifest["schema_version"] != PACKAGE_SCHEMA or manifest["canonicalization"] != CANONICALIZATION:
        raise MigrationError("package schema is unsupported")
    migration_id = require_id(manifest["migration_id"], "migration_id")
    if root.name != migration_id:
        raise MigrationError("package directory and migration ID differ")
    if not isinstance(manifest["ci_only"], bool):
        raise MigrationError("ci_only is invalid")
    if manifest["lane"] not in LANES or manifest["lane"] == "fractal_monism_policy":
        raise MigrationError("migration lane is invalid or policy-only")
    if not isinstance(manifest["execution_owner"], str) or not ROLE_RE.fullmatch(manifest["execution_owner"]):
        raise MigrationError("execution owner is invalid")
    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list) or dependencies != sorted(set(dependencies)):
        raise MigrationError("dependencies are not canonical")
    for dependency in dependencies:
        require_id(dependency, "dependency")
        if dependency == migration_id:
            raise MigrationError("migration depends on itself")
    baseline = require_exact(manifest["baseline"], BASELINE_FIELDS, "baseline")
    for field in ("ledger_id", "minimum_schema_baseline", "expected_schema_baseline"):
        require_id(baseline[field], "baseline " + field)
    for field in ("ledger_sha256", "catalog_evidence_sha256"):
        require_hash(baseline[field], "baseline " + field)
    for field in ("ledger_id", "ledger_sha256", "catalog_evidence_sha256"):
        if baseline[field] != ledger_identity[field]:
            raise MigrationError("package baseline diverges from schema ledger")
    transaction = require_exact(manifest["transaction"], TRANSACTION_FIELDS, "transaction")
    if transaction["mode"] != "required":
        raise MigrationError("only required transactions are accepted in v1")
    for field, minimum, maximum in (("statement_timeout_ms", 100, 60000), ("lock_timeout_ms", 50, 30000)):
        number = transaction[field]
        if not isinstance(number, int) or isinstance(number, bool) or not minimum <= number <= maximum:
            raise MigrationError("transaction timeout is invalid")
    lock_key = transaction["advisory_lock_key"]
    if not isinstance(lock_key, int) or isinstance(lock_key, bool) or not 1 <= lock_key < 2**63:
        raise MigrationError("advisory lock key is invalid")
    forward = require_exact(manifest["forward"], ACTION_FIELDS, "forward")
    forward_path = require_relative_path(forward["path"], "forward path", ".pgsql")
    if pathlib.PurePosixPath(forward_path).name != "forward.pgsql":
        raise MigrationError("forward action filename is not fixed")
    require_hash(forward["sha256"], "forward hash")
    forward_file = _safe_regular_child(root, forward_path)
    forward_bytes = forward_file.read_bytes()
    if sha256(forward_bytes) != forward["sha256"]:
        raise MigrationError("forward bytes changed")
    classify_sql(forward_bytes, "forward")
    verification = require_exact(manifest["verification"], VERIFICATION_FIELDS, "verification")
    require_hash(verification["expected_state_sha256"], "expected state hash")
    _validate_object_expectations(verification["object_expectations"])
    recovery = manifest["recovery"]
    if not isinstance(recovery, dict) or recovery.get("mode") not in {"rollback", "forward_recovery"}:
        raise MigrationError("recovery declaration is missing")
    rollback_bytes: bytes | None = None
    if recovery["mode"] == "rollback":
        recovery = require_exact(recovery, {"mode", "path", "sha256", "exact_restoration_required"}, "recovery")
        if recovery["exact_restoration_required"] is not True:
            raise MigrationError("rollback must require exact restoration")
        rollback_path = require_relative_path(recovery["path"], "rollback path", ".pgsql")
        if pathlib.PurePosixPath(rollback_path).name != "rollback.pgsql":
            raise MigrationError("rollback action filename is not fixed")
        require_hash(recovery["sha256"], "rollback hash")
        rollback_file = _safe_regular_child(root, rollback_path)
        rollback_bytes = rollback_file.read_bytes()
        if sha256(rollback_bytes) != recovery["sha256"]:
            raise MigrationError("rollback bytes changed")
        classify_sql(rollback_bytes, "rollback")
    else:
        recovery = require_exact(recovery, {"mode", "procedure", "procedure_sha256", "operator_review_required"}, "recovery")
        require_relative_path(recovery["procedure"], "recovery procedure", ".md")
        require_hash(recovery["procedure_sha256"], "recovery procedure hash")
        if recovery["operator_review_required"] is not True:
            raise MigrationError("forward recovery requires operator review")
        procedure = _safe_regular_child(root, recovery["procedure"])
        if sha256(procedure.read_bytes()) != recovery["procedure_sha256"]:
            raise MigrationError("forward recovery procedure changed")
    policy = require_exact(manifest["policy"], POLICY_FIELDS, "policy")
    if any(policy.values()):
        raise MigrationError("v1 package policy must deny every exceptional capability")
    audit = require_exact(manifest["audit"], AUDIT_FIELDS, "audit")
    if audit != {"schema_version": EXECUTION_EVENT_SCHEMA, "append_only": True}:
        raise MigrationError("append-only audit declaration is invalid")
    package_hash = sha256(
        canonical_bytes(
            {
                "manifest_sha256": sha256(manifest_bytes),
                "forward_sha256": sha256(forward_bytes),
                "recovery_sha256": sha256(rollback_bytes) if rollback_bytes is not None else recovery["procedure_sha256"],
            }
        )
    )
    return LoadedPackage(root, manifest, manifest_bytes, forward_bytes, rollback_bytes, package_hash)


def validate_dependency_graph(packages: Iterable[LoadedPackage]) -> None:
    items = list(packages)
    by_id: dict[str, LoadedPackage] = {}
    for package in items:
        if package.migration_id in by_id:
            raise MigrationError("duplicate migration ID")
        by_id[package.migration_id] = package
    graph = {identifier: tuple(package.manifest["dependencies"]) for identifier, package in by_id.items()}
    for identifier, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in by_id:
                raise MigrationError(identifier + " has an unknown dependency")
            if bool(by_id[identifier].manifest["ci_only"]) != bool(by_id[dependency].manifest["ci_only"]):
                raise MigrationError("CI-only and production dependency lanes cannot cross")
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in temporary:
            raise MigrationError("migration dependency cycle")
        if identifier in permanent:
            return
        temporary.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        temporary.remove(identifier)
        permanent.add(identifier)

    for identifier in sorted(graph):
        visit(identifier)


def parse_registry(payload: bytes) -> dict[str, dict[str, object]]:
    value = parse_canonical(payload)
    registry = require_exact(value, REGISTRY_FIELDS, "registry")
    if registry["schema_version"] != REGISTRY_SCHEMA or registry["canonicalization"] != CANONICALIZATION:
        raise MigrationError("registry schema is invalid")
    records = registry["packages"]
    if not isinstance(records, list):
        raise MigrationError("registry packages are invalid")
    result: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(records):
        record = require_exact(raw, REGISTRY_RECORD_FIELDS, f"registry[{index}]")
        identifier = require_id(record["migration_id"], "registry migration ID")
        require_hash(record["package_sha256"], "registry package hash")
        if identifier in result or record["lane"] not in LANES or record["status"] not in {"active", "superseded"}:
            raise MigrationError("registry record is invalid or duplicated")
        result[identifier] = dict(record)
    if list(result) != sorted(result):
        raise MigrationError("registry records are not sorted")
    return result


def load_registry(path: pathlib.Path) -> dict[str, dict[str, object]]:
    return parse_registry(path.read_bytes())


def parse_external_deployed_sql_registry(
    payload: bytes,
) -> dict[str, dict[str, object]]:
    value = parse_canonical(payload, maximum=1024 * 1024)
    registry = require_exact(value, EXTERNAL_REGISTRY_FIELDS, "external registry")
    if (
        registry["schema_version"] != EXTERNAL_DEPLOYED_SQL_REGISTRY_SCHEMA
        or registry["canonicalization"] != CANONICALIZATION
        or registry["closed"] is not True
    ):
        raise MigrationError("external deployed SQL registry contract is invalid")
    records = registry["records"]
    if not isinstance(records, list) or len(records) != len(EXPECTED_EXTERNAL_DEPLOYED_SQL_V1):
        raise MigrationError("external deployed SQL registry is not closed")
    output: dict[str, dict[str, object]] = {}
    record_ids: list[str] = []
    for index, raw in enumerate(records):
        record = require_exact(raw, EXTERNAL_RECORD_FIELDS, f"external registry[{index}]")
        record_id = require_id(record["record_id"], "external record ID")
        path = require_relative_path(record["path"], "external SQL path", ".sql")
        if not isinstance(record["git_blob"], str) or not GIT_OID_RE.fullmatch(record["git_blob"]):
            raise MigrationError("external SQL Git blob is malformed")
        require_hash(record["sha256"], "external SQL hash")
        require_hash(record["source_projection_sha256"], "external source projection hash")
        if not isinstance(record["repository_commit"], str) or not GIT_OID_RE.fullmatch(record["repository_commit"]):
            raise MigrationError("external SQL repository commit is malformed")
        if record["source_role"] not in {"forward", "rollback"}:
            raise MigrationError("external SQL source role is invalid")
        evidence = require_exact(
            record["deployment_evidence"],
            EXTERNAL_DEPLOYMENT_FIELDS,
            "external deployment evidence",
        )
        if not isinstance(evidence["event_sequence"], int) or isinstance(evidence["event_sequence"], bool) or evidence["event_sequence"] <= 0:
            raise MigrationError("external deployment event sequence is invalid")
        require_hash(evidence["event_sha256"], "external deployment event hash")
        if evidence["outcome"] not in {
            "verified_current_forward_apply",
            "verified_rollback_then_superseded_by_forward",
        }:
            raise MigrationError("external deployment outcome is invalid")
        if (
            record["execution_authorized"] is not False
            or record["future_source_policy"] != "governed_pgsql_required"
            or record["status"] != "adopted_read_only"
        ):
            raise MigrationError("external SQL record grants authority or changes policy")
        if record_id in record_ids or path in output:
            raise MigrationError("external deployed SQL record collides")
        record_ids.append(record_id)
        output[path] = dict(record)
    if record_ids != sorted(record_ids):
        raise MigrationError("external deployed SQL records are not sorted")
    expected = {str(item["path"]): dict(item) for item in EXPECTED_EXTERNAL_DEPLOYED_SQL_V1}
    if output != expected:
        raise MigrationError("external deployed SQL registry differs from the authorized closed binding")
    return output


def load_external_deployed_sql_registry(
    path: pathlib.Path,
) -> dict[str, dict[str, object]]:
    return parse_external_deployed_sql_registry(path.read_bytes())


def validate_external_deployed_sql_registry_append_only(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
) -> None:
    if previous and {
        key: dict(value) for key, value in previous.items()
    } != {key: dict(value) for key, value in current.items()}:
        raise MigrationError("closed external deployed SQL registry changed")


def parse_legacy_sql_path_alias_registry(
    payload: bytes,
) -> dict[str, dict[str, object]]:
    value = parse_canonical(payload, maximum=1024 * 1024)
    registry = require_exact(value, LEGACY_ALIAS_REGISTRY_FIELDS, "legacy alias registry")
    if (
        registry["schema_version"] != LEGACY_SQL_PATH_ALIAS_REGISTRY_SCHEMA
        or registry["canonicalization"] != CANONICALIZATION
        or registry["closed"] is not True
    ):
        raise MigrationError("legacy SQL path alias registry contract is invalid")
    records = registry["records"]
    if not isinstance(records, list) or len(records) != len(EXPECTED_LEGACY_SQL_PATH_ALIASES_V1):
        raise MigrationError("legacy SQL path alias registry is not closed")
    output: dict[str, dict[str, object]] = {}
    repository_paths: set[str] = set()
    record_ids: list[str] = []
    for index, raw in enumerate(records):
        record = require_exact(raw, LEGACY_ALIAS_RECORD_FIELDS, f"legacy alias registry[{index}]")
        record_id = require_id(record["record_id"], "legacy path alias record ID")
        ledger_path = require_relative_path(record["ledger_path"], "legacy ledger path", ".sql")
        repository_path = require_relative_path(record["repository_path"], "legacy repository path", ".sql")
        if ledger_path == repository_path:
            raise MigrationError("legacy SQL path alias does not change path")
        if not isinstance(record["git_blob"], str) or not GIT_OID_RE.fullmatch(record["git_blob"]):
            raise MigrationError("legacy path alias Git blob is malformed")
        require_hash(record["sha256"], "legacy path alias SQL hash")
        require_hash(record["ledger_source_projection_sha256"], "legacy path alias projection hash")
        if not isinstance(record["repository_commit"], str) or not GIT_OID_RE.fullmatch(record["repository_commit"]):
            raise MigrationError("legacy path alias repository commit is malformed")
        if record["execution_authorized"] is not False or record["status"] != "identity_only":
            raise MigrationError("legacy SQL path alias grants authority")
        if record_id in record_ids or ledger_path in output or repository_path in repository_paths:
            raise MigrationError("legacy SQL path alias collides")
        record_ids.append(record_id)
        repository_paths.add(repository_path)
        output[ledger_path] = dict(record)
    if record_ids != sorted(record_ids):
        raise MigrationError("legacy SQL path alias records are not sorted")
    expected = {str(item["ledger_path"]): dict(item) for item in EXPECTED_LEGACY_SQL_PATH_ALIASES_V1}
    if output != expected:
        raise MigrationError("legacy SQL path alias registry differs from the authorized closed binding")
    return output


def load_legacy_sql_path_alias_registry(
    path: pathlib.Path,
) -> dict[str, dict[str, object]]:
    return parse_legacy_sql_path_alias_registry(path.read_bytes())


def validate_legacy_sql_path_alias_registry_append_only(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
) -> None:
    if previous and {
        key: dict(value) for key, value in previous.items()
    } != {key: dict(value) for key, value in current.items()}:
        raise MigrationError("closed legacy SQL path alias registry changed")


def validate_registry_append_only(previous: Mapping[str, Mapping[str, object]], current: Mapping[str, Mapping[str, object]]) -> None:
    if not set(previous).issubset(current):
        raise MigrationError("governed migration registry is not append-only")
    for identifier, record in previous.items():
        if dict(current[identifier]) != dict(record):
            raise MigrationError("governed migration registry record changed")


def validate_registry(packages: Iterable[LoadedPackage], registry: Mapping[str, Mapping[str, object]]) -> None:
    production = {package.migration_id: package for package in packages if not package.manifest["ci_only"]}
    if set(production) != set(registry):
        raise MigrationError("production package registry coverage differs")
    for identifier, package in production.items():
        record = registry[identifier]
        if record["package_sha256"] != package.package_sha256 or record["lane"] != package.manifest["lane"]:
            raise MigrationError("registered migration ID or bytes were reused")


def _git(repository: pathlib.Path, arguments: list[str], input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "--no-pager", "-C", repository.as_posix(), *arguments],
        input=input_bytes,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=120,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if result.returncode != 0 or len(result.stdout) > 128 * 1024 * 1024 or len(result.stderr) > 1024 * 1024:
        raise MigrationError("read-only Git command failed")
    return result.stdout


def git_sql_inventory(repository: pathlib.Path, commit: str) -> dict[str, tuple[str, str]]:
    root = repository.resolve(strict=True)
    raw = _git(root, ["ls-tree", "-r", "-z", commit])
    candidates: list[tuple[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_bytes = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[1] != b"blob":
            continue
        path = os.fsdecode(path_bytes)
        pure = pathlib.PurePosixPath(path)
        if pure.is_absolute() or pure.as_posix() != path or ".." in pure.parts:
            raise MigrationError("Git SQL path is unsafe")
        if path.endswith((".sql", ".pgsql")):
            candidates.append((path, fields[2].decode("ascii", "strict")))
    if not candidates:
        raise MigrationError("repository has no SQL inventory")
    input_bytes = b"".join((oid + "\n").encode("ascii") for _, oid in candidates)
    batch = _git(root, ["cat-file", "--batch"], input_bytes=input_bytes)
    position = 0
    output: dict[str, tuple[str, str]] = {}
    for path, oid in candidates:
        newline = batch.find(b"\n", position)
        if newline < 0:
            raise MigrationError("Git batch header is incomplete")
        header = batch[position:newline].split()
        if len(header) != 3 or header[0].decode("ascii") != oid or header[1] != b"blob":
            raise MigrationError("Git batch object differs")
        size = int(header[2])
        start = newline + 1
        end = start + size
        payload = batch[start:end]
        if len(payload) != size or batch[end:end + 1] != b"\n":
            raise MigrationError("Git batch payload is incomplete")
        position = end + 1
        if path in output:
            raise MigrationError("Git SQL path collision")
        output[path] = (oid, sha256(payload))
    if position != len(batch):
        raise MigrationError("Git batch has trailing data")
    return output


def validate_external_deployed_sql_commit_bindings(
    repository: pathlib.Path,
    commit: str,
    records: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    root = repository.resolve(strict=True)
    resolved = _git(root, ["rev-parse", "--verify", commit + "^{commit}"]).rstrip(b"\n")
    if len(resolved) != 40 or not GIT_OID_RE.fullmatch(resolved.decode("ascii", "strict")):
        raise MigrationError("repository commit resolution failed")
    current_commit = resolved.decode("ascii", "strict")
    adopted_commits: set[str] = set()
    for path, record in sorted(records.items()):
        adopted = str(record["repository_commit"])
        _git(root, ["merge-base", "--is-ancestor", adopted, current_commit])
        tree = _git(root, ["ls-tree", "-z", adopted, "--", path])
        entries = [entry for entry in tree.split(b"\0") if entry]
        if len(entries) != 1:
            raise MigrationError("external SQL adopted commit path is absent or ambiguous")
        metadata, separator, encoded_path = entries[0].partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or encoded_path.decode("utf-8", "strict") != path
            or len(fields) != 3
            or fields[0] != b"100644"
            or fields[1] != b"blob"
            or fields[2].decode("ascii", "strict") != record["git_blob"]
        ):
            raise MigrationError("external SQL adopted commit binding differs")
        adopted_commits.add(adopted)
    return {
        "adopted_repository_commits": sorted(adopted_commits),
        "current_repository_commit": current_commit,
        "status": "bound",
    }


def validate_legacy_sql_path_alias_commit_bindings(
    repository: pathlib.Path,
    commit: str,
    records: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    root = repository.resolve(strict=True)
    resolved = _git(root, ["rev-parse", "--verify", commit + "^{commit}"]).rstrip(b"\n")
    if len(resolved) != 40 or not GIT_OID_RE.fullmatch(resolved.decode("ascii", "strict")):
        raise MigrationError("repository commit resolution failed")
    current_commit = resolved.decode("ascii", "strict")
    adopted_commits: set[str] = set()
    for ledger_path, record in sorted(records.items()):
        adopted = str(record["repository_commit"])
        repository_path = str(record["repository_path"])
        _git(root, ["merge-base", "--is-ancestor", adopted, current_commit])
        tree = _git(root, ["ls-tree", "-z", adopted, "--", repository_path])
        entries = [entry for entry in tree.split(b"\0") if entry]
        if len(entries) != 1:
            raise MigrationError("legacy path alias adopted path is absent or ambiguous")
        metadata, separator, encoded_path = entries[0].partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or encoded_path.decode("utf-8", "strict") != repository_path
            or len(fields) != 3
            or fields[0] != b"100644"
            or fields[1] != b"blob"
            or fields[2].decode("ascii", "strict") != record["git_blob"]
        ):
            raise MigrationError("legacy SQL path alias adopted commit binding differs")
        old_tree = _git(root, ["ls-tree", "-z", current_commit, "--", ledger_path])
        if old_tree:
            raise MigrationError("legacy SQL path alias ledger path still exists")
        adopted_commits.add(adopted)
    return {
        "adopted_repository_commits": sorted(adopted_commits),
        "current_repository_commit": current_commit,
        "status": "identity_only_bound",
    }


def validate_repository_inventory(
    repository: pathlib.Path,
    commit: str,
    baseline_sources: Iterable[Mapping[str, object]],
    packages: Iterable[LoadedPackage],
    *,
    current_legacy_sources: Iterable[Mapping[str, object]] | None = None,
    external_deployed_sources: Mapping[str, Mapping[str, object]] | None = None,
    legacy_path_aliases: Mapping[str, Mapping[str, object]] | None = None,
    function_governed_sources: Mapping[str, str] | None = None,
) -> dict[str, object]:
    packages = list(packages)
    baseline_sources = list(baseline_sources)
    inventory = git_sql_inventory(repository, commit)
    baseline = {
        str(item["path"]): (str(item["git_blob"]), str(item["sha256"]))
        for item in baseline_sources
        if str(item.get("path", "")).endswith(".sql")
    }
    baseline_by_path = {str(item["path"]): item for item in baseline_sources}
    legacy_path_aliases = dict(legacy_path_aliases or {})
    observed_baseline = dict(baseline)
    alias_repository_paths: set[str] = set()
    for ledger_path, record in legacy_path_aliases.items():
        repository_path = str(record["repository_path"])
        expected_identity = (str(record["git_blob"]), str(record["sha256"]))
        if (
            ledger_path not in baseline
            or baseline[ledger_path] != expected_identity
            or sha256(canonical_bytes(baseline_by_path[ledger_path]))
            != record["ledger_source_projection_sha256"]
            or repository_path in baseline
            or repository_path in alias_repository_paths
        ):
            raise MigrationError("legacy SQL path alias differs from the frozen baseline")
        del observed_baseline[ledger_path]
        observed_baseline[repository_path] = expected_identity
        alias_repository_paths.add(repository_path)
    external_deployed_sources = dict(external_deployed_sources or {})
    external = {
        path: (str(record["git_blob"]), str(record["sha256"]))
        for path, record in external_deployed_sources.items()
    }
    if (
        set(baseline).intersection(external)
        or alias_repository_paths.intersection(external)
        or set(legacy_path_aliases).intersection(external)
    ):
        raise MigrationError("external deployed SQL overlaps the frozen baseline")
    projected_expected_legacy = {**baseline, **external}
    observed_expected_legacy = {**observed_baseline, **external}
    observed_legacy = {path: value for path, value in inventory.items() if path.endswith(".sql")}
    if current_legacy_sources is None:
        if observed_legacy != observed_expected_legacy:
            raise MigrationError("legacy SQL baseline changed or an ungoverned .sql source was added")
    else:
        projected = list(current_legacy_sources)
        projected_paths = [
            str(item.get("path"))
            for item in projected
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        ]
        projected_by_path = {
            str(item.get("path")): item
            for item in projected
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        if (
            projected_paths != sorted(projected_expected_legacy)
            or len(projected_paths) != len(projected)
            or len(projected_by_path) != len(projected)
            or set(projected_by_path) != set(projected_expected_legacy)
            or any(projected_by_path[path] != item for path, item in baseline_by_path.items())
            or any(
                sha256(canonical_bytes(projected_by_path[path]))
                != record["source_projection_sha256"]
                for path, record in external_deployed_sources.items()
            )
            or observed_legacy != observed_expected_legacy
        ):
            raise MigrationError("legacy SQL baseline changed or an ungoverned .sql source was added")
    schema_governed: dict[str, str] = {}
    for package in packages:
        package_root = package.directory
        for action in ("forward", "recovery"):
            record = package.manifest[action]
            if action == "recovery" and record["mode"] != "rollback":
                continue
            relative = str(record["path"])
            full = package_root / relative
            try:
                repository_relative = full.resolve(strict=True).relative_to(repository.resolve(strict=True)).as_posix()
            except ValueError as error:
                raise MigrationError("package is outside the repository") from error
            expected_hash = str(record["sha256"])
            if repository_relative in schema_governed:
                raise MigrationError("governed SQL source is referenced twice")
            schema_governed[repository_relative] = expected_hash
    function_governed: dict[str, str] = {}
    for raw_path, raw_hash in dict(function_governed_sources or {}).items():
        relative = require_relative_path(raw_path, "governed function SQL path", ".pgsql")
        expected_hash = require_hash(raw_hash, "governed function SQL hash")
        if relative in schema_governed or relative in function_governed:
            raise MigrationError("governed SQL source is referenced twice")
        function_governed[relative] = expected_hash
    governed = {**schema_governed, **function_governed}
    observed_governed = {path: value[1] for path, value in inventory.items() if path.endswith(".pgsql")}
    if observed_governed != governed:
        raise MigrationError("governed SQL coverage or bytes differ")
    return {
        "legacy_sql_count": len(observed_legacy),
        "legacy_baseline_sql_count": len(baseline),
        "legacy_status": "frozen_667_unverifiable_not_executable",
        "legacy_path_alias_count": len(legacy_path_aliases),
        "legacy_path_alias_execution_authorized": False,
        "legacy_path_alias_status": "closed_identity_only",
        "external_deployed_sql_count": len(external),
        "external_deployed_sql_execution_authorized": False,
        "external_deployed_sql_status": "closed_hash_bound_read_only",
        "governed_sql_count": len(observed_governed),
        "schema_governed_sql_count": len(schema_governed),
        "function_governed_sql_count": len(function_governed),
        "governed_package_count": len(packages),
        "status": "reconciled",
    }
