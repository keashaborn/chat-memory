from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, TOOLS.as_posix())

from build_schema_ledger import lanes, migration_records  # noqa: E402
from schema_ledger import (  # noqa: E402
    CANONICALIZATION,
    CATALOG_OBJECT_KINDS,
    CATALOG_SCHEMA,
    GitBlob,
    LEDGER_SCHEMA,
    QDRANT_SCHEMA,
    SOURCE_SCHEMA,
    LedgerError,
    analyze_sql,
    build_source_inventory,
    canonical_bytes,
    catalog_object_records,
    fingerprint_record,
    git_sql_blobs,
    parse_canonical,
    reconcile,
    sha256,
    validate_catalog_snapshot,
    validate_ledger,
    validate_qdrant_metadata,
    validate_source_inventory,
    sanitize_text,
)
import source_record_projection  # noqa: E402
from run_readonly_production_audit import build_in_memory_source_helper  # noqa: E402
from source_record_projection import SourceProjectionError, project_source_record  # noqa: E402
from validate_schema_ledger import compare_source_records, validate_repository_sql  # noqa: E402
from verify_production_compatibility import catalog_stable_identity  # noqa: E402


def catalog(objects: dict[str, list[dict[str, object]]] | None = None) -> dict[str, object]:
    inventory = {kind: [] for kind in CATALOG_OBJECT_KINDS}
    if objects:
        inventory.update(objects)
    return {
        "schema_version": CATALOG_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "source": {"container": "postgres-test", "database": "memory", "user": "sage"},
        "read_only_proof": {"transaction_read_only": "on", "transaction_isolation": "repeatable read", "row_data_read": False, "vector_payload_read": False},
        "observed_at_utc": "2026-08-01T12:00:00.000000Z",
        "database": {"name": "memory", "owner": "sage", "server_version": "16.10", "server_version_num": "160010", "encoding": "UTF8", "collation": "C", "ctype": "C", "acl": []},
        "objects": inventory,
        "scheduler_capabilities": {"pg_cron_installed": False, "timescaledb_installed": False, "database_job_rows_read": False},
        "migration_history_relations": [],
        "schema_only_dump": {"sha256": "a" * 64, "raw_size": 10, "canonical_size": 10, "canonicalization": "strip-matched-pg-restrict-token-lf-v1", "contains_row_data": False, "retained": False},
    }


def qdrant() -> dict[str, object]:
    value = {
        "schema_version": QDRANT_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "authority": "derived_rebuildable",
        "endpoint": "loopback",
        "version": "1.11.0",
        "point_payload_read": False,
        "point_search_or_scroll": False,
        "collections": [{"name": "memory_raw", "status": "green", "parameters": {"vectors": {"size": 3, "distance": "Cosine"}}, "hnsw_config": {}, "optimizer_config": {}, "wal_config": {}, "payload_schema": [{"name": "owner_user_id", "data_type": "keyword"}]}],
    }
    value["metadata_sha256"] = sha256(canonical_bytes(value))
    return value


def migration(identifier: str, path: str, order: int, digest: str, **updates: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": identifier,
        "path": path,
        "sha256": digest,
        "git_blob": "b" * 40,
        "size": 10,
        "mode": "100644",
        "kind": "forward",
        "lane": "personal_memory",
        "order": order,
        "dependencies": [],
        "classification": "unverifiable",
        "duplicate_of": None,
        "rollback_for": None,
        "execution_owner": "unverifiable",
        "transaction_mode": "unspecified",
        "recovery": "none_declared",
        "unsafe_categories": [],
        "declared_objects": [],
        "apply_evidence": "no production migration-history relation; apply state is unverifiable",
    }
    item.update(updates)
    return item


