#!/usr/bin/env python3
"""Fail-closed validation for hash-bound governed function replacements.

This is a separate contract.  It deliberately does not extend or relax the
table-only governed-migration-package-v1 SQL classifier.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import stat
from collections.abc import Iterable, Mapping

from governed_migration import (
    CANONICALIZATION,
    LANES,
    MigrationError,
    ROLE_RE,
    canonical_bytes,
    classify_sql,
    parse_canonical,
    require_exact,
    require_hash,
    require_id,
    require_relative_path,
    sha256,
)


PACKAGE_SCHEMA = "governed-function-migration-package-v1"
REGISTRY_SCHEMA = "governed-function-migration-registry-v1"
EVENT_SCHEMA = "governed-function-migration-execution-event-v1"
PACKAGE_FIELDS = {
    "schema_version",
    "canonicalization",
    "migration_id",
    "lane",
    "execution_owner",
    "dependencies",
    "baseline",
    "transaction",
    "relations",
    "functions",
    "recovery",
    "policy",
    "audit",
}
BASELINE_FIELDS = {"ledger_id", "ledger_sha256", "catalog_evidence_sha256"}
TRANSACTION_FIELDS = {"mode", "statement_timeout_ms", "lock_timeout_ms", "advisory_lock_key"}
RELATION_FIELDS = {"forward_path", "forward_sha256", "rollback_path", "rollback_sha256"}
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
    "rollback_execute_roles",
    "rls_dependencies",
    "prior_definition_sha256",
    "expected_definition_sha256",
    "forward_path",
    "forward_sha256",
    "rollback_path",
    "rollback_sha256",
}
RLS_FIELDS = {"relation", "owner", "forced", "policies"}
RECOVERY_FIELDS = {"mode", "exact_restoration_required", "forward_recovery_path", "forward_recovery_sha256"}
POLICY_FIELDS = {
    "preexisting_functions_only",
    "exact_hashes_required",
    "security_definer_required",
    "fixed_search_path_required",
    "rls_dependencies_required",
    "dynamic_sql_allowed",
    "data_definition_in_function_body_allowed",
    "grant_widening_allowed",
}
AUDIT_FIELDS = {"schema_version", "append_only"}
REGISTRY_FIELDS = {"schema_version", "canonicalization", "packages"}
REGISTRY_RECORD_FIELDS = {"migration_id", "package_sha256", "lane", "status"}
SIGNATURE_RE = re.compile(r"[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}\([^\n\r;]{0,2048}\)")
RELATION_RE = re.compile(r"[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}")


@dataclasses.dataclass(frozen=True)
class FunctionSpec:
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
    rollback_execute_roles: tuple[str, ...]
    rls_dependencies: tuple[Mapping[str, object], ...]
    prior_definition_sha256: str
    expected_definition_sha256: str
    forward_path: str
    forward_sha256: str
    rollback_path: str
    rollback_sha256: str


@dataclasses.dataclass(frozen=True)
class LoadedFunctionPackage:
    directory: pathlib.Path
    manifest: Mapping[str, object]
    manifest_bytes: bytes
    relation_forward_bytes: bytes
    relation_rollback_bytes: bytes
    functions: tuple[FunctionSpec, ...]
    package_sha256: str

    @property
    def migration_id(self) -> str:
        return str(self.manifest["migration_id"])


def _safe_regular_child(directory: pathlib.Path, relative: str) -> pathlib.Path:
    root = directory.resolve(strict=True)
    candidate = root / relative
    resolved = candidate.resolve(strict=True)
    if candidate.is_symlink() or resolved.parent != root:
        raise MigrationError("function package file escaped or is a symlink")
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise MigrationError("function package child is not a single-link regular file")
    return candidate


def _require_role(value: object, location: str) -> str:
    if not isinstance(value, str) or ROLE_RE.fullmatch(value) is None:
        raise MigrationError(location + " is not a canonical role")
    return value


def _require_roles(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or value != sorted(set(value)):
        raise MigrationError(location + " is not sorted and unique")
    roles = tuple(_require_role(role, location) for role in value)
    if "public" in roles:
        raise MigrationError(location + " may not grant PUBLIC")
    return roles


def _require_search_path(value: object) -> tuple[str, ...]:
    allowed = ([], ["pg_catalog"], ["pg_catalog", "memory"])
    if not isinstance(value, list) or value not in allowed:
        raise MigrationError("function search_path is not an accepted fixed safe path")
    return tuple(value)


def _split_top_level_sql(payload: bytes) -> tuple[str, ...]:
    if not payload or len(payload) > 4 * 1024 * 1024 or b"\0" in payload or b"\r" in payload:
        raise MigrationError("function SQL encoding or size is invalid")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise MigrationError("function SQL is not UTF-8") from error
    if not text.endswith("\n") or "\\" in text or "--" in text or "/*" in text:
        raise MigrationError("function SQL framing is invalid")
    statements: list[str] = []
    start = 0
    index = 0
    single = False
    dollar = False
    tag = "$function$"
    while index < len(text):
        if dollar:
            if text.startswith(tag, index):
                dollar = False
                index += len(tag)
                continue
            index += 1
            continue
        character = text[index]
        if single:
            if character == "'" and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            if character == "'":
                single = False
            index += 1
            continue
        if character == "'":
            single = True
            index += 1
            continue
        if text.startswith(tag, index):
            dollar = True
            index += len(tag)
            continue
        if character == "$":
            raise MigrationError("only the fixed $function$ delimiter is accepted")
        if character == ";":
            statement = text[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    if single or dollar or text[start:].strip():
        raise MigrationError("function SQL is unterminated")
    if not statements:
        raise MigrationError("function SQL has no statements")
    return tuple(statements)


def _function_tail(spec: FunctionSpec, execute_roles: tuple[str, ...]) -> tuple[str, ...]:
    signature = spec.signature_sql
    roles = ["public", *sorted(set(spec.forward_execute_roles) | set(spec.rollback_execute_roles))]
    output = [f"ALTER FUNCTION {signature} OWNER TO {spec.owner}"]
    output.extend(f"REVOKE ALL ON FUNCTION {signature} FROM {role.upper() if role == 'public' else role}" for role in roles)
    output.extend(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}" for role in execute_roles)
    return tuple(output)


def validate_function_sql(payload: bytes, spec: FunctionSpec, *, rollback: bool) -> None:
    statements = _split_top_level_sql(payload)
    expected_prefix = f"CREATE OR REPLACE FUNCTION {spec.declaration_sql}\n"
    create = statements[0]
    if not create.startswith(expected_prefix):
        raise MigrationError("function SQL signature differs from the manifest")
    search_path_sql = "''" if not spec.search_path else ", ".join(spec.search_path)
    required = (
        f"\nLANGUAGE {spec.language}\n",
        f"\n{spec.volatility.upper()}\n",
        "\nSECURITY DEFINER\n",
        f"\nSET search_path TO {search_path_sql}\n",
        "\nAS $function$\n",
    )
    if any(fragment not in create for fragment in required) or not create.endswith("\n$function$"):
        raise MigrationError("function SQL security clauses are not canonical")
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
        raise MigrationError("function body contains a forbidden capability")
    expected_tail = _function_tail(
        spec,
        spec.rollback_execute_roles if rollback else spec.forward_execute_roles,
    )
    if statements[1:] != expected_tail:
        raise MigrationError("function ownership or ACL statements differ from the manifest")


def _load_artifact(root: pathlib.Path, value: object, path_field: str, hash_field: str) -> tuple[str, bytes]:
    if not isinstance(value, Mapping):
        raise MigrationError("artifact declaration is invalid")
    relative = require_relative_path(value[path_field], path_field, ".pgsql")
    expected = require_hash(value[hash_field], hash_field)
    payload = _safe_regular_child(root, relative).read_bytes()
    if sha256(payload) != expected:
        raise MigrationError(path_field + " bytes changed")
    return relative, payload


def _parse_function(root: pathlib.Path, raw: object, index: int) -> FunctionSpec:
    item = require_exact(raw, FUNCTION_FIELDS, f"functions[{index}]")
    identity = require_id(item["id"], "function id")
    declaration = item["declaration_sql"]
    if not isinstance(declaration, str) or SIGNATURE_RE.fullmatch(declaration) is None:
        raise MigrationError("function declaration is not canonical")
    signature = item["signature_sql"]
    if not isinstance(signature, str) or SIGNATURE_RE.fullmatch(signature) is None:
        raise MigrationError("function signature is not canonical")
    if declaration.split("(", 1)[0] != signature.split("(", 1)[0]:
        raise MigrationError("function declaration and identity names differ")
    language = item["language"]
    volatility = item["volatility"]
    parallel = item["parallel"]
    strict = item["strict"]
    leakproof = item["leakproof"]
    if (
        item["operation"] != "replace"
        or language not in {"sql", "plpgsql"}
        or volatility not in {"volatile", "stable", "immutable"}
        or parallel not in {"unsafe", "restricted", "safe"}
        or not isinstance(strict, bool)
        or leakproof is not False
        or item["security_definer"] is not True
    ):
        raise MigrationError("only exact preexisting SECURITY DEFINER replacement is accepted")
    owner = _require_role(item["owner"], "function owner")
    search_path = _require_search_path(item["search_path"])
    forward_roles = _require_roles(item["forward_execute_roles"], "forward execute roles")
    rollback_roles = _require_roles(item["rollback_execute_roles"], "rollback execute roles")
    dependencies = item["rls_dependencies"]
    if not isinstance(dependencies, list) or not dependencies:
        raise MigrationError("function RLS dependencies are missing")
    parsed_dependencies: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for dependency_index, dependency_raw in enumerate(dependencies):
        dependency = require_exact(dependency_raw, RLS_FIELDS, f"rls_dependencies[{dependency_index}]")
        relation = dependency["relation"]
        if not isinstance(relation, str) or RELATION_RE.fullmatch(relation) is None or relation in seen:
            raise MigrationError("RLS dependency relation is malformed or duplicated")
        _require_role(dependency["owner"], "RLS dependency owner")
        if dependency["forced"] is not True:
            raise MigrationError("RLS dependency must require forced RLS")
        policies = dependency["policies"]
        if not isinstance(policies, list) or not policies or policies != sorted(set(policies)):
            raise MigrationError("RLS dependency policies are not canonical")
        for policy in policies:
            require_id(policy, "RLS policy")
        seen.add(relation)
        parsed_dependencies.append(dict(dependency))
    if [item["relation"] for item in parsed_dependencies] != sorted(seen):
        raise MigrationError("RLS dependencies are not sorted")
    prior_hash = require_hash(item["prior_definition_sha256"], "prior function definition hash")
    expected_hash = require_hash(item["expected_definition_sha256"], "expected function definition hash")
    forward_path, forward_bytes = _load_artifact(root, item, "forward_path", "forward_sha256")
    rollback_path, rollback_bytes = _load_artifact(root, item, "rollback_path", "rollback_sha256")
    spec = FunctionSpec(
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
        rollback_roles,
        tuple(parsed_dependencies),
        prior_hash,
        expected_hash,
        forward_path,
        str(item["forward_sha256"]),
        rollback_path,
        str(item["rollback_sha256"]),
    )
    validate_function_sql(forward_bytes, spec, rollback=False)
    validate_function_sql(rollback_bytes, spec, rollback=True)
    return spec


def load_function_package(directory: pathlib.Path, ledger_identity: Mapping[str, str]) -> LoadedFunctionPackage:
    root = directory.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("function package directory is unsafe")
    manifest_path = _safe_regular_child(root, "package.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = require_exact(parse_canonical(manifest_bytes), PACKAGE_FIELDS, "function package")
    if manifest["schema_version"] != PACKAGE_SCHEMA or manifest["canonicalization"] != CANONICALIZATION:
        raise MigrationError("function package schema is unsupported")
    migration_id = require_id(manifest["migration_id"], "migration_id")
    if root.name != migration_id or manifest["lane"] not in LANES or manifest["lane"] == "fractal_monism_policy":
        raise MigrationError("function package identity or lane is invalid")
    _require_role(manifest["execution_owner"], "execution owner")
    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list) or dependencies != sorted(set(dependencies)) or migration_id in dependencies:
        raise MigrationError("function package dependencies are invalid")
    for dependency in dependencies:
        require_id(dependency, "dependency")
    baseline = require_exact(manifest["baseline"], BASELINE_FIELDS, "baseline")
    require_id(baseline["ledger_id"], "baseline ledger ID")
    require_hash(baseline["ledger_sha256"], "baseline ledger hash")
    require_hash(baseline["catalog_evidence_sha256"], "baseline catalog evidence hash")
    for field in BASELINE_FIELDS:
        expected = ledger_identity.get(field)
        if baseline[field] != expected:
            raise MigrationError("function package baseline diverges from the schema ledger")
    transaction = require_exact(manifest["transaction"], TRANSACTION_FIELDS, "transaction")
    if transaction["mode"] != "required":
        raise MigrationError("function migration must be transactional")
    for field, minimum, maximum in (("statement_timeout_ms", 100, 60000), ("lock_timeout_ms", 50, 30000)):
        value = transaction[field]
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise MigrationError("function migration timeout is invalid")
    lock_key = transaction["advisory_lock_key"]
    if not isinstance(lock_key, int) or isinstance(lock_key, bool) or not 1 <= lock_key < 2**63:
        raise MigrationError("function migration advisory lock is invalid")
    relations = require_exact(manifest["relations"], RELATION_FIELDS, "relations")
    _, relation_forward = _load_artifact(root, relations, "forward_path", "forward_sha256")
    _, relation_rollback = _load_artifact(root, relations, "rollback_path", "rollback_sha256")
    classify_sql(relation_forward, "forward")
    classify_sql(relation_rollback, "rollback")
    raw_functions = manifest["functions"]
    if not isinstance(raw_functions, list) or not raw_functions:
        raise MigrationError("function replacement inventory is empty")
    functions = tuple(_parse_function(root, raw, index) for index, raw in enumerate(raw_functions))
    identities = [spec.identity for spec in functions]
    signatures = [spec.signature_sql for spec in functions]
    if identities != sorted(set(identities)) or len(set(signatures)) != len(signatures):
        raise MigrationError("function replacement inventory is not sorted and unique")
    recovery = require_exact(manifest["recovery"], RECOVERY_FIELDS, "recovery")
    if recovery["mode"] != "rollback_and_forward_recovery" or recovery["exact_restoration_required"] is not True:
        raise MigrationError("function migration recovery mode is invalid")
    recovery_path = require_relative_path(recovery["forward_recovery_path"], "forward recovery path", ".md")
    recovery_hash = require_hash(recovery["forward_recovery_sha256"], "forward recovery hash")
    if sha256(_safe_regular_child(root, recovery_path).read_bytes()) != recovery_hash:
        raise MigrationError("forward recovery procedure changed")
    expected_files = {
        "package.json",
        str(relations["forward_path"]),
        str(relations["rollback_path"]),
        recovery_path,
        *(spec.forward_path for spec in functions),
        *(spec.rollback_path for spec in functions),
    }
    if len(expected_files) != 4 + 2 * len(functions):
        raise MigrationError("function package artifact paths collide")
    observed_files: set[str] = set()
    for child in root.iterdir():
        metadata = child.lstat()
        if child.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MigrationError("function package contains a non-regular artifact")
        observed_files.add(child.name)
    if observed_files != expected_files:
        raise MigrationError("function package file inventory differs")
    policy = require_exact(manifest["policy"], POLICY_FIELDS, "policy")
    expected_policy = {
        "preexisting_functions_only": True,
        "exact_hashes_required": True,
        "security_definer_required": True,
        "fixed_search_path_required": True,
        "rls_dependencies_required": True,
        "dynamic_sql_allowed": False,
        "data_definition_in_function_body_allowed": False,
        "grant_widening_allowed": False,
    }
    if policy != expected_policy:
        raise MigrationError("function migration policy is not fail closed")
    audit = require_exact(manifest["audit"], AUDIT_FIELDS, "audit")
    if audit != {"schema_version": EVENT_SCHEMA, "append_only": True}:
        raise MigrationError("function migration audit contract is invalid")
    package_hash = sha256(
        canonical_bytes(
            {
                "manifest_sha256": sha256(manifest_bytes),
                "relation_forward_sha256": sha256(relation_forward),
                "relation_rollback_sha256": sha256(relation_rollback),
                "function_forward_sha256": [spec.forward_sha256 for spec in functions],
                "function_rollback_sha256": [spec.rollback_sha256 for spec in functions],
                "forward_recovery_sha256": recovery_hash,
            }
        )
    )
    return LoadedFunctionPackage(root, manifest, manifest_bytes, relation_forward, relation_rollback, functions, package_hash)


def parse_function_registry(payload: bytes) -> dict[str, dict[str, object]]:
    registry = require_exact(parse_canonical(payload), REGISTRY_FIELDS, "function registry")
    if registry["schema_version"] != REGISTRY_SCHEMA or registry["canonicalization"] != CANONICALIZATION:
        raise MigrationError("function registry schema is invalid")
    records = registry["packages"]
    if not isinstance(records, list):
        raise MigrationError("function registry records are invalid")
    output: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(records):
        record = require_exact(raw, REGISTRY_RECORD_FIELDS, f"function registry[{index}]")
        identity = require_id(record["migration_id"], "function registry migration ID")
        require_hash(record["package_sha256"], "function registry package hash")
        if identity in output or record["lane"] not in LANES or record["status"] not in {"active", "superseded"}:
            raise MigrationError("function registry record is invalid")
        output[identity] = dict(record)
    if list(output) != sorted(output):
        raise MigrationError("function registry is not sorted")
    return output


def validate_function_registry_append_only(previous: Mapping[str, Mapping[str, object]], current: Mapping[str, Mapping[str, object]]) -> None:
    if not set(previous).issubset(current):
        raise MigrationError("governed function registry is not append-only")
    for identity, record in previous.items():
        if current[identity] != record:
            raise MigrationError("governed function registry record changed")


def validate_function_registry(packages: Iterable[LoadedFunctionPackage], registry: Mapping[str, Mapping[str, object]]) -> None:
    production = {package.migration_id: package for package in packages}
    if set(production) != set(registry):
        raise MigrationError("function package registry coverage differs")
    for identity, package in production.items():
        record = registry[identity]
        if record["package_sha256"] != package.package_sha256 or record["lane"] != package.manifest["lane"]:
            raise MigrationError("function package registry identity differs")
