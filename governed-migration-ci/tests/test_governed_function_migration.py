from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from governed_function_migration import (  # noqa: E402
    FunctionSpec,
    load_function_package,
    parse_function_registry,
    validate_function_registry,
    validate_function_registry_append_only,
    validate_function_sql,
)
from governed_migration import MigrationError, canonical_bytes, sha256  # noqa: E402


LEDGER = {
    "ledger_id": "governed_memory_schema_ledger_v1",
    "ledger_sha256": "a" * 64,
    "catalog_evidence_sha256": "b" * 64,
}
FORWARD_FUNCTION = b"""CREATE OR REPLACE FUNCTION memory.fixture_api_v1(p_owner uuid)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog, memory
AS $function$
BEGIN
    RETURN p_owner;
END
$function$;
ALTER FUNCTION memory.fixture_api_v1(uuid) OWNER TO sage;
REVOKE ALL ON FUNCTION memory.fixture_api_v1(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.fixture_api_v1(uuid) FROM memory_reader;
GRANT EXECUTE ON FUNCTION memory.fixture_api_v1(uuid) TO memory_reader;
"""
ROLLBACK_FUNCTION = FORWARD_FUNCTION.replace(b"RETURN p_owner;", b"RETURN NULL;")


def write_package(root: pathlib.Path) -> pathlib.Path:
    package = root / "fixture_function_migration_v1"
    package.mkdir()
    relation_forward = b"""CREATE TABLE memory.fixture_function_relation (owner_id uuid NOT NULL);
ALTER TABLE memory.fixture_function_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.fixture_function_relation FORCE ROW LEVEL SECURITY;
"""
    relation_rollback = b"DROP TABLE memory.fixture_function_relation;\n"
    recovery = b"# Forward recovery\n\nRestore the exact prior function definition, then reapply.\n"
    (package / "relations-forward.pgsql").write_bytes(relation_forward)
    (package / "relations-rollback.pgsql").write_bytes(relation_rollback)
    (package / "fixture-forward.pgsql").write_bytes(FORWARD_FUNCTION)
    (package / "fixture-rollback.pgsql").write_bytes(ROLLBACK_FUNCTION)
    (package / "FORWARD_RECOVERY.md").write_bytes(recovery)
    manifest = {
        "schema_version": "governed-function-migration-package-v1",
        "canonicalization": "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1",
        "migration_id": package.name,
        "lane": "personal_memory",
        "execution_owner": "sage",
        "dependencies": [],
        "baseline": LEDGER,
        "transaction": {
            "mode": "required",
            "statement_timeout_ms": 15000,
            "lock_timeout_ms": 1000,
            "advisory_lock_key": 731942001,
        },
        "relations": {
            "forward_path": "relations-forward.pgsql",
            "forward_sha256": sha256(relation_forward),
            "rollback_path": "relations-rollback.pgsql",
            "rollback_sha256": sha256(relation_rollback),
        },
        "functions": [
            {
                "id": "memory.fixture_api_v1",
                "declaration_sql": "memory.fixture_api_v1(p_owner uuid)",
                "signature_sql": "memory.fixture_api_v1(uuid)",
                "operation": "replace",
                "owner": "sage",
                "language": "plpgsql",
                "volatility": "volatile",
                "parallel": "unsafe",
                "strict": False,
                "leakproof": False,
                "security_definer": True,
                "search_path": ["pg_catalog", "memory"],
                "forward_execute_roles": ["memory_reader"],
                "rollback_execute_roles": ["memory_reader"],
                "rls_dependencies": [
                    {
                        "relation": "memory.fixture_function_relation",
                        "owner": "sage",
                        "forced": True,
                        "policies": ["fixture_owner_isolation"],
                    }
                ],
                "prior_definition_sha256": "c" * 64,
                "expected_definition_sha256": "d" * 64,
                "forward_path": "fixture-forward.pgsql",
                "forward_sha256": sha256(FORWARD_FUNCTION),
                "rollback_path": "fixture-rollback.pgsql",
                "rollback_sha256": sha256(ROLLBACK_FUNCTION),
            }
        ],
        "recovery": {
            "mode": "rollback_and_forward_recovery",
            "exact_restoration_required": True,
            "forward_recovery_path": "FORWARD_RECOVERY.md",
            "forward_recovery_sha256": sha256(recovery),
        },
        "policy": {
            "preexisting_functions_only": True,
            "exact_hashes_required": True,
            "security_definer_required": True,
            "fixed_search_path_required": True,
            "rls_dependencies_required": True,
            "dynamic_sql_allowed": False,
            "data_definition_in_function_body_allowed": False,
            "grant_widening_allowed": False,
        },
        "audit": {
            "schema_version": "governed-function-migration-execution-event-v1",
            "append_only": True,
        },
    }
    (package / "package.json").write_bytes(canonical_bytes(manifest))
    return package


