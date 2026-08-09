from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from governed_function_creation import (  # noqa: E402
    FunctionCreationSpec,
    load_function_creation_package,
    parse_function_creation_registry,
    validate_creation_rollback,
    validate_creation_sql,
    validate_function_creation_registry,
    validate_function_creation_registry_append_only,
)
from governed_migration import MigrationError, canonical_bytes, sha256  # noqa: E402


LEDGER = {
    "ledger_id": "fixture_ledger_v1",
    "ledger_sha256": "1" * 64,
    "catalog_evidence_sha256": "2" * 64,
}
FORWARD = b"""CREATE FUNCTION memory.fixture_created_v1(p_owner uuid)
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
ALTER FUNCTION memory.fixture_created_v1(uuid) OWNER TO sage;
REVOKE ALL ON FUNCTION memory.fixture_created_v1(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.fixture_created_v1(uuid) TO memory_reader;
"""
ROLLBACK = b"DROP FUNCTION memory.fixture_created_v1(uuid);\n"


def function_spec() -> FunctionCreationSpec:
    return FunctionCreationSpec(
        "memory.fixture_created_v1",
        "memory.fixture_created_v1(p_owner uuid)",
        "memory.fixture_created_v1(uuid)",
        "sage",
        "plpgsql",
        "volatile",
        "unsafe",
        False,
        False,
        ("pg_catalog", "memory"),
        ("memory_reader",),
        (
            {
                "relation": "memory.fixture",
                "owner": "sage",
                "forced": True,
                "policies": ["owner_policy"],
            },
        ),
        "3" * 64,
        "fixture-forward.pgsql",
        sha256(FORWARD),
        "fixture-rollback.pgsql",
        sha256(ROLLBACK),
    )


def write_package(root: pathlib.Path) -> pathlib.Path:
    package = root / "fixture_function_creation_v1"
    package.mkdir()
    recovery = b"# Forward recovery\n\nVerify prior absence, then reapply the exact package.\n"
    (package / "fixture-forward.pgsql").write_bytes(FORWARD)
    (package / "fixture-rollback.pgsql").write_bytes(ROLLBACK)
    (package / "FORWARD_RECOVERY.md").write_bytes(recovery)
    manifest = {
        "schema_version": "governed-function-creation-package-v1",
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
            "advisory_lock_key": 724613047777,
        },
        "functions": [
            {
                "id": "memory.fixture_created_v1",
                "declaration_sql": "memory.fixture_created_v1(p_owner uuid)",
                "signature_sql": "memory.fixture_created_v1(uuid)",
                "operation": "create",
                "owner": "sage",
                "language": "plpgsql",
                "volatility": "volatile",
                "parallel": "unsafe",
                "strict": False,
                "leakproof": False,
                "security_definer": True,
                "search_path": ["pg_catalog", "memory"],
                "forward_execute_roles": ["memory_reader"],
                "rls_dependencies": [
                    {
                        "relation": "memory.fixture",
                        "owner": "sage",
                        "forced": True,
                        "policies": ["owner_policy"],
                    }
                ],
                "expected_definition_sha256": "3" * 64,
                "forward_path": "fixture-forward.pgsql",
                "forward_sha256": sha256(FORWARD),
                "rollback_path": "fixture-rollback.pgsql",
                "rollback_sha256": sha256(ROLLBACK),
            }
        ],
        "recovery": {
            "mode": "rollback_and_forward_recovery",
            "exact_restoration_required": True,
            "forward_recovery_path": "FORWARD_RECOVERY.md",
            "forward_recovery_sha256": sha256(recovery),
        },
        "policy": {
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
        },
        "audit": {
            "schema_version": "governed-function-creation-execution-event-v1",
            "append_only": True,
        },
    }
    (package / "package.json").write_bytes(canonical_bytes(manifest))
    return package


