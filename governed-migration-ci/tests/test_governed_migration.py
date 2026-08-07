from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from governed_migration import (  # noqa: E402
    CANONICALIZATION,
    EXPECTED_EXTERNAL_DEPLOYED_SQL_V1,
    EXPECTED_LEGACY_SQL_PATH_ALIASES_V1,
    MigrationError,
    canonical_bytes,
    classify_sql,
    load_package,
    load_registry,
    parse_canonical,
    parse_external_deployed_sql_registry,
    parse_legacy_sql_path_alias_registry,
    sha256,
    validate_dependency_graph,
    validate_external_deployed_sql_commit_bindings,
    validate_external_deployed_sql_registry_append_only,
    validate_legacy_sql_path_alias_commit_bindings,
    validate_legacy_sql_path_alias_registry_append_only,
    validate_registry,
    validate_registry_append_only,
    validate_repository_inventory,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/gm_ci_add_governed_record_v1"
IDENTITY = {
    "ledger_id": "governed-memory-v1-initial-production-baseline",
    "ledger_sha256": "c2195f4d5ea132f6eb82781ca34f7ae12516d81c6eaca3b9488d1f51037da995",
    "catalog_evidence_sha256": "e18acde5ba79608fa56e63e968e2e0abe02e19e72d0628aa42a55b7f4cfe0c72",
}


def write_canonical(path: pathlib.Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value))


class PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.package = self.root / FIXTURE.name
        shutil.copytree(FIXTURE, self.package)

    def value(self) -> dict[str, object]:
        return json.loads((self.package / "package.json").read_text())

    def update(self, mutate) -> None:
        value = self.value()
        mutate(value)
        write_canonical(self.package / "package.json", value)

    def test_valid_package_is_stable(self) -> None:
        first = load_package(self.package, IDENTITY)
        second = load_package(self.package, IDENTITY)
        self.assertEqual(first.package_sha256, second.package_sha256)
        self.assertEqual(first.migration_id, FIXTURE.name)

    def test_altered_forward_bytes_rejected(self) -> None:
        (self.package / "forward.pgsql").write_bytes((self.package / "forward.pgsql").read_bytes() + b"\n")
        with self.assertRaisesRegex(MigrationError, "forward bytes changed"):
            load_package(self.package, IDENTITY)

    def test_missing_recovery_rejected(self) -> None:
        self.update(lambda value: value.pop("recovery"))
        with self.assertRaisesRegex(MigrationError, "fields"):
            load_package(self.package, IDENTITY)

    def test_missing_verification_rejected(self) -> None:
        self.update(lambda value: value.pop("verification"))
        with self.assertRaisesRegex(MigrationError, "fields"):
            load_package(self.package, IDENTITY)

    def test_unknown_field_rejected(self) -> None:
        self.update(lambda value: value.__setitem__("unexpected", True))
        with self.assertRaisesRegex(MigrationError, "fields"):
            load_package(self.package, IDENTITY)

    def test_nontransactional_mode_rejected(self) -> None:
        self.update(lambda value: value["transaction"].__setitem__("mode", "forbidden"))
        with self.assertRaisesRegex(MigrationError, "transactions"):
            load_package(self.package, IDENTITY)

    def test_invalid_timeout_rejected(self) -> None:
        self.update(lambda value: value["transaction"].__setitem__("lock_timeout_ms", 0))
        with self.assertRaisesRegex(MigrationError, "timeout"):
            load_package(self.package, IDENTITY)

    def test_baseline_drift_rejected(self) -> None:
        self.update(lambda value: value["baseline"].__setitem__("ledger_sha256", "a" * 64))
        with self.assertRaisesRegex(MigrationError, "baseline diverges"):
            load_package(self.package, IDENTITY)

    def test_policy_exception_rejected(self) -> None:
        self.update(lambda value: value["policy"].__setitem__("grant_widening_allowed", True))
        with self.assertRaisesRegex(MigrationError, "deny every"):
            load_package(self.package, IDENTITY)

    def test_policy_only_lane_rejected(self) -> None:
        self.update(lambda value: value.__setitem__("lane", "fractal_monism_policy"))
        with self.assertRaisesRegex(MigrationError, "policy-only"):
            load_package(self.package, IDENTITY)

    def test_owner_malformed_rejected(self) -> None:
        self.update(lambda value: value.__setitem__("execution_owner", "Role With Space"))
        with self.assertRaisesRegex(MigrationError, "owner"):
            load_package(self.package, IDENTITY)

    def test_symlinked_action_rejected(self) -> None:
        original = self.package / "forward.pgsql"
        external = self.root / "external.pgsql"
        external.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(external)
        with self.assertRaisesRegex(MigrationError, "escaped or is a symlink"):
            load_package(self.package, IDENTITY)


