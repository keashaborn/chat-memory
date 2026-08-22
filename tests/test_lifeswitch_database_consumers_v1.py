from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "database_consumer_audit_v1.py"
SPEC = importlib.util.spec_from_file_location("database_consumers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def relation(oid: int, name: str, *, extension: str = "") -> module.DatabaseObject:
    return module.DatabaseObject(oid, "relation", "lifeswitch_plan", name, extension=extension)


def function(oid: int, name: str) -> module.DatabaseObject:
    return module.DatabaseObject(oid, "function", "lifeswitch_plan", name, "uuid")


class LifeSwitchDatabaseConsumersV1Tests(unittest.TestCase):
    def test_contract_values_are_strict(self) -> None:
        self.assertEqual(module.validate_run_id("20260821t120000z"), "20260821t120000z")
        self.assertEqual(module.validate_commit("a" * 40), "a" * 40)
        self.assertEqual(module.validate_manifest_sha256("b" * 64), "b" * 64)
        for invalid in ("short", "../escape", "UPPERCASE01"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(module.AuditContractError):
                    module.validate_run_id(invalid)

    def test_parse_objects_rejects_duplicate_identity_and_unknown_schema(self) -> None:
        row = {
            "oid": 1,
            "object_type": "relation",
            "schema": "lifeswitch_plan",
            "name": "plan_profile",
        }
        with self.assertRaises(module.AuditExecutionError):
            module.parse_objects([row, {**row, "oid": 2}])
        with self.assertRaises(module.AuditExecutionError):
            module.parse_objects([{**row, "schema": "unknown"}])

    def test_source_scan_is_qualified_content_free_and_hash_bound(self) -> None:
        objects = [relation(1, "plan_profile"), relation(2, "plan_comment")]
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / "seebx").mkdir()
            (repository / "seebx" / "adapter.py").write_text(
                "SQL = 'select * from lifeswitch_plan.plan_profile'\n"
                "UNQUALIFIED = 'plan_comment'\n",
                encoding="utf-8",
            )
            result = module.scan_sources(repository, (Path("seebx"),), objects)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(
            result["matches"],
            {"lifeswitch_plan.plan_profile": ["seebx/adapter.py"]},
        )
        self.assertRegex(result["tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("select", json.dumps(result))

    def test_source_scan_maps_only_explicit_canonical_identity_aliases(self) -> None:
        objects = [relation(1, "plan_profile"), relation(2, "plan_comment")]
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / "seebx").mkdir()
            (repository / "seebx" / "adapter.py").write_text(
                "SQL = 'select * from plan.plan_profile'\n",
                encoding="utf-8",
            )
            result = module.scan_sources(
                repository,
                (Path("seebx"),),
                objects,
                identity_aliases=(("lifeswitch_plan.plan_profile", "plan.plan_profile"),),
            )
        self.assertEqual(
            result["matches"],
            {"lifeswitch_plan.plan_profile": ["seebx/adapter.py"]},
        )
        with self.assertRaisesRegex(
            module.AuditContractError,
            "runtime_identity_alias_invalid",
        ):
            module.scan_sources(
                Path("/tmp"),
                (),
                objects,
                identity_aliases=(("lifeswitch_plan.unknown", "plan.unknown"),),
            )

    def test_source_scan_recognizes_only_schema_bound_templates(self) -> None:
        objects = [
            module.DatabaseObject(
                2,
                "function",
                "lifeswitch_training",
                "create_training_session",
                "uuid",
            ),
            module.DatabaseObject(
                3,
                "function",
                "lifeswitch_chat",
                "read_measurement_observations_v1",
                "uuid, date, date",
            ),
        ]
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / "seebx").mkdir()
            (repository / "seebx" / "training.py").write_text(
                "DEFAULT = 'lifeswitch_plan'\n"
                "SQL = f'select {schema}.create_training_session()'\n",
                encoding="utf-8",
            )
            (repository / "seebx" / "bound.py").write_text(
                "DEFAULT = 'lifeswitch_plan'\n"
                "SQL = f'select {schema}.create_training_session()'\n"
                "TRAINING = 'lifeswitch_training'\n",
                encoding="utf-8",
            )
            (repository / "seebx" / "selector.py").write_text(
                "function = 'read_measurement_observations_v1'\n"
                "SQL = f'select * from lifeswitch_chat.{function}()'\n",
                encoding="utf-8",
            )
            result = module.scan_sources(repository, (Path("seebx"),), objects)
        self.assertEqual(
            result["matches"]["lifeswitch_training.create_training_session(uuid)"],
            ["seebx/bound.py"],
        )
        self.assertEqual(
            result["matches"][
                "lifeswitch_chat.read_measurement_observations_v1(uuid, date, date)"
            ],
            ["seebx/selector.py"],
        )

    def test_source_scan_excludes_other_database_adapters_exactly(self) -> None:
        objects = [relation(1, "plan_profile")]
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / "seebx").mkdir()
            (repository / "seebx" / "platform.py").write_text(
                "SQL = 'select * from lifeswitch_plan.plan_profile'\n",
                encoding="utf-8",
            )
            (repository / "seebx" / "other_database.py").write_text(
                "SQL = 'select * from lifeswitch_plan.plan_profile'\n",
                encoding="utf-8",
            )
            result = module.scan_sources(
                repository,
                (Path("seebx"),),
                objects,
                (Path("seebx/other_database.py"),),
            )
        self.assertEqual(
            result["matches"],
            {"lifeswitch_plan.plan_profile": ["seebx/platform.py"]},
        )

    def test_definition_and_explicit_edges_use_object_identities(self) -> None:
        objects = [relation(1, "plan_profile"), function(2, "enforce_owner")]
        definitions = [{
            "consumer_type": "function",
            "consumer_oid": 2,
            "definition": "select * from lifeswitch_plan.plan_profile",
        }]
        self.assertEqual(
            module.definition_edges(objects, definitions),
            [{
                "consumer": "lifeswitch_plan.enforce_owner(uuid)",
                "referenced": "lifeswitch_plan.plan_profile",
                "evidence": "definition",
            }],
        )
        self.assertEqual(
            module.explicit_edges(objects, [{
                "consumer_type": "relation",
                "consumer_oid": 1,
                "referenced_type": "function",
                "referenced_oid": 2,
                "evidence": "trigger",
            }]),
            [{
                "consumer": "lifeswitch_plan.plan_profile",
                "referenced": "lifeswitch_plan.enforce_owner(uuid)",
                "evidence": "trigger",
            }],
        )

    def test_classification_propagates_only_from_verified_seeds(self) -> None:
        direct = relation(1, "plan_profile")
        helper = function(2, "enforce_owner")
        migration = relation(3, "plan_history")
        unknown = relation(4, "abandoned")
        extension = relation(5, "extension_table", extension="example")
        operational = relation(6, "verification_evidence")
        classified = module.classify_objects(
            [direct, helper, migration, unknown, extension, operational],
            {direct.identity: ["seebx/adapters/plan.py"]},
            {
                helper.identity: ["scripts/verify.py"],
                operational.identity: ["scripts/verify.py"],
            },
            {migration.identity: ["ops/sql/old.sql"]},
            [{"consumer": direct.identity, "referenced": helper.identity, "evidence": "trigger"}],
        )
        actual = {item["identity"]: item["classification"] for item in classified}
        self.assertEqual(actual[direct.identity], "application_direct")
        self.assertEqual(actual[helper.identity], "database_internal_reachable")
        self.assertEqual(actual[migration.identity], "migration_only")
        self.assertEqual(actual[unknown.identity], "unproven")
        self.assertEqual(actual[extension.identity], "extension_owned")
        self.assertEqual(actual[operational.identity], "operational_reference_only")

    def test_psql_is_read_only_and_never_uses_a_shell(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("repeatable read read only", source)
        self.assertIn("safe.directory=", source)
        self.assertIn("source_manifest_sha256_mismatch", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_repository_state_requires_exact_commit_and_clean_worktree(self) -> None:
        repository = Path("/tmp/candidate")
        with patch.object(module, "run_text", side_effect=["a" * 40, ""]) as run:
            module.verify_repository_state(repository, "a" * 40)
        self.assertEqual(run.call_count, 2)
        self.assertIn("--porcelain=v1", run.call_args_list[1].args[0])
        with patch.object(module, "run_text", side_effect=["a" * 40, " M app.py"]):
            with self.assertRaisesRegex(
                module.AuditContractError,
                "candidate_worktree_not_clean",
            ):
                module.verify_repository_state(repository, "a" * 40)

    def test_repository_state_rejects_commit_mismatch_before_status(self) -> None:
        with patch.object(module, "run_text", return_value="b" * 40) as run:
            with self.assertRaisesRegex(
                module.AuditContractError,
                "candidate_commit_mismatch",
            ):
                module.verify_repository_state(Path("/tmp/candidate"), "a" * 40)
        run.assert_called_once()

    def test_source_manifest_hash_is_collected_from_existing_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            verifier = Path(raw) / "verifier.py"
            verifier.write_text("# fixture\n", encoding="utf-8")
            with patch.object(module.runpy, "run_path", return_value={
                "collect_manifest": lambda _database: {"manifest_sha256": "c" * 64}
            }):
                self.assertEqual(
                    module.current_source_manifest_sha256(verifier), "c" * 64
                )

    def test_governance_manifest_hashes_definitions_and_preserves_controls(self) -> None:
        fixtures = {
            "governance_schemas": [{"name": "lifeswitch_plan", "owner": "owner", "acl": []}],
            "governance_relations": [{
                "schema": "lifeswitch_plan",
                "name": "plan_profile",
                "kind": "r",
                "owner": "owner",
                "rls_enabled": True,
                "rls_forced": True,
                "definition": "secret relation definition",
            }],
            "governance_columns": [{
                "schema": "lifeswitch_plan",
                "relation": "plan_profile",
                "name": "id",
                "default_definition": "gen_random_uuid()",
            }],
            "governance_functions": [{"name": "protect", "definition": "function body"}],
            "governance_policies": [{
                "name": "owner_policy",
                "using_definition": "owner_user_id = current_user",
                "check_definition": "owner_user_id = current_user",
            }],
            "governance_triggers": [{"name": "protect_history", "definition": "trigger body"}],
            "governance_constraints": [{"name": "owner_required", "definition": "not null"}],
            "governance_indexes": [{"name": "plan_profile_pkey", "definition": "create index"}],
            "governance_extensions": [{"name": "pgcrypto", "version": "1.3"}],
            "governance_roles": [{"name": "lifeswitch_app", "bypass_rls": False}],
            "governance_memberships": [{"member": "lifeswitch_app", "granted": "reader"}],
            "governance_sequences": [{"schema": "lifeswitch_plan", "name": "example_seq", "last_value": 7}],
        }

        def fake_psql_json(_sql: str, *, label: str, spec: object) -> list[dict]:
            del spec
            return fixtures[label]

        with patch.object(module, "psql_json", side_effect=fake_psql_json), patch.object(
            module,
            "collect_exact_table_counts",
            return_value=[{"schema": "lifeswitch_plan", "relation": "plan_profile", "row_count": 3}],
        ):
            manifest = module.collect_governance_manifest()
        encoded = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("secret relation definition", encoded)
        self.assertNotIn("function body", encoded)
        self.assertNotIn("owner_user_id = current_user", encoded)
        self.assertTrue(manifest["sections"]["relations"][0]["rls_enabled"])
        self.assertTrue(manifest["sections"]["relations"][0]["rls_forced"])
        self.assertEqual(manifest["sections"]["table_counts"][0]["row_count"], 3)
        self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            manifest["sections"]["functions"][0]["definition_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_exact_table_counts_quote_identifiers_and_reject_bad_counts(self) -> None:
        relations = [{"schema": "lifeswitch_plan", "name": "plan_profile", "kind": "r"}]
        with patch.object(module, "psql_scalar", return_value="12") as scalar:
            counts = module.collect_exact_table_counts(relations)
        self.assertEqual(counts[0]["row_count"], 12)
        self.assertIn('"lifeswitch_plan"."plan_profile"', scalar.call_args.args[0])
        with patch.object(module, "psql_scalar", return_value="not-a-count"):
            with self.assertRaises(module.AuditExecutionError):
                module.collect_exact_table_counts(relations)

    def test_main_failure_is_content_free(self) -> None:
        with patch.object(
            module,
            "execute",
            side_effect=module.AuditExecutionError("objects_failed"),
        ), patch("builtins.print") as output:
            status = module.main([
                "--repository", "/tmp/repo",
                "--run-id", "20260821t120000z",
                "--candidate-commit", "a" * 40,
                "--source-manifest-sha256", "b" * 64,
            ])
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(output.call_args.args[0]),
            {"error": "objects_failed", "status": "failed"},
        )


if __name__ == "__main__":
    unittest.main()
