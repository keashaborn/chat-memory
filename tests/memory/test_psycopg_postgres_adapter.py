from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.governed_memory_install.controller_runtime import (
    VerifiedControllerRuntimeEvidence,
    _RUNTIME_TOKEN,
    _VerifiedControllerRuntimeCapability,
)
from tools.governed_memory_install.postgres_native_stages import (
    EXPECTED_DRIVER_IDENTITY_SHA256,
    ROLLBACK_TRANSITIONS,
    RollbackObservation,
    RollbackOperation,
    RollbackPrefix,
    SOURCE_SHA256,
)
from tools.governed_memory_install.psycopg_postgres_adapter import (
    PsycopgPostgreSQLAdapter,
    PsycopgPostgreSQLAdapterError,
    _mint_synthetic_runtime_capability,
    _split_fixed_sql,
    verified_psycopg_runtime_capability,
)


ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


class SecretSource:
    def read_fixed_postgres_password(self) -> bytes:
        return b"x" * 43


class ReceiptSink:
    def persist_postgres_stage_receipt(self, receipt: object) -> None:
        del receipt

    def postgres_controller_authority_marker_held(self) -> bool:
        return True


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None = None, name: str = "") -> None:
        self._row = row
        self.description = () if row is None else (SimpleNamespace(name=name),)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _BootstrapConnection:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def execute(self, sql: str) -> _Cursor:
        self._executed.append(sql)
        if "governed_memory_fresh_cluster" in sql:
            return _Cursor((True,), "governed_memory_fresh_cluster")
        if "governed_memory_bootstrap_verified" in sql:
            return _Cursor((True,), "governed_memory_bootstrap_verified")
        return _Cursor()

    def close(self) -> None:
        return None


class _BootstrapScriptAdapter(PsycopgPostgreSQLAdapter):
    def __init__(self) -> None:
        self.executed: list[str] = []

    def _connect(self, database: str, *, autocommit: bool) -> _BootstrapConnection:
        del database, autocommit
        return _BootstrapConnection(self.executed)