class CanonicalTest(unittest.TestCase):
    def test_canonical_round_trip(self) -> None:
        value = {"schema_version": "x", "items": [True, 1, None]}
        self.assertEqual(parse_canonical(canonical_bytes(value)), value)

    def test_pretty_json_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            parse_canonical(b'{\n  "a": 1\n}\n')

    def test_duplicate_field_rejected(self) -> None:
        with self.assertRaisesRegex(MigrationError, "duplicate"):
            parse_canonical(b'{"a":1,"a":2}\n')

    def test_float_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            parse_canonical(b'{"a":1.5}\n')

    def test_control_and_cr_rejected(self) -> None:
        for payload in (b'{"a":"\\u0000"}\n', b'{"a":1}\r\n', b'{"a":1}\0\n'):
            with self.subTest(payload=payload), self.assertRaises(MigrationError):
                parse_canonical(payload)


class SqlPolicyTest(unittest.TestCase):
    def test_fixture_actions_are_allowlisted(self) -> None:
        self.assertGreater(len(classify_sql((FIXTURE / "forward.pgsql").read_bytes(), "forward")), 5)
        self.assertEqual(len(classify_sql((FIXTURE / "rollback.pgsql").read_bytes(), "rollback")), 1)

    def test_hostile_sql_is_rejected(self) -> None:
        cases = [
            b"\\! id\n",
            b"COPY t TO PROGRAM 'id';\n",
            b"CREATE EXTENSION dblink;\n",
            b"CREATE ROLE attacker;\n",
            b"SELECT pg_read_file('/etc/passwd');\n",
            b"CREATE SERVER outside FOREIGN DATA WRAPPER postgres_fdw;\n",
            b"DO $$ BEGIN NULL; END $$;\n",
            b"INSERT INTO t VALUES (1);\n",
            b"TRUNCATE t;\n",
            b"BEGIN; CREATE TABLE t(x int); COMMIT;\n",
            b"DROP TABLE protected;\n",
            b"ALTER TABLE protected DISABLE ROW LEVEL SECURITY;\n",
            b"GRANT SELECT ON protected TO PUBLIC;\n",
            b"GRANT ALL ON protected TO reader;\n",
            b"GRANT SELECT, INSERT ON TABLE protected TO reader WITH GRANT OPTION;\n",
            b"GRANT SELECT, INSERT ON TABLE protected TO PUBLIC;\n",
            b"GRANT SELECT, INSERT ON TABLE protected TO reader INSERT;\n",
            b"GRANT SELECT ON TABLE protected TO reader; INSERT INTO t VALUES (1);\n",
            b"WITH changed AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM changed;\n",
            b"UPDATE t SET value = 1;\n",
            b"DELETE FROM t;\n",
            b"MERGE INTO t USING s ON false WHEN NOT MATCHED THEN INSERT VALUES (1);\n",
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(MigrationError):
                classify_sql(payload, "forward")

    def test_narrow_insert_table_privilege_is_allowlisted(self) -> None:
        statements = classify_sql(
            b"GRANT SELECT, INSERT ON TABLE protected TO reader;\n",
            "forward",
        )
        self.assertEqual(
            statements,
            ("GRANT SELECT, INSERT ON TABLE protected TO reader",),
        )

    def test_sql_requires_final_newline(self) -> None:
        with self.assertRaisesRegex(MigrationError, "final newline"):
            classify_sql(b"CREATE TABLE x(y int);", "forward")

    def test_unallowlisted_select_rejected(self) -> None:
        with self.assertRaisesRegex(MigrationError, "not allowlisted"):
            classify_sql(b"SELECT 1;\n", "forward")


class DependencyAndRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def copy_package(self, name: str, dependencies: list[str], ci_only: bool = True):
        destination = self.root / name
        shutil.copytree(FIXTURE, destination)
        value = json.loads((destination / "package.json").read_text())
        value["migration_id"] = name
        value["dependencies"] = dependencies
        value["ci_only"] = ci_only
        write_canonical(destination / "package.json", value)
        return load_package(destination, IDENTITY)

    def test_dependency_order_valid(self) -> None:
        first = self.copy_package("fixture_one", [])
        second = self.copy_package("fixture_two", ["fixture_one"])
        validate_dependency_graph([second, first])

    def test_duplicate_id_rejected(self) -> None:
        package = self.copy_package("fixture_one", [])
        with self.assertRaisesRegex(MigrationError, "duplicate"):
            validate_dependency_graph([package, package])

    def test_missing_dependency_rejected(self) -> None:
        package = self.copy_package("fixture_one", ["missing_one"])
        with self.assertRaisesRegex(MigrationError, "unknown"):
            validate_dependency_graph([package])

    def test_dependency_cycle_rejected(self) -> None:
        first = self.copy_package("fixture_one", ["fixture_two"])
        second = self.copy_package("fixture_two", ["fixture_one"])
        with self.assertRaisesRegex(MigrationError, "cycle"):
            validate_dependency_graph([first, second])

    def test_ci_and_production_dependency_crossing_rejected(self) -> None:
        first = self.copy_package("fixture_one", [], ci_only=False)
        second = self.copy_package("fixture_two", ["fixture_one"], ci_only=True)
        with self.assertRaisesRegex(MigrationError, "cannot cross"):
            validate_dependency_graph([first, second])

    def test_registry_contains_current_production_package(self) -> None:
        registry = load_registry(ROOT / "registry/governed-migrations-v1.json")
        package = load_package(
            ROOT.parent
            / "governed-migrations/lifeswitch_prior_answer_provenance_v1",
            IDENTITY,
        )
        self.assertEqual(
            tuple(registry),
            ("lifeswitch_prior_answer_provenance_v1",),
        )
        validate_registry([load_package(FIXTURE, IDENTITY), package], registry)

    def test_registry_hash_mismatch_rejected(self) -> None:
        package = self.copy_package("production_one", [], ci_only=False)
        registry = {
            "production_one": {
                "migration_id": "production_one",
                "package_sha256": "a" * 64,
                "lane": "schema_governance",
                "status": "active",
            }
        }
        with self.assertRaisesRegex(MigrationError, "reused"):
            validate_registry([package], registry)

    def test_registry_is_append_only(self) -> None:
        record = {
            "migration_id": "production_one",
            "package_sha256": "a" * 64,
            "lane": "schema_governance",
            "status": "active",
        }
        validate_registry_append_only({"production_one": record}, {"production_one": record, "production_two": {**record, "migration_id": "production_two"}})
        with self.assertRaisesRegex(MigrationError, "append-only"):
            validate_registry_append_only({"production_one": record}, {})
        with self.assertRaisesRegex(MigrationError, "record changed"):
            validate_registry_append_only({"production_one": record}, {"production_one": {**record, "package_sha256": "b" * 64}})


class ExternalDeployedSqlRegistryTest(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "governed-external-deployed-sql-registry-v1",
            "canonicalization": CANONICALIZATION,
            "closed": True,
            "records": copy.deepcopy(list(EXPECTED_EXTERNAL_DEPLOYED_SQL_V1)),
        }

    def parse(self, value: dict[str, object]) -> dict[str, dict[str, object]]:
        return parse_external_deployed_sql_registry(canonical_bytes(value))

    def test_exact_closed_registry_passes(self) -> None:
        records = self.parse(self.payload())
        self.assertEqual(
            set(records),
            {
                "ops/sql/20260806_chat_attachments_v1.sql",
                "ops/sql/20260806_chat_attachments_v1.rollback.sql",
            },
        )
        self.assertTrue(all(record["execution_authorized"] is False for record in records.values()))

    def test_modified_hash_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["sha256"] = "a" * 64
        with self.assertRaisesRegex(MigrationError, "authorized closed binding"):
            self.parse(value)

    def test_modified_deployment_evidence_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["deployment_evidence"]["event_sequence"] = 656
        with self.assertRaisesRegex(MigrationError, "authorized closed binding"):
            self.parse(value)

    def test_execution_authority_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["execution_authorized"] = True
        with self.assertRaisesRegex(MigrationError, "grants authority"):
            self.parse(value)

    def test_added_record_is_rejected(self) -> None:
        value = self.payload()
        extra = copy.deepcopy(value["records"][0])
        extra["record_id"] = "other_source_v1"
        extra["path"] = "ops/sql/other_source.sql"
        value["records"].append(extra)
        with self.assertRaisesRegex(MigrationError, "not closed"):
            self.parse(value)

    def test_closed_registry_is_immutable_after_creation(self) -> None:
        current = self.parse(self.payload())
        validate_external_deployed_sql_registry_append_only({}, current)
        changed = copy.deepcopy(current)
        changed["ops/sql/20260806_chat_attachments_v1.sql"]["status"] = "changed"
        with self.assertRaisesRegex(MigrationError, "registry changed"):
            validate_external_deployed_sql_registry_append_only(current, changed)


class LegacySqlPathAliasRegistryTest(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "governed-legacy-sql-path-alias-registry-v1",
            "canonicalization": CANONICALIZATION,
            "closed": True,
            "records": copy.deepcopy(list(EXPECTED_LEGACY_SQL_PATH_ALIASES_V1)),
        }

    def parse(self, value: dict[str, object]) -> dict[str, dict[str, object]]:
        return parse_legacy_sql_path_alias_registry(canonical_bytes(value))

    def test_exact_identity_only_registry_passes(self) -> None:
        records = self.parse(self.payload())
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["execution_authorized"] is False for record in records.values()))
        self.assertTrue(all(record["status"] == "identity_only" for record in records.values()))

    def test_changed_repository_path_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["repository_path"] = "ops/sql/other.sql"
        with self.assertRaisesRegex(MigrationError, "authorized closed binding"):
            self.parse(value)

    def test_changed_blob_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["git_blob"] = "a" * 40
        with self.assertRaisesRegex(MigrationError, "authorized closed binding"):
            self.parse(value)

    def test_execution_authority_is_rejected(self) -> None:
        value = self.payload()
        value["records"][0]["execution_authorized"] = True
        with self.assertRaisesRegex(MigrationError, "grants authority"):
            self.parse(value)

    def test_third_alias_is_rejected(self) -> None:
        value = self.payload()
        value["records"].append(copy.deepcopy(value["records"][0]))
        with self.assertRaisesRegex(MigrationError, "not closed"):
            self.parse(value)

    def test_closed_alias_registry_is_immutable_after_creation(self) -> None:
        current = self.parse(self.payload())
        validate_legacy_sql_path_alias_registry_append_only({}, current)
        changed = copy.deepcopy(current)
        first = next(iter(changed))
        changed[first]["status"] = "changed"
        with self.assertRaisesRegex(MigrationError, "registry changed"):
            validate_legacy_sql_path_alias_registry_append_only(current, changed)


class RepositoryInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = pathlib.Path(self.temporary.name) / "repository"
        self.repository.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", self.repository], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.repository, "config", "user.name", "CI"], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.repository, "config", "user.email", "ci@example.invalid"], check=True)
        (self.repository / "legacy.sql").write_text("CREATE TABLE legacy_anchor(id integer);\n")
        package_root = self.repository / "governed-migration-ci/fixtures" / FIXTURE.name
        package_root.parent.mkdir(parents=True)
        shutil.copytree(FIXTURE, package_root)
        subprocess.run(["/usr/bin/git", "-C", self.repository, "add", "."], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.repository, "commit", "-qm", "fixture"], check=True)
        oid = subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "rev-parse", "HEAD:legacy.sql"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.baseline = [{"path": "legacy.sql", "git_blob": oid, "sha256": sha256((self.repository / "legacy.sql").read_bytes())}]
        self.package = load_package(package_root, IDENTITY)

    def commit(self, name: str) -> None:
        subprocess.run(["/usr/bin/git", "-C", self.repository, "add", "."], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.repository, "commit", "-qm", name], check=True)

    def add_external(self) -> dict[str, dict[str, object]]:
        path = "ops/sql/adopted.sql"
        source = self.repository / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("CREATE TABLE adopted_anchor(id integer);\n")
        self.commit("adopted external source")
        commit = subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        blob = subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "rev-parse", "HEAD:" + path],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return {
            path: {
                "path": path,
                "git_blob": blob,
                "sha256": sha256(source.read_bytes()),
                "source_projection_sha256": "a" * 64,
                "repository_commit": commit,
            }
        }

    def alias_legacy(self) -> dict[str, dict[str, object]]:
        ledger_path = "legacy.sql"
        repository_path = "renamed_legacy.sql"
        subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "mv", ledger_path, repository_path],
            check=True,
        )
        self.commit("identity-only legacy path alias")
        commit = subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return {
            ledger_path: {
                "ledger_path": ledger_path,
                "repository_path": repository_path,
                "git_blob": self.baseline[0]["git_blob"],
                "sha256": self.baseline[0]["sha256"],
                "ledger_source_projection_sha256": sha256(canonical_bytes(self.baseline[0])),
                "repository_commit": commit,
            }
        }

    def test_exact_inventory_passes(self) -> None:
        report = validate_repository_inventory(self.repository, "HEAD", self.baseline, [self.package])
        self.assertEqual(report["legacy_sql_count"], 1)
        self.assertEqual(report["governed_sql_count"], 2)

    def test_new_legacy_sql_rejected(self) -> None:
        (self.repository / "unregistered.sql").write_text("CREATE TABLE nope(id integer);\n")
        self.commit("unregistered")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(self.repository, "HEAD", self.baseline, [self.package])

    def test_unregistered_pgsql_rejected(self) -> None:
        (self.repository / "unregistered.pgsql").write_text("CREATE TABLE nope(id integer);\n")
        self.commit("unregistered")
        with self.assertRaisesRegex(MigrationError, "governed SQL"):
            validate_repository_inventory(self.repository, "HEAD", self.baseline, [self.package])

    def test_altered_legacy_bytes_rejected(self) -> None:
        (self.repository / "legacy.sql").write_text("CREATE TABLE legacy_anchor(id text);\n")
        self.commit("altered")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(self.repository, "HEAD", self.baseline, [self.package])

    def test_exact_external_deployed_source_passes_without_changing_baseline(self) -> None:
        external = self.add_external()
        report = validate_repository_inventory(
            self.repository,
            "HEAD",
            self.baseline,
            [self.package],
            external_deployed_sources=external,
        )
        self.assertEqual(report["legacy_baseline_sql_count"], 1)
        self.assertEqual(report["external_deployed_sql_count"], 1)
        self.assertEqual(report["legacy_sql_count"], 2)
        self.assertFalse(report["external_deployed_sql_execution_authorized"])
        binding = validate_external_deployed_sql_commit_bindings(
            self.repository,
            "HEAD",
            external,
        )
        self.assertEqual(binding["status"], "bound")

    def test_modified_external_deployed_source_is_rejected(self) -> None:
        external = self.add_external()
        (self.repository / "ops/sql/adopted.sql").write_text(
            "CREATE TABLE adopted_anchor(id text);\n"
        )
        self.commit("altered adopted source")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(
                self.repository,
                "HEAD",
                self.baseline,
                [self.package],
                external_deployed_sources=external,
            )

    def test_reordered_current_legacy_projection_is_rejected(self) -> None:
        external = self.add_external()
        external_path = "ops/sql/adopted.sql"
        external_projection = {
            "path": external_path,
            "git_blob": external[external_path]["git_blob"],
            "sha256": external[external_path]["sha256"],
        }
        external[external_path]["source_projection_sha256"] = sha256(
            canonical_bytes(external_projection)
        )
        projected = sorted(
            [self.baseline[0], external_projection],
            key=lambda item: str(item["path"]),
        )
        report = validate_repository_inventory(
            self.repository,
            "HEAD",
            self.baseline,
            [self.package],
            current_legacy_sources=projected,
            external_deployed_sources=external,
        )
        self.assertEqual(report["legacy_sql_count"], 2)
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(
                self.repository,
                "HEAD",
                self.baseline,
                [self.package],
                current_legacy_sources=reversed(projected),
                external_deployed_sources=external,
            )

    def test_additional_sql_is_rejected_with_external_registry(self) -> None:
        external = self.add_external()
        (self.repository / "ops/sql/unregistered.sql").write_text(
            "CREATE TABLE unregistered_anchor(id integer);\n"
        )
        self.commit("unregistered after adopted source")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(
                self.repository,
                "HEAD",
                self.baseline,
                [self.package],
                external_deployed_sources=external,
            )

    def test_adopted_commit_must_be_ancestor(self) -> None:
        external = self.add_external()
        subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "commit", "--allow-empty", "-qm", "descendant"],
            check=True,
        )
        orphan = subprocess.run(
            ["/usr/bin/git", "-C", self.repository, "commit-tree", "HEAD^{tree}", "-m", "orphan"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        changed = copy.deepcopy(external)
        changed["ops/sql/adopted.sql"]["repository_commit"] = orphan
        with self.assertRaises(MigrationError):
            validate_external_deployed_sql_commit_bindings(
                self.repository,
                "HEAD",
                changed,
            )

    def test_exact_legacy_path_alias_passes_without_changing_baseline(self) -> None:
        aliases = self.alias_legacy()
        report = validate_repository_inventory(
            self.repository,
            "HEAD",
            self.baseline,
            [self.package],
            legacy_path_aliases=aliases,
        )
        self.assertEqual(report["legacy_baseline_sql_count"], 1)
        self.assertEqual(report["legacy_path_alias_count"], 1)
        self.assertEqual(report["legacy_sql_count"], 1)
        self.assertFalse(report["legacy_path_alias_execution_authorized"])
        binding = validate_legacy_sql_path_alias_commit_bindings(
            self.repository,
            "HEAD",
            aliases,
        )
        self.assertEqual(binding["status"], "identity_only_bound")

    def test_legacy_alias_with_changed_bytes_is_rejected(self) -> None:
        aliases = self.alias_legacy()
        (self.repository / "renamed_legacy.sql").write_text(
            "CREATE TABLE legacy_anchor(id text);\n"
        )
        self.commit("changed aliased bytes")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(
                self.repository,
                "HEAD",
                self.baseline,
                [self.package],
                legacy_path_aliases=aliases,
            )

    def test_legacy_alias_does_not_allow_another_sql_source(self) -> None:
        aliases = self.alias_legacy()
        (self.repository / "additional.sql").write_text(
            "CREATE TABLE additional_anchor(id integer);\n"
        )
        self.commit("additional source beside alias")
        with self.assertRaisesRegex(MigrationError, "legacy SQL"):
            validate_repository_inventory(
                self.repository,
                "HEAD",
                self.baseline,
                [self.package],
                legacy_path_aliases=aliases,
            )


if __name__ == "__main__":
    unittest.main()