def ledger(migrations: list[dict[str, object]] | None = None, objects: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": LEDGER_SCHEMA,
        "canonicalization": CANONICALIZATION,
        "ledger_id": "test-ledger",
        "authority": {"live_schema": "postgres", "intended_history": "schema_ledger", "derived_index": "qdrant_rebuildable", "extraction": "proposal_only"},
        "baseline": {
            "production_commit": "a" * 40,
            "production_tree": "b" * 40,
            "catalog_evidence_path": "evidence/catalog.json",
            "catalog_evidence_sha256": "c" * 64,
            "source_evidence_path": "evidence/sources.json",
            "source_evidence_sha256": "d" * 64,
            "qdrant_evidence_path": "evidence/qdrant.json",
            "qdrant_evidence_sha256": "e" * 64,
            "reconciliation_evidence_path": "evidence/reconciliation.json",
            "reconciliation_evidence_sha256": "f" * 64,
            "production_ref_count": 1,
            "production_ref_sha256": "1" * 64,
            "production_worktree_count": 1,
            "production_worktree_sha256": "2" * 64,
        },
        "lanes": lanes(),
        "migrations": migrations or [],
        "object_expectations": objects or [],
    }


class CanonicalEncodingTests(unittest.TestCase):
    def test_exact_round_trip(self) -> None:
        raw = canonical_bytes({"b": [True, None], "a": "value"})
        self.assertEqual(parse_canonical(raw), {"a": "value", "b": [True, None]})

    def test_rejects_noncanonical_and_hostile_encodings(self) -> None:
        bad = [b'{"b":1,"a":2}\n', b'{"a":1}', b'{"a":1}\r\n', b'{"a":1,"a":2}\n', b'{"a":1.2}\n', b'{"a":"x\\u0000"}\n', b'{"a":1}\n\0']
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(LedgerError):
                parse_canonical(payload)

    def test_sensitive_identifier_token_is_deterministically_aliased(self) -> None:
        sensitive = bytes.fromhex("6a65727279").decode("ascii")
        aliased = sanitize_text("case-" + sensitive + "-record")
        self.assertRegex(aliased, r"^case-subject_[0-9a-f]{16}_[0-9a-f]{12}-record$")
        self.assertNotIn(sensitive, aliased.lower())


class LedgerContractTests(unittest.TestCase):
    def test_valid_minimal_ledger(self) -> None:
        self.assertEqual(validate_ledger(ledger())["ledger_id"], "test-ledger")

    def test_ordering_duplicate_id_and_missing_dependency_fail(self) -> None:
        cases = [
            [migration("m:a", "ops/sql/a.sql", 2, "1" * 64), migration("m:b", "ops/sql/b.sql", 1, "2" * 64)],
            [migration("m:a", "ops/sql/a.sql", 1, "1" * 64), migration("m:a", "ops/sql/b.sql", 2, "2" * 64)],
            [migration("m:a", "ops/sql/a.sql", 1, "1" * 64, dependencies=["m:missing"])],
        ]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(LedgerError):
                validate_ledger(ledger(items))

    def test_dependency_cycle_fails(self) -> None:
        items = [migration("m:a", "ops/sql/a.sql", 1, "1" * 64, dependencies=["m:b"]), migration("m:b", "ops/sql/b.sql", 2, "2" * 64, dependencies=["m:a"])]
        with self.assertRaisesRegex(LedgerError, "cycle"):
            validate_ledger(ledger(items))

    def test_duplicate_hash_requires_explicit_secondary(self) -> None:
        items = [migration("m:a", "ops/sql/a.sql", 1, "1" * 64), migration("m:b", "ops/sql/b.sql", 2, "1" * 64)]
        with self.assertRaisesRegex(LedgerError, "duplicate migration hash"):
            validate_ledger(ledger(items))
        items[1]["classification"] = "duplicated"
        items[1]["duplicate_of"] = "m:a"
        validate_ledger(ledger(items))

    def test_lane_order_and_separation_are_exact(self) -> None:
        value = ledger()
        value["lanes"] = list(reversed(value["lanes"]))
        with self.assertRaisesRegex(LedgerError, "lane order"):
            validate_ledger(value)


