#!/usr/bin/env python3
"""Fail-closed validation for hash-bound creation of new governed functions.

This lane is deliberately separate from both the table-only migration contract
and the preexisting-function replacement contract.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import stat
from collections.abc import Iterable, Mapping

from governed_function_migration import (
    AUDIT_FIELDS,
    BASELINE_FIELDS,
    RECOVERY_FIELDS,
    REGISTRY_RECORD_FIELDS,
    RELATION_RE,
    RLS_FIELDS,
    SIGNATURE_RE,
    TRANSACTION_FIELDS,
    _require_role,
    _require_roles,
    _require_search_path,
    _safe_regular_child,
    _split_top_level_sql,
)
from governed_migration import (
    CANONICALIZATION,
    LANES,
    MigrationError,
    canonical_bytes,
    parse_canonical,
    require_exact,
    require_hash,
    require_id,
    require_relative_path,
    sha256,
)


PACKAGE_SCHEMA = "governed-function-creation-package-v1"
REGISTRY_SCHEMA = "governed-function-creation-registry-v1"
EVENT_SCHEMA = "governed-function-creation-execution-event-v1"
PACKAGE_FIELDS = {
    "schema_version",
    "canonicalization",
    "migration_id",
    "lane",
    "execution_owner",
    "dependencies",
    "baseline",
    "transaction",
    "functions",
    "recovery",
    "policy",
    "audit",
}
FUNCTION_FIELDS = {
    "id",
    "declaration_sql",
    "signature_sql",
    "operation",
    "owner",
    "language",
    "volatility",
    "parallel",
    "strict",
    "leakproof",
    "security_definer",
    "search_path",
    "forward_execute_roles",
    "rls_dependencies",
    "expected_definition_sha256",
    "forward_path",
    "forward_sha256",
    "rollback_path",
    "rollback_sha256",
}
POLICY_FIELDS = {
    "new_functions_only",
    "prior_absence_required",
    "exact_hashes_required",
    "security_definer_required",
    "fixed_search_path_required",
    "rls_dependencies_required",
    "dynamic_sql_allowed",
    "data_definition_in_function_body_allowed",
    "public_execute_allowed",
    "rollback_exact_drop_only",
}
REGISTRY_FIELDS = {"schema_version", "canonicalization", "packages"}


@dataclasses.dataclass(frozen=True)
class FunctionCreationSpec:
    identity: str
    declaration_sql: str
    signature_sql: str
    owner: str
    language: str
    volatility: str
    parallel: str
    strict: bool
    leakproof: bool
    search_path: tuple[str, ...]
    forward_execute_roles: tuple[str, ...]
    rls_dependencies: tuple[Mapping[str, object], ...]
    expected_definition_sha256: str
    forward_path: str
    forward_sha256: str
    rollback_path: str
    rollback_sha256: str


@dataclasses.dataclass(frozen=True)
class LoadedFunctionCreationPackage:
    directory: pathlib.Path
    manifest: Mapping[str, object]
    manifest_bytes: bytes
    functions: tuple[FunctionCreationSpec, ...]
    package_sha256: str

    @property
    def migration_id(self) -> str:
        return str(self.manifest["migration_id"])


def _load_artifact(
    root: pathlib.Path,
    value: Mapping[str, object],
    path_field: str,
    hash_field: str,
) -> tuple[str, bytes]:
    relative = require_relative_path(value[path_field], path_field, ".pgsql")
    expected = require_hash(value[hash_field], hash_field)
    payload = _safe_regular_child(root, relative).read_bytes()
    if sha256(payload) != expected:
        raise MigrationError(path_field + " bytes changed")
    return relative, payload


def _function_tail(spec: FunctionCreationSpec) -> tuple[str, ...]:
    output = [
        f"ALTER FUNCTION {spec.signature_sql} OWNER TO {spec.owner}",
        f"REVOKE ALL ON FUNCTION {spec.signature_sql} FROM PUBLIC",
    ]
    output.extend(
        f"GRANT EXECUTE ON FUNCTION {spec.signature_sql} TO {role}"
        for role in spec.forward_execute_roles
    )
    return tuple(output)


def validate_creation_sql(payload: bytes, spec: FunctionCreationSpec) -> None:
    statements = _split_top_level_sql(payload)
    expected_prefix = f"CREATE FUNCTION {spec.declaration_sql}\n"
    create = statements[0]
    if not create.startswith(expected_prefix) or create.startswith("CREATE OR REPLACE FUNCTION"):
        raise MigrationError("function creation signature differs from the manifest")
    search_path_sql = "''" if not spec.search_path else ", ".join(spec.search_path)
    required = (
        f"\nLANGUAGE {spec.language}\n",
        f"\n{spec.volatility.upper()}\n",
        "\nSECURITY DEFINER\n",
        f"\nSET search_path TO {search_path_sql}\n",
        "\nAS $function$\n",
    )
    if any(fragment not in create for fragment in required) or not create.endswith("\n$function$"):
        raise MigrationError("function creation security clauses are not canonical")
    body = create.split("\nAS $function$\n", 1)[1][:-len("\n$function$")]
    forbidden = (
        r"\bEXECUTE\b",
        r"\b(?:CREATE|ALTER|DROP)\s+(?:AGGREGATE|CAST|COLLATION|CONVERSION|DATABASE|DOMAIN|EVENT\s+TRIGGER|EXTENSION|FOREIGN|FUNCTION|INDEX|LANGUAGE|MATERIALIZED\s+VIEW|OPERATOR|POLICY|PROCEDURE|PUBLICATION|ROLE|RULE|SCHEMA|SEQUENCE|SERVER|STATISTICS|SUBSCRIPTION|TABLE|TABLESPACE|TEXT\s+SEARCH|TRANSFORM|TRIGGER|TYPE|USER|VIEW)\b",
        r"\bALTER\s+SYSTEM\b",
        r"\bSET\s+(?:ROLE|SESSION\s+AUTHORIZATION)\b",
        r"\b(?:GRANT|REVOKE|TRUNCATE)\b",
        r"\bCOPY\b",
        r"\bPROGRAM\b",
        r"\b(?:dblink|postgres_fdw|file_fdw|lo_import|lo_export)\b",
        r"\b(?:pg_read_file|pg_write_file|pg_ls_dir|pg_stat_file|pg_execute_server_program)\b",
    )
    if any(re.search(pattern, body, re.IGNORECASE) for pattern in forbidden):
        raise MigrationError("function creation body contains a forbidden capability")
    if statements[1:] != _function_tail(spec):
        raise MigrationError("function creation ownership or ACL statements differ")


def validate_creation_rollback(payload: bytes, spec: FunctionCreationSpec) -> None:
    statements = _split_top_level_sql(payload)
    if statements != (f"DROP FUNCTION {spec.signature_sql}",):
        raise MigrationError("function creation rollback is not the exact drop")


def _parse_function(
    root: pathlib.Path,
    raw: object,
    index: int,
) -> FunctionCreationSpec:
    item = require_exact(raw, FUNCTION_FIELDS, f"functions[{index}]")
    identity = require_id(item["id"], "function id")
    declaration = item["declaration_sql"]
    signature = item["signature_sql"]
    if not isinstance(declaration, str) or SIGNATURE_RE.fullmatch(declaration) is None:
        raise MigrationError("function creation declaration is not canonical")
    if not isinstance(signature, str) or SIGNATURE_RE.fullmatch(signature) is None:
        raise MigrationError("function creation signature is not canonical")
    if declaration.split("(", 1)[0] != signature.split("(", 1)[0]:
        raise MigrationError("function creation declaration and identity names differ")
    language = item["language"]
    volatility = item["volatility"]
    parallel = item["parallel"]
    strict = item["strict"]
    leakproof = item["leakproof"]
    if (
        item["operation"] != "create"
        or language not in {"sql", "plpgsql"}
        or volatility not in {"volatile", "stable", "immutable"}
        or parallel not in {"unsafe", "restricted", "safe"}
        or not isinstance(strict, bool)
        or leakproof is not False
        or item["security_definer"] is not True
    ):
        raise MigrationError("only exact new SECURITY DEFINER creation is accepted")
    owner = _require_role(item["owner"], "function creation owner")
    search_path = _require_search_path(item["search_path"])
    forward_roles = _require_roles(item["forward_execute_roles"], "forward execute roles")
    dependencies = item["rls_dependencies"]
    if not isinstance(dependencies, list) or not dependencies:
        raise MigrationError("function creation RLS dependencies are missing")
    parsed_dependencies: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for dependency_index, dependency_raw in enumerate(dependencies):
        dependency = require_exact(
            dependency_raw,
            RLS_FIELDS,
            f"rls_dependencies[{dependency_index}]",
        )
        relation = dependency["relation"]
        if not isinstance(relation, str) or RELATION_RE.fullmatch(relation) is None or relation in seen:
            raise MigrationError("function creation RLS dependency is malformed or duplicated")
        _require_role(dependency["owner"], "function creation RLS dependency owner")
        if dependency["forced"] is not True:
            raise MigrationError("function creation RLS dependency must require forced RLS")
        policies = dependency["policies"]
        if not isinstance(policies, list) or not policies or policies != sorted(set(policies)):
            raise MigrationError("function creation RLS policies are not canonical")
        for policy in policies:
            require_id(policy, "function creation RLS policy")
        seen.add(relation)
        parsed_dependencies.append(dict(dependency))
    if [item["relation"] for item in parsed_dependencies] != sorted(seen):
        raise MigrationError("function creation RLS dependencies are not sorted")
    expected_hash = require_hash(item["expected_definition_sha256"], "expected function definition hash")
    forward_path, forward_bytes = _load_artifact(root, item, "forward_path", "forward_sha256")
    rollback_path, rollback_bytes = _load_artifact(root, item, "rollback_path", "rollback_sha256")
    spec = FunctionCreationSpec(
        identity,
        declaration,
        signature,
        owner,
        language,
        volatility,
        parallel,
        strict,
        leakproof,
        search_path,
        forward_roles,
        tuple(parsed_dependencies),
        expected_hash,
        forward_path,
        str(item["forward_sha256"]),
        rollback_path,
        str(item["rollback_sha256"]),
    )
    validate_creation_sql(forward_bytes, spec)
    validate_creation_rollback(rollback_bytes, spec)
    return spec


def load_function_creation_package(
    directory: pathlib.Path,
    ledger_identity: Mapping[str, str],
) -> LoadedFunctionCreationPackage:
    root = directory.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("function creation package directory is unsafe")
    manifest_path = _safe_regular_child(root, "package.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = require_exact(parse_canonical(manifest_bytes), PACKAGE_FIELDS, "function creation package")
    if manifest["schema_version"] != PACKAGE_SCHEMA or manifest["canonicalization"] != CANONICALIZATION:
        raise MigrationError("function creation package schema is unsupported")
    migration_id = require_id(manifest["migration_id"], "migration_id")
    if root.name != migration_id or manifest["lane"] not in LANES or manifest["lane"] == "fractal_monism_policy":
        raise MigrationError("function creation package identity or lane is invalid")
    _require_role(manifest["execution_owner"], "function creation execution owner")
    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list) or dependencies != sorted(set(dependencies)) or migration_id in dependencies:
        raise MigrationError("function creation package dependencies are invalid")
    for dependency in dependencies:
        require_id(dependency, "function creation dependency")
    baseline = require_exact(manifest["baseline"], BASELINE_FIELDS, "baseline")
    require_id(baseline["ledger_id"], "baseline ledger ID")
    require_hash(baseline["ledger_sha256"], "baseline ledger hash")
    require_hash(baseline["catalog_evidence_sha256"], "baseline catalog evidence hash")
    for field in BASELINE_FIELDS:
        if baseline[field] != ledger_identity.get(field):
            raise MigrationError("function creation package baseline diverges from the schema ledger")
    transaction = require_exact(manifest["transaction"], TRANSACTION_FIELDS, "transaction")
    if transaction["mode"] != "required":
        raise MigrationError("function creation must be transactional")
    for field, minimum, maximum in (
        ("statement_timeout_ms", 100, 60000),
        ("lock_timeout_ms", 50, 30000),
    ):
        value = transaction[field]
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise MigrationError("function creation timeout is invalid")
    lock_key = transaction["advisory_lock_key"]
    if not isinstance(lock_key, int) or isinstance(lock_key, bool) or not 1 <= lock_key < 2**63:
        raise MigrationError("function creation advisory lock is invalid")
    raw_functions = manifest["functions"]
    if not isinstance(raw_functions, list) or not raw_functions:
        raise MigrationError("function creation inventory is empty")
    functions = tuple(_parse_function(root, raw, index) for index, raw in enumerate(raw_functions))
    identities = [spec.identity for spec in functions]
    signatures = [spec.signature_sql for spec in functions]
    if identities != sorted(set(identities)) or len(set(signatures)) != len(signatures):
        raise MigrationError("function creation inventory is not sorted and unique")
    recovery = require_exact(manifest["recovery"], RECOVERY_FIELDS, "recovery")
    if recovery["mode"] != "rollback_and_forward_recovery" or recovery["exact_restoration_required"] is not True:
        raise MigrationError("function creation recovery mode is invalid")
    recovery_path = require_relative_path(recovery["forward_recovery_path"], "forward recovery path", ".md")
    recovery_hash = require_hash(recovery["forward_recovery_sha256"], "forward recovery hash")
    if sha256(_safe_regular_child(root, recovery_path).read_bytes()) != recovery_hash:
        raise MigrationError("function creation recovery procedure changed")
    expected_files = {
        "package.json",
        recovery_path,
        *(spec.forward_path for spec in functions),
        *(spec.rollback_path for spec in functions),
    }
    if len(expected_files) != 2 + 2 * len(functions):
        raise MigrationError("function creation artifact paths collide")
    observed_files: set[str] = set()
    for child in root.iterdir():
        metadata = child.lstat()
        if child.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MigrationError("function creation package contains a non-regular artifact")
        observed_files.add(child.name)
    if observed_files != expected_files:
        raise MigrationError("function creation package file inventory differs")
    policy = require_exact(manifest["policy"], POLICY_FIELDS, "policy")
    expected_policy = {
        "new_functions_only": True,
        "prior_absence_required": True,
        "exact_hashes_required": True,
        "security_definer_required": True,
        "fixed_search_path_required": True,
        "rls_dependencies_required": True,
        "dynamic_sql_allowed": False,
        "data_definition_in_function_body_allowed": False,
        "public_execute_allowed": False,
        "rollback_exact_drop_only": True,
    }
    if policy != expected_policy:
        raise MigrationError("function creation policy is not fail closed")
    audit = require_exact(manifest["audit"], AUDIT_FIELDS, "audit")
    if audit != {"schema_version": EVENT_SCHEMA, "append_only": True}:
        raise MigrationError("function creation audit contract is invalid")
    package_hash = sha256(
        canonical_bytes(
            {
                "manifest_sha256": sha256(manifest_bytes),
                "function_forward_sha256": [spec.forward_sha256 for spec in functions],
                "function_rollback_sha256": [spec.rollback_sha256 for spec in functions],
                "forward_recovery_sha256": recovery_hash,
            }
        )
    )
    return LoadedFunctionCreationPackage(root, manifest, manifest_bytes, functions, package_hash)


def parse_function_creation_registry(payload: bytes) -> dict[str, dict[str, object]]:
    registry = require_exact(parse_canonical(payload), REGISTRY_FIELDS, "function creation registry")
    if registry["schema_version"] != REGISTRY_SCHEMA or registry["canonicalization"] != CANONICALIZATION:
        raise MigrationError("function creation registry schema is invalid")
    records = registry["packages"]
    if not isinstance(records, list):
        raise MigrationError("function creation registry records are invalid")
    output: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(records):
        record = require_exact(raw, REGISTRY_RECORD_FIELDS, f"function creation registry[{index}]")
        identity = require_id(record["migration_id"], "function creation registry migration ID")
        require_hash(record["package_sha256"], "function creation registry package hash")
        if identity in output or record["lane"] not in LANES or record["status"] not in {"active", "superseded"}:
            raise MigrationError("function creation registry record is invalid")
        output[identity] = dict(record)
    if list(output) != sorted(output):
        raise MigrationError("function creation registry is not sorted")
    return output


def validate_function_creation_registry_append_only(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
) -> None:
    if not set(previous).issubset(current):
        raise MigrationError("governed function creation registry is not append-only")
    for identity, record in previous.items():
        if current[identity] != record:
            raise MigrationError("governed function creation registry record changed")


def validate_function_creation_registry(
    packages: Iterable[LoadedFunctionCreationPackage],
    registry: Mapping[str, Mapping[str, object]],
) -> None:
    production = {package.migration_id: package for package in packages}
    if set(production) != set(registry):
        raise MigrationError("function creation package registry coverage differs")
    for identity, package in production.items():
        record = registry[identity]
        if record["package_sha256"] != package.package_sha256 or record["lane"] != package.manifest["lane"]:
            raise MigrationError("function creation package registry identity differs")