class FunctionCreationSqlTest(unittest.TestCase):
    def test_accepts_exact_create_and_drop(self) -> None:
        validate_creation_sql(FORWARD, function_spec())
        validate_creation_rollback(ROLLBACK, function_spec())

    def test_rejects_create_or_replace(self) -> None:
        with self.assertRaisesRegex(MigrationError, "signature"):
            validate_creation_sql(
                FORWARD.replace(b"CREATE FUNCTION", b"CREATE OR REPLACE FUNCTION", 1),
                function_spec(),
            )

    def test_rejects_body_ddl(self) -> None:
        with self.assertRaisesRegex(MigrationError, "forbidden capability"):
            validate_creation_sql(
                FORWARD.replace(b"RETURN p_owner;", b"DROP TABLE memory.fixture;\n  RETURN p_owner;"),
                function_spec(),
            )

    def test_rejects_public_execute(self) -> None:
        with self.assertRaisesRegex(MigrationError, "ACL"):
            validate_creation_sql(
                FORWARD.replace(b"TO memory_reader", b"TO PUBLIC"),
                function_spec(),
            )

    def test_rejects_nonexact_rollback(self) -> None:
        with self.assertRaisesRegex(MigrationError, "exact drop"):
            validate_creation_rollback(
                b"DROP FUNCTION IF EXISTS memory.fixture_created_v1(uuid);\n",
                function_spec(),
            )


class FunctionCreationPackageTest(unittest.TestCase):
    def test_loads_exact_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = load_function_creation_package(write_package(pathlib.Path(temporary)), LEDGER)
            self.assertEqual(package.migration_id, "fixture_function_creation_v1")
            self.assertEqual(len(package.functions), 1)

    def test_rejects_replace_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            manifest = json.loads((directory / "package.json").read_text())
            manifest["functions"][0]["operation"] = "replace"
            (directory / "package.json").write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(MigrationError, "new SECURITY DEFINER"):
                load_function_creation_package(directory, LEDGER)

    def test_rejects_unlisted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_package(pathlib.Path(temporary))
            (directory / "unlisted.txt").write_text("not authority\n")
            with self.assertRaisesRegex(MigrationError, "inventory differs"):
                load_function_creation_package(directory, LEDGER)

    def test_retirement_package_checks_every_processing_effect(self) -> None:
        repository = pathlib.Path(__file__).resolve().parents[2]
        directory = (
            repository
            / "governed-function-creations"
            / "memory_pending_job_retirement_v1"
        )
        if not directory.exists():
            self.skipTest("repository retirement package is not present")
        identity = {
            "ledger_id": "governed-memory-v1-initial-production-baseline",
            "ledger_sha256": (
                "c2195f4d5ea132f6eb82781ca34f7ae1"
                "2516d81c6eaca3b9488d1f51037da995"
            ),
            "catalog_evidence_sha256": (
                "e18acde5ba79608fa56e63e968e2e0ab"
                "e02e19e72d0628aa42a55b7f4cfe0c72"
            ),
        }
        package = load_function_creation_package(directory, identity)
        source = (
            directory / package.functions[0].forward_path
        ).read_text(encoding="utf-8")
        for relation in (
            "memory.openai_provider_request_receipt_v1",
            "memory.openai_provider_completion_receipt_v1",
            "memory.evidence_extraction_packet_v5",
            "memory.v5_extraction_call_event",
        ):
            self.assertIn(relation, source)
        self.assertIn("p_expected_attempts<>0", source)
        self.assertNotIn("DELETE FROM", source.upper())
        self.assertNotIn("TRUNCATE", source.upper())


class FunctionCreationRegistryTest(unittest.TestCase):
    def test_registry_is_exact_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = load_function_creation_package(write_package(pathlib.Path(temporary)), LEDGER)
            payload = canonical_bytes(
                {
                    "schema_version": "governed-function-creation-registry-v1",
                    "canonicalization": "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1",
                    "packages": [
                        {
                            "migration_id": package.migration_id,
                            "package_sha256": package.package_sha256,
                            "lane": "personal_memory",
                            "status": "active",
                        }
                    ],
                }
            )
            registry = parse_function_creation_registry(payload)
            validate_function_creation_registry_append_only({}, registry)
            validate_function_creation_registry([package], registry)
            with self.assertRaises(MigrationError):
                validate_function_creation_registry_append_only(registry, {})


if __name__ == "__main__":
    unittest.main()