class SqlClassificationTests(unittest.TestCase):
    def test_unsafe_sql_and_literals_are_distinguished(self) -> None:
        unsafe, declarations = analyze_sql(b"-- DROP TABLE ignored\nCREATE TABLE memory.t(id int); UPDATE memory.t SET id=1;")
        self.assertEqual(unsafe, ["data_update"])
        self.assertEqual(declarations, ["table:memory.t"])

    def test_transaction_and_recovery_classification(self) -> None:
        from schema_ledger import GitBlob

        source = build_source_inventory(
            [
                GitBlob("ops/sql/a.sql", "100644", "a" * 40, b"BEGIN; CREATE TABLE memory.a(id int); COMMIT;"),
                GitBlob("ops/sql/a_rollback.sql", "100644", "b" * 40, b"DROP TABLE memory.a;"),
                GitBlob("ops/sql/b.sql", "100644", "c" * 40, b"CREATE INDEX CONCURRENTLY b_idx ON memory.a(id);"),
            ],
            "d" * 40,
        )
        by_path = {item["path"]: item for item in source["sources"]}
        self.assertEqual(by_path["ops/sql/a.sql"]["transaction_mode"], "explicit")
        self.assertEqual(by_path["ops/sql/b.sql"]["transaction_mode"], "nontransactional")

    def test_shared_projection_sanitizes_declarations_and_is_deterministic(self) -> None:
        sensitive = bytes.fromhex("6a65727279").decode("ascii")
        payload = f"CREATE TABLE memory.{sensitive}(id int);\n".encode()
        first = project_source_record("ops/sql/memory_fixture.sql", "100644", "a" * 40, payload)
        second = project_source_record("ops/sql/memory_fixture.sql", "100644", "a" * 40, payload)
        self.assertEqual(first, second)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        self.assertNotIn(sensitive, canonical_bytes(first).decode().lower())

    def test_shared_projection_rejects_hostile_metadata_and_oversized_blob(self) -> None:
        cases = [
            ("/absolute.sql", "100644", "a" * 40, b"SELECT 1;"),
            ("ops/../escape.sql", "100644", "a" * 40, b"SELECT 1;"),
            ("ops/sql/bad\x00.sql", "100644", "a" * 40, b"SELECT 1;"),
            ("ops/sql/a.sql", "100600", "a" * 40, b"SELECT 1;"),
            ("ops/sql/a.sql", "100644", "BAD", b"SELECT 1;"),
        ]
        for case in cases:
            with self.subTest(case=case[:3]), self.assertRaises(SourceProjectionError):
                project_source_record(*case)
        with mock.patch.object(source_record_projection, "MAX_BLOB", 4), self.assertRaises(SourceProjectionError):
            project_source_record("ops/sql/a.sql", "100644", "a" * 40, b"12345")

    def test_projection_implementation_exists_in_one_module_only(self) -> None:
        names_by_file = {}
        for name in ("source_record_projection.py", "schema_ledger.py", "readonly_repository_schema_sources.py"):
            tree = ast.parse((TOOLS / name).read_text())
            names_by_file[name] = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("project_source_record", names_by_file["source_record_projection.py"])
        for duplicate in ("strip_sql", "analyze_sql", "source_lane", "source_kind", "project_source_record"):
            self.assertNotIn(duplicate, names_by_file["schema_ledger.py"])
            self.assertNotIn(duplicate, names_by_file["readonly_repository_schema_sources.py"])

    def test_remote_source_helper_loads_shared_projection_from_reviewed_memory_payload(self) -> None:
        program = build_in_memory_source_helper(b"VALUE=7\n", b"from source_record_projection import VALUE\nprint(VALUE)\n")
        result = subprocess.run(
            [sys.executable, "-"],
            input=program,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={"LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"7\n")

    def test_full_sanitized_source_evidence_has_exact_zero_mismatch_round_trip(self) -> None:
        source_path = ROOT / "evidence/production-schema-sources-d554a3d9.json"
        raw = source_path.read_bytes()
        value = parse_canonical(raw)
        records = value["sources"]
        comparison = compare_source_records(records, parse_canonical(canonical_bytes(records)))
        self.assertEqual(len(records), 667)
        self.assertEqual(comparison["mismatched_record_count"], 0)
        self.assertEqual(comparison["mismatch_by_field"], {})
        self.assertEqual(comparison["canonical_sha256"], sha256(canonical_bytes(records)))


class CatalogContractTests(unittest.TestCase):
    def test_read_only_and_schema_dump_boundaries(self) -> None:
        value = catalog()
        validate_catalog_snapshot(value)
        for field, replacement in (("contains_row_data", True), ("retained", True), ("sha256", "BAD"), ("canonical_size", 0)):
            modified = copy.deepcopy(value)
            modified["schema_only_dump"][field] = replacement
            with self.subTest(field=field), self.assertRaises(LedgerError):
                validate_catalog_snapshot(modified)
        modified = copy.deepcopy(value)
        modified["read_only_proof"]["transaction_read_only"] = "off"
        with self.assertRaises(LedgerError):
            validate_catalog_snapshot(modified)

    def test_schema_dump_volatile_token_is_removed_deterministically(self) -> None:
        from readonly_pg_catalog_snapshot import canonical_schema_dump

        first = b"header\n\\restrict RANDOM_ONE\nCREATE TABLE x();\n\\unrestrict RANDOM_ONE\n"
        second = b"header\n\\restrict RANDOM_TWO\nCREATE TABLE x();\n\\unrestrict RANDOM_TWO\n"
        self.assertEqual(canonical_schema_dump(first), canonical_schema_dump(second))
        with self.assertRaises(RuntimeError):
            canonical_schema_dump(b"\\restrict A\ncontent\n\\unrestrict B\n")

    def test_missing_extra_and_divergent_objects(self) -> None:
        left = [{"id": "relation:memory.a", "fingerprint_sha256": "1" * 64}, {"id": "relation:memory.b", "fingerprint_sha256": "2" * 64}]
        right = [{"id": "relation:memory.a", "fingerprint_sha256": "9" * 64}, {"id": "relation:memory.c", "fingerprint_sha256": "3" * 64}]
        result = reconcile(left, right)
        self.assertEqual(result["counts"], {"divergent": 1, "production-only/orphaned": 1, "source-only": 1})

    def test_rls_policy_grant_owner_default_generated_and_extension_drift(self) -> None:
        cases = [
            ({"owner": "sage"}, {"owner": "other"}),
            ({"rls_enabled": True, "rls_forced": True}, {"rls_enabled": True, "rls_forced": False}),
            ({"roles": ["app"], "using_sha256": "1" * 64}, {"roles": ["other"], "using_sha256": "1" * 64}),
            ({"acl": ["app=r/sage"]}, {"acl": []}),
            ({"default_sha256": "1" * 64}, {"default_sha256": "2" * 64}),
            ({"generated_kind": "s"}, {"generated_kind": ""}),
            ({"version": "1.0"}, {"version": "1.1"}),
        ]
        for before, after in cases:
            with self.subTest(before=before):
                self.assertNotEqual(fingerprint_record(before), fingerprint_record(after))

    def test_catalog_stable_identity_ignores_only_observation_time(self) -> None:
        first = catalog()
        second = copy.deepcopy(first)
        second["observed_at_utc"] = "2026-08-01T13:00:00.000000Z"
        self.assertEqual(catalog_stable_identity(first), catalog_stable_identity(second))
        second["database"]["owner"] = "changed"
        self.assertNotEqual(catalog_stable_identity(first), catalog_stable_identity(second))

    def test_hostile_identity_collision_fails(self) -> None:
        value = catalog({"relation": [{"schema": "memory", "name": "a", "identity": "same"}, {"schema": "memory", "name": "b", "identity": "same"}]})
        with self.assertRaisesRegex(LedgerError, "collide"):
            validate_catalog_snapshot(value)


class SourceAndDerivedMetadataTests(unittest.TestCase):
    def test_qdrant_metadata_only_contract_and_tamper(self) -> None:
        value = qdrant()
        validate_qdrant_metadata(value)
        for field in ("point_payload_read", "point_search_or_scroll"):
            modified = copy.deepcopy(value)
            modified[field] = True
            with self.subTest(field=field), self.assertRaises(LedgerError):
                validate_qdrant_metadata(modified)
        modified = copy.deepcopy(value)
        modified["collections"][0]["status"] = "changed"
        with self.assertRaisesRegex(LedgerError, "hash diverged"):
            validate_qdrant_metadata(modified)

    def test_source_inventory_rejects_altered_duplicate_index(self) -> None:
        current = json.loads((ROOT / "evidence/production-schema-sources-d554a3d9.json").read_text())
        validate_source_inventory(current)
        modified = copy.deepcopy(current)
        modified["duplicate_hashes"] = []
        with self.assertRaisesRegex(LedgerError, "duplicate-source"):
            validate_source_inventory(modified)

    def test_altered_migration_changes_ledger_record(self) -> None:
        current = json.loads((ROOT / "evidence/production-schema-sources-d554a3d9.json").read_text())
        before = migration_records(current)
        modified = copy.deepcopy(current)
        modified["sources"][0]["sha256"] = "0" * 64
        after = migration_records(modified)
        self.assertNotEqual(before[0]["sha256"], after[0]["sha256"])


class SyntheticRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir(mode=0o700)
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "ops/sql").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "ops/sql/001.sql").write_text("BEGIN; CREATE TABLE memory.a(id int); COMMIT;\n")
        (self.repo / "ops/sql/001_rollback.sql").write_text("DROP TABLE memory.a;\n")
        (self.repo / "tests/fixture.sql").write_text("SELECT 1;\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["/usr/bin/git", "-C", self.repo.as_posix(), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env={"LANG": "C", "LC_ALL": "C"})

    def test_git_object_inventory_and_index_are_read_only(self) -> None:
        commit = self.git("rev-parse", "HEAD").stdout.strip().decode()
        index = self.repo / ".git/index"
        before = (index.read_bytes(), index.stat().st_ino, index.stat().st_mtime_ns)
        blobs = git_sql_blobs(self.repo, commit)
        after = (index.read_bytes(), index.stat().st_ino, index.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual([blob.path for blob in blobs], ["ops/sql/001.sql", "ops/sql/001_rollback.sql", "tests/fixture.sql"])

    def test_repository_sql_comparison_rejects_added_or_altered_source(self) -> None:
        commit = self.git("rev-parse", "HEAD").stdout.strip().decode()
        source = build_source_inventory(git_sql_blobs(self.repo, commit), commit)
        validate_repository_sql(self.repo, commit, source)
        validate_repository_sql(self.repo, "HEAD", source)
        (self.repo / "ops/sql/002.sql").write_text("CREATE TABLE memory.b(id int);\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "altered")
        altered = self.git("rev-parse", "HEAD").stdout.strip().decode()
        with self.assertRaisesRegex(RuntimeError, "diverged"):
            validate_repository_sql(self.repo, altered, source)
        with self.assertRaisesRegex(RuntimeError, "diverged"):
            validate_repository_sql(self.repo, "HEAD", source)

    def test_repository_sql_comparison_rejects_unreviewed_symbolic_revision(self) -> None:
        commit = self.git("rev-parse", "HEAD").stdout.strip().decode()
        source = build_source_inventory(git_sql_blobs(self.repo, commit), commit)
        with self.assertRaisesRegex(LedgerError, "commit is malformed"):
            validate_repository_sql(self.repo, "HEAD~1", source)

    def test_offline_source_lane_projection_matches_production_inventory_contract(self) -> None:
        records = build_source_inventory(
            [
                GitBlob("ops/sql/001_memory.sql", "100644", "a" * 40, b"SELECT 1;\n"),
                GitBlob("ops/sql/002_memory_qdrant.sql", "100644", "b" * 40, b"SELECT 1;\n"),
                GitBlob("ops/sql/003_lifeswitch.sql", "100644", "c" * 40, b"SELECT 1;\n"),
            ],
            "d" * 40,
        )["sources"]
        self.assertEqual(
            [record["lane"] for record in records],
            ["personal_memory", "derived_index_coordination", "lifeswitch_live_context"],
        )

    def test_fixed_source_helper_detects_untracked_dirtiness(self) -> None:
        commit = self.git("rev-parse", "HEAD").stdout.strip().decode()
        (self.repo / "untracked.txt").write_text("dirty")
        result = subprocess.run([sys.executable, (TOOLS / "readonly_repository_schema_sources.py").as_posix(), "--source", self.repo.as_posix(), "--expected-commit", commit], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env={"LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")


class EndToEndCandidateTests(unittest.TestCase):
    def test_current_candidate_validates_offline(self) -> None:
        result = subprocess.run([sys.executable, (TOOLS / "validate_schema_ledger.py").as_posix(), "--root", ROOT.as_posix()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env={"LANG": "C", "LC_ALL": "C", "PYTHONPATH": TOOLS.as_posix(), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        report = parse_canonical(result.stdout)
        self.assertEqual(report["status"], "valid_candidate")
        self.assertEqual(report["migration_count"], 667)
        self.assertEqual(report["catalog_object_count"], 11967)

    def test_evidence_hash_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = pathlib.Path(directory) / "candidate"
            shutil.copytree(ROOT / "ledger", copied / "ledger")
            shutil.copytree(ROOT / "evidence", copied / "evidence")
            target = copied / "evidence/production-qdrant-metadata-d554a3d9.json"
            value = json.loads(target.read_text())
            value["collections"][0]["status"] = "tampered"
            target.write_bytes(canonical_bytes(value))
            result = subprocess.run([sys.executable, (TOOLS / "validate_schema_ledger.py").as_posix(), "--root", copied.as_posix()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env={"LANG": "C", "LC_ALL": "C", "PYTHONPATH": TOOLS.as_posix(), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"evidence hash diverged", result.stderr)

    def test_manifest_verifies_exact_file_set(self) -> None:
        result = subprocess.run([sys.executable, (TOOLS / "verify_manifest.py").as_posix(), "--root", ROOT.as_posix()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env={"LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"manifest valid", result.stdout)

    def test_ci_adoption_contract_is_canonical_and_read_only(self) -> None:
        raw = (ROOT / "ci/auditability-schema-ledger-v1.json").read_bytes()
        value = parse_canonical(raw)
        self.assertEqual(value["schema_version"], "schema-ledger-ci-adoption-v1")
        self.assertFalse(value["network_or_production_database_access"])
        self.assertEqual(value["target"], ".github/workflows/auditability.yml")
        self.assertEqual(len(value["commands"]), 3)

    def test_candidate_has_no_known_sensitive_plaintext_or_secret_shapes(self) -> None:
        sensitive = bytes.fromhex("6a65727279")
        patterns = [
            re.compile(re.escape(sensitive), re.I),
            re.compile(rb"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I),
            re.compile(rb"(?:postgres(?:ql)?|redis|mongodb)://", re.I),
            re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY"),
            re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[oprsu]_[A-Za-z0-9]{20,})\b", re.I),
        ]
        for path in ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            payload = path.read_bytes()
            for pattern in patterns:
                with self.subTest(path=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(payload))


if __name__ == "__main__":
    unittest.main()
