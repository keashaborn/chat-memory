from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_lifeswitch_disposable_restore_v1.py"
SPEC = importlib.util.spec_from_file_location("disposable_restore", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class LifeSwitchDisposableRestoreV1Tests(unittest.TestCase):
    def test_run_id_and_target_database_are_bounded(self) -> None:
        self.assertEqual(
            module.target_database("20260822t012500z"),
            "lifeswitch_verify_20260822t012500z",
        )
        for invalid in ("short", "../escape", "UPPERCASE01", "a" * 41):
            with self.subTest(invalid=invalid):
                with self.assertRaises(module.VerificationContractError):
                    module.target_database(invalid)

    def test_identifier_and_literal_quoting_are_total(self) -> None:
        self.assertEqual(module.quote_identifier('a"b'), '"a""b"')
        self.assertEqual(module.quote_literal("a'b"), "'a''b'")
        with self.assertRaises(module.VerificationContractError):
            module.quote_identifier("bad\x00name")

    def test_docker_commands_never_use_a_shell(self) -> None:
        command = module.docker_command("psql", "-c", "select 1")
        self.assertEqual(command[:3], ["/usr/bin/docker", "exec", module.CONTAINER])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_relation_name_requires_exact_schema_and_relation(self) -> None:
        self.assertEqual(module.relation_sql("a.b"), '"a"."b"')
        for invalid in ("a", "a.b.c", ""):
            with self.assertRaises(module.VerificationContractError):
                module.relation_sql(invalid)

    def test_owner_surfaces_are_unique_and_cover_all_domain_schemas(self) -> None:
        names = [surface.relation for surface in module.SURFACES]
        self.assertEqual(len(names), 33)
        self.assertEqual(len(names), len(set(names)))
        for prefix in (
            "lifeswitch_chat.",
            "lifeswitch_nutrition.",
            "lifeswitch_plan.",
            "lifeswitch_training.",
            "public.",
        ):
            self.assertTrue(any(name.startswith(prefix) for name in names), prefix)

    def test_app_visible_query_binds_both_owner_gucs_and_app_role(self) -> None:
        captured: list[str] = []

        def fake_psql(_database: str, sql: str, *, label: str) -> str:
            self.assertEqual(label, "app_visible_count")
            captured.append(sql)
            return "7"

        with patch.object(module, "psql", side_effect=fake_psql):
            actual = module.app_visible_count(
                "clone", "11111111-1111-4111-8111-111111111111", "a.b"
            )
        self.assertEqual(actual, 7)
        self.assertIn('set local role "lifeswitch_app_login"', captured[0])
        self.assertIn("set local app.user_id=", captured[0])
        self.assertIn("set local app.lifeswitch_owner_id=", captured[0])
        self.assertTrue(captured[0].endswith("rollback;"))

    def test_nonsecret_config_requires_fail_closed_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "settings.env"
            path.write_text(
                "LIFESWITCH_DELEGATED_READS_ENABLED=0\n"
                "LIFESWITCH_PEOPLE_ENABLED=0\n"
                "SECRET=value-not-read\n",
                encoding="utf-8",
            )
            with patch.object(module, "CONFIG_PATH", path):
                self.assertEqual(
                    module.read_nonsecret_config(),
                    {
                        "delegated_reads_enabled": False,
                        "people_dependency_enabled": False,
                    },
                )
            path.write_text("LIFESWITCH_DELEGATED_READS_ENABLED=1\n", encoding="utf-8")
            with patch.object(module, "CONFIG_PATH", path):
                with self.assertRaises(module.VerificationExecutionError):
                    module.read_nonsecret_config()

    def test_manifest_hash_is_content_deterministic(self) -> None:
        first = module.sha256_bytes(module.canonical_bytes({"b": 2, "a": 1}))
        second = module.sha256_bytes(module.canonical_bytes({"a": 1, "b": 2}))
        self.assertEqual(first, second)

    def test_manifest_difference_is_content_free_and_object_bound(self) -> None:
        base = {
            "schemas": [{"name": "public", "owner": "one"}],
            "relations": [],
            "functions": [],
            "policies": [],
            "extensions": [],
            "table_counts": {"public.example": 1},
        }
        restored = {
            **base,
            "schemas": [{"name": "public", "owner": "two"}],
            "table_counts": {"public.example": 2},
        }
        difference = module.manifest_difference(base, restored)
        self.assertEqual(difference["schemas"][0]["identity"], ["public"])
        self.assertEqual(difference["schemas"][0]["changed_fields"], ["owner"])
        self.assertEqual(
            difference["table_counts"]["public.example"],
            {"source": 1, "restored": 2},
        )
        rendered = json.dumps(difference)
        self.assertNotIn('"one"', rendered)
        self.assertNotIn('"two"', rendered)

    def test_main_failure_is_content_free(self) -> None:
        with patch.object(
            module,
            "execute",
            side_effect=module.VerificationExecutionError("pg_restore_failed"),
        ), patch("builtins.print") as output:
            status = module.main(
                ["--run-id", "20260822t012500z", "--candidate-commit", "a" * 40]
            )
        self.assertEqual(status, 1)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(rendered, {"error": "pg_restore_failed", "status": "failed"})


    def test_legacy_live_only_verifier_is_retired(self) -> None:
        self.assertFalse(
            (ROOT / "scripts" / "verify_lifeswitch_isolated_postgres_v1.py").exists()
        )


if __name__ == "__main__":
    unittest.main()