class FunctionSqlTest(unittest.TestCase):
    def spec(self) -> FunctionSpec:
        return FunctionSpec(
            "memory.fixture_api_v1",
            "memory.fixture_api_v1(p_owner uuid)",
            "memory.fixture_api_v1(uuid)",
            "sage",
            "plpgsql",
            "volatile",
            "unsafe",
            False,
            False,
            ("pg_catalog", "memory"),
            ("memory_reader",),
            ("memory_reader",),
            ({"relation": "memory.fixture", "owner": "sage", "forced": True, "policies": ["owner_policy"]},),
            "c" * 64,
            "d" * 64,
            "fixture-forward.pgsql",
            sha256(FORWARD_FUNCTION),
            "fixture-rollback.pgsql",
            sha256(ROLLBACK_FUNCTION),
        )

    def test_accepts_exact_canonical_replacement(self) -> None:
        validate_function_sql(FORWARD_FUNCTION, self.spec(), rollback=False)

    def test_rejects_dynamic_sql(self) -> None:
        payload = FORWARD_FUNCTION.replace(b"RETURN p_owner;", b"EXECUTE 'SELECT 1';\n    RETURN p_owner;")
        with self.assertRaisesRegex(MigrationError, "forbidden capability"):
            validate_function_sql(payload, self.spec(), rollback=False)

    def test_rejects_function_body_ddl(self) -> None:
        payload = FORWARD_FUNCTION.replace(b"RETURN p_owner;", b"DROP TABLE memory.fixture;\n    RETURN p_owner;")
        with self.assertRaisesRegex(MigrationError, "forbidden capability"):
            validate_function_sql(payload, self.spec(), rollback=False)

    def test_rejects_security_invoker(self) -> None:
        payload = FORWARD_FUNCTION.replace(b"SECURITY DEFINER", b"SECURITY INVOKER")
        with self.assertRaisesRegex(MigrationError, "security clauses"):
            validate_function_sql(payload, self.spec(), rollback=False)

    def test_rejects_search_path_drift(self) -> None:
        payload = FORWARD_FUNCTION.replace(b"pg_catalog, memory", b"memory, public")
        with self.assertRaisesRegex(MigrationError, "security clauses"):
            validate_function_sql(payload, self.spec(), rollback=False)

    def test_rejects_acl_widening(self) -> None:
        payload = FORWARD_FUNCTION.replace(b"TO memory_reader;", b"TO PUBLIC;", 1)
        with self.assertRaisesRegex(MigrationError, "ownership or ACL"):
            validate_function_sql(payload, self.spec(), rollback=False)

    def test_rejects_second_function(self) -> None:
        payload = FORWARD_FUNCTION + FORWARD_FUNCTION
        with self.assertRaisesRegex(MigrationError, "ownership or ACL"):
            validate_function_sql(payload, self.spec(), rollback=False)


class PackageTest(unittest.TestCase):
    def test_loads_exact_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = load_function_package(write_package(pathlib.Path(temporary)), LEDGER)
            self.assertEqual(package.migration_id, "fixture_function_migration_v1")
            self.assertEqual(len(package.functions), 1)
            self.assertEqual(len(package.package_sha256), 64)

    def test_rejects_changed_function_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            (directory / "fixture-forward.pgsql").write_bytes(FORWARD_FUNCTION + b"\n")
            with self.assertRaisesRegex(MigrationError, "bytes changed"):
                load_function_package(directory, LEDGER)

    def test_rejects_new_function_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            manifest = json.loads((directory / "package.json").read_text())
            manifest["functions"][0]["operation"] = "create"
            (directory / "package.json").write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(MigrationError, "preexisting"):
                load_function_package(directory, LEDGER)

    def test_rejects_missing_rls_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            manifest = json.loads((directory / "package.json").read_text())
            manifest["functions"][0]["rls_dependencies"] = []
            (directory / "package.json").write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(MigrationError, "RLS dependencies"):
                load_function_package(directory, LEDGER)

    def test_rejects_public_execute_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            manifest = json.loads((directory / "package.json").read_text())
            manifest["functions"][0]["forward_execute_roles"] = ["public"]
            (directory / "package.json").write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(MigrationError, "may not grant PUBLIC"):
                load_function_package(directory, LEDGER)

    def test_rejects_unlisted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            (directory / "unlisted.txt").write_text("not authority\n")
            with self.assertRaisesRegex(MigrationError, "file inventory differs"):
                load_function_package(directory, LEDGER)


class RegistryTest(unittest.TestCase):
    def test_registry_is_exact_and_append_only(self) -> None:
        record = {
            "migration_id": "fixture_function_migration_v1",
            "package_sha256": "a" * 64,
            "lane": "personal_memory",
            "status": "active",
        }
        payload = canonical_bytes(
            {
                "schema_version": "governed-function-migration-registry-v1",
                "canonicalization": "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1",
                "packages": [record],
            }
        )
        current = parse_function_registry(payload)
        validate_function_registry_append_only({}, current)
        with self.assertRaises(MigrationError):
            validate_function_registry_append_only(current, {})

    def test_registry_binds_package_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = load_function_package(write_package(pathlib.Path(temporary)), LEDGER)
            registry = {
                package.migration_id: {
                    "migration_id": package.migration_id,
                    "package_sha256": package.package_sha256,
                    "lane": "personal_memory",
                    "status": "active",
                }
            }
            validate_function_registry([package], registry)
            registry[package.migration_id] = {**registry[package.migration_id], "package_sha256": "f" * 64}
            with self.assertRaises(MigrationError):
                validate_function_registry([package], registry)


if __name__ == "__main__":
    unittest.main()