def _runtime_evidence() -> VerifiedControllerRuntimeEvidence:
    return VerifiedControllerRuntimeEvidence(
        result_type="verified_controller_runtime_v1",
        controller_runtime_receipt_sha256=HASH,
        build_plan_sha256=HASH,
        package_manifest_sha256=HASH,
        controller_runtime_contract_sha256=HASH,
        controller_requirements_lock_sha256=HASH,
        standalone_cpython_specification_sha256=HASH,
        standalone_cpython_archive_sha256=HASH,
        standalone_cpython_payload_tree_sha256=HASH,
        wheelhouse_tree_sha256=HASH,
        runtime_root=f"/opt/governed-memory-controller/runtimes/{HASH}",
        runtime_tree_sha256=HASH,
        release_root=f"/opt/governed-memory-controller/releases/{HASH}",
        release_tree_sha256=HASH,
        package_manifest_path=(
            f"/opt/governed-memory-controller/releases/{HASH}/ops/"
            "governed_memory/installation/current/package_manifest.json"
        ),
        interpreter_path=(
            f"/opt/governed-memory-controller/runtimes/{HASH}/bin/python"
        ),
        interpreter_sha256=HASH,
        inventory_path=(
            f"/opt/governed-memory-controller/runtimes/{HASH}/"
            "controller-distributions.json"
        ),
        installed_distribution_inventory_sha256=HASH,
        interpreter_path_facts_sha256=HASH,
        postgresql_driver_identity_sha256=EXPECTED_DRIVER_IDENTITY_SHA256,
        supervisor_launcher_path=(
            f"/opt/governed-memory-controller/releases/{HASH}/tools/"
            "governed_memory_install/store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256=HASH,
        launcher_help_probe_sha256=HASH,
        python_implementation="CPython",
        python_version="3.12.13",
        platform_os="linux",
        platform_architecture="x86_64",
        persistent_controller_substrate_created=True,
        persistent_store_resources_created=False,
    )


class SyntheticRollbackAdapter(PsycopgPostgreSQLAdapter):
    def __init__(self, prefix: RollbackPrefix) -> None:
        self.prefix = prefix
        self.calls: list[object] = []
        self._last_empty_proof_sha256 = "b" * 64
        self._forward_session_prefix = None

    @staticmethod
    def _load_fixed_source(expected_path: str) -> object:
        return expected_path

    def observe_rollback_prefix(self) -> RollbackObservation:
        return RollbackObservation(self.prefix, "b" * 64)

    def rollback_migration_0004(self, source: object) -> None:
        self.calls.append((RollbackOperation.R01_ROLLBACK_PILOT_MARKER_0004, source))
        self.prefix = RollbackPrefix.WITHOUT_0004

    def rollback_migration_0003(self, source: object) -> None:
        self.calls.append(
            (RollbackOperation.R01_ROLLBACK_OWNER_CLAIM_DETAIL_0003, source)
        )
        self.prefix = RollbackPrefix.WITHOUT_0003

    def rollback_migration_0001(self, source: object) -> None:
        self.calls.append((RollbackOperation.R01_ROLLBACK_FOUNDATION_0001, source))
        self.prefix = RollbackPrefix.DATABASE_PREFIX

    def rollback_canonical_bootstrap(self, source: object) -> None:
        self.calls.append((RollbackOperation.R01_DROP_EXACT_DATABASE_PREFIX, source))
        self.prefix = RollbackPrefix.ROLES_ONLY_5

    def _require_rollback_boundary(self) -> None:
        self.calls.append("boundary")

    def _execute_fixed_psql_script(self, source: object, *, rollback: bool) -> None:
        self.calls.append(
            (RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION, source, rollback)
        )
        self.prefix = RollbackPrefix.EMPTY


class PsycopgPostgreSQLAdapterTests(unittest.TestCase):
    def test_public_constructor_accepts_capabilities_not_endpoint_or_sql(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(PsycopgPostgreSQLAdapter).parameters),
            ("secret_source", "receipt_sink", "runtime_capability"),
        )
        capability = _mint_synthetic_runtime_capability(
            runtime_receipt_sha256=HASH,
            native_closure_receipt_sha256=HASH,
        )
        adapter = PsycopgPostgreSQLAdapter(
            secret_source=SecretSource(),
            receipt_sink=ReceiptSink(),
            runtime_capability=capability,
        )
        self.assertEqual(adapter.bind, "127.0.0.1:55433")
        self.assertIsNone(adapter._lock_connection)
        for forbidden in ("dsn", "host", "port", "database", "role", "sql", "path"):
            self.assertNotIn(
                forbidden,
                inspect.signature(PsycopgPostgreSQLAdapter).parameters,
            )

    def test_runtime_capability_is_derived_from_verified_controller_evidence(self) -> None:
        source = _VerifiedControllerRuntimeCapability(_runtime_evidence(), _RUNTIME_TOKEN)
        capability = verified_psycopg_runtime_capability(source)
        self.assertEqual(
            capability.driver_identity_sha256, EXPECTED_DRIVER_IDENTITY_SHA256
        )
        self.assertEqual(capability.libpq_version, 180000)
        self.assertEqual(capability.native_file_count, 17)
        tampered = _VerifiedControllerRuntimeCapability(
            replace(_runtime_evidence(), python_version="3.12.12"), _RUNTIME_TOKEN
        )
        with self.assertRaisesRegex(
            PsycopgPostgreSQLAdapterError,
            "psycopg_controller_runtime_evidence_invalid",
        ):
            verified_psycopg_runtime_capability(tampered)

    def test_exact_source_loader_rejects_any_hash_or_path_drift(self) -> None:
        for path, expected in SOURCE_SHA256.items():
            source = PsycopgPostgreSQLAdapter._load_fixed_source(path)
            self.assertEqual(source.path, path)
            self.assertEqual(source.sha256, expected)
            self.assertEqual(source.content, (ROOT / path).read_bytes())
        with self.assertRaisesRegex(
            PsycopgPostgreSQLAdapterError, "postgres_source_identity_invalid"
        ):
            PsycopgPostgreSQLAdapter._load_fixed_source("caller.sql")

    def test_fixed_sql_splitter_preserves_dollar_quoted_bodies(self) -> None:
        source = (
            "CREATE FUNCTION x() RETURNS void LANGUAGE plpgsql AS $body$\n"
            "BEGIN PERFORM ';'; END;\n$body$;\nSELECT 1;"
        )
        statements = _split_fixed_sql(source)
        self.assertEqual(len(statements), 2)
        self.assertIn("PERFORM ';'", statements[0])
        self.assertEqual(statements[1], "SELECT 1;")
        foundation = (ROOT / "governed-memory-migrations/0001_foundation/forward.pgsql").read_text(
            encoding="utf-8"
        )
        split = _split_fixed_sql(foundation)
        self.assertGreater(len(split), 100)
        self.assertTrue(all(statement.strip() for statement in split))

    def test_exact_migration_allows_sql_backslashes_but_not_meta_commands(self) -> None:
        foundation = (
            ROOT / "governed-memory-migrations/0001_foundation/forward.pgsql"
        ).read_text(encoding="utf-8")
        self.assertIn("E'\\n'", foundation)
        self.assertFalse(
            any(line.lstrip().startswith("\\") for line in foundation.splitlines())
        )
        self.assertTrue(
            any(
                line.lstrip().startswith("\\")
                for line in "SELECT 1;\n\\connect caller".splitlines()
            )
        )

    def test_exact_bootstrap_gset_executes_prefix_and_captures_final_query(self) -> None:
        adapter = _BootstrapScriptAdapter()
        source = PsycopgPostgreSQLAdapter._load_fixed_source(
            "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in"
        )
        adapter._execute_fixed_psql_script(source, rollback=False)
        self.assertGreater(len(adapter.executed), 12)
        self.assertTrue(
            any("CREATE EXTENSION pgcrypto" in sql for sql in adapter.executed)
        )
        self.assertTrue(
            any("governed_memory_bootstrap_verified" in sql for sql in adapter.executed)
        )

    def test_rollback_operations_map_to_exact_sources_and_resume_prefixes(self) -> None:
        adapter = SyntheticRollbackAdapter(RollbackPrefix.INSTALLED)
        while adapter.prefix is not RollbackPrefix.EMPTY:
            operation, expected = ROLLBACK_TRANSITIONS[adapter.prefix]
            observed = adapter.apply_rollback_transition(operation)
            self.assertIs(observed.prefix, expected)
        paths = tuple(
            call[1]
            for call in adapter.calls
            if type(call) is tuple and len(call) >= 2
        )
        self.assertEqual(
            paths,
            (
                "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
                "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
                "governed-memory-migrations/0001_foundation/rollback.pgsql",
                (
                    "ops/governed_memory/installation/postgres/"
                    "canonical_cluster_rollback.pgsql.in"
                ),
                (
                    "ops/governed_memory/installation/postgres/"
                    "canonical_cluster_rollback.pgsql.in"
                ),
            ),
        )
        resume = SyntheticRollbackAdapter(RollbackPrefix.WITHOUT_0003)
        operation, expected = ROLLBACK_TRANSITIONS[resume.prefix]
        self.assertIs(resume.apply_rollback_transition(operation).prefix, expected)
        self.assertEqual(len(resume.calls), 1)


if __name__ == "__main__":
    unittest.main()
