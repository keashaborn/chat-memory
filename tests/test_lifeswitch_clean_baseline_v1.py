from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT.parent / "scripts" / "build_lifeswitch_clean_baseline_v1.py"
if not SCRIPT.is_file():
    SCRIPT = ROOT / "build_lifeswitch_clean_baseline_v1.py"
SPEC = importlib.util.spec_from_file_location("clean_baseline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def disposition_fixture() -> dict[str, object]:
    objects = []
    for identity in sorted(module.EXCLUDED_RELATIONS):
        objects.append(
            {
                "baseline_action": "exclude",
                "disposition": "archive_candidate",
                "identity": identity,
                "object_type": "relation",
            }
        )
    for identity in sorted(module.EXCLUDED_FUNCTIONS):
        objects.append(
            {
                "baseline_action": "exclude",
                "disposition": "archive_candidate",
                "identity": identity,
                "object_type": "function",
            }
        )
    for identity in sorted(module.CANONICAL_MUSCLE_RELATIONS):
        objects.append(
            {
                "baseline_action": "include",
                "disposition": "retained_canonical_product_data",
                "identity": identity,
                "object_type": "relation",
            }
        )
    return {
        "baseline_generation_allowed": True,
        "deletion_authority": False,
        "object_count": len(objects),
        "objects": objects,
        "production_change_authority": False,
        "schema_version": module.DISPOSITION_VERSION,
        "status": "candidate_baseline_ready",
    }


class LifeSwitchCleanBaselineV1Tests(unittest.TestCase):
    def test_exact_disposition_builds_supported_plan(self):
        plan = module.validate_disposition(disposition_fixture())
        self.assertEqual(plan["excluded_relations"], sorted(module.EXCLUDED_RELATIONS))
        self.assertEqual(plan["excluded_functions"], sorted(module.EXCLUDED_FUNCTIONS))
        self.assertEqual(
            plan["retained_canonical_product_data"],
            sorted(module.CANONICAL_MUSCLE_RELATIONS),
        )

    def test_hold_or_changed_exclusion_fails_closed(self):
        manifest = disposition_fixture()
        manifest["objects"][0]["baseline_action"] = "hold"
        with self.assertRaisesRegex(module.BaselineContractError, "hold_present"):
            module.validate_disposition(manifest)

        manifest = disposition_fixture()
        manifest["objects"][0]["identity"] = "catalog_dev.unknown"
        with self.assertRaisesRegex(module.BaselineContractError, "relations_changed"):
            module.validate_disposition(manifest)

    def test_disposition_hash_is_exact(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "disposition.json"
            path.write_text(json.dumps(disposition_fixture()), encoding="utf-8")
            with self.assertRaisesRegex(module.BaselineContractError, "sha256_mismatch"):
                module.read_disposition(path, "0" * 64)
            exact = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(module.read_disposition(path, exact)["status"], "candidate_baseline_ready")

    def test_dump_is_schema_only_and_excludes_exact_relations(self):
        plan = module.validate_disposition(disposition_fixture())
        command = module.dump_command(plan)
        joined = " ".join(command)
        self.assertIn("--schema-only", command)
        self.assertIn("--exclude-schema=lifeswitch_snapshot", command)
        for identity in sorted(module.EXCLUDED_RELATIONS):
            if identity.startswith("lifeswitch_snapshot."):
                continue
            self.assertIn("--exclude-table=" + identity, command)
        self.assertNotIn("--data-only", joined)

    def test_restore_list_removes_only_retired_function_entries(self):
        raw = "\n".join(
            [
                "; header",
                "1; 1259 1 TABLE catalog_dev muscle lifeswitch_owner",
                "2; 1259 2 TABLE catalog_dev muscle_alias lifeswitch_owner",
                "3; 1259 3 TABLE catalog_dev exercise_muscle lifeswitch_owner",
                "4; 1255 4 FUNCTION catalog_dev search_foods(text, integer, text) lifeswitch_owner",
                "5; 0 0 ACL catalog_dev FUNCTION search_foods(q text, max_results integer, p_locale text) lifeswitch_owner",
                "6; 1255 5 FUNCTION catalog_dev search_exercises(text, integer, text) lifeswitch_owner",
            ]
        )
        filtered, removed = module.filter_restore_list(raw)
        self.assertEqual(removed, 2)
        self.assertNotIn("search_foods", filtered)
        self.assertIn("search_exercises", filtered)

    def test_restore_list_rejects_data_or_missing_canonical_table(self):
        with self.assertRaisesRegex(module.BaselineContractError, "table_data"):
            module.filter_restore_list("1; 0 0 TABLE DATA catalog_dev muscle owner")
        raw = "\n".join(
            [
                "1; 1259 1 TABLE catalog_dev muscle lifeswitch_owner",
                "2; 1259 2 TABLE catalog_dev muscle_alias lifeswitch_owner",
                "4; 1255 4 FUNCTION catalog_dev search_foods(text, integer, text) lifeswitch_owner",
                "5; 0 0 ACL catalog_dev FUNCTION search_foods(q text, max_results integer, p_locale text) lifeswitch_owner",
            ]
        )
        with self.assertRaisesRegex(module.BaselineContractError, "canonical_muscle"):
            module.filter_restore_list(raw)

    def test_role_bootstrap_contains_no_password_or_login_authority(self):
        upper = module.ROLE_SQL.upper()
        self.assertNotIn("PASSWORD", upper)
        self.assertEqual(upper.count("CREATE ROLE"), 7)
        self.assertEqual(upper.count("NOLOGIN"), 7)
        self.assertIn("GRANT LIFESWITCH_APP TO LIFESWITCH_APP_LOGIN", upper)
        self.assertIn("GRANT LIFESWITCH_CHAT_READER TO LIFESWITCH_CHAT_LOGIN", upper)

    def test_plain_schema_validation_allows_function_dml_and_similar_names(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "schema.sql"
            path.write_text(
                "CREATE TABLE catalog_dev.exercise_muscle ();\n"
                "CREATE TABLE catalog_dev.muscle ();\n"
                "CREATE TABLE catalog_dev.muscle_alias ();\n"
                "CREATE FUNCTION retained_writer() RETURNS void AS $$\n"
                "BEGIN\n"
                "  INSERT INTO retained_table VALUES (1);\n"
                "END\n"
                "$$ LANGUAGE plpgsql;\n"
                "ALTER TABLE retained_table ADD CONSTRAINT ck_my_food_nutrient_source_nonempty CHECK (true);\n",
                encoding="utf-8",
            )
            module.validate_plain_schema(path)

    def test_plain_schema_validation_rejects_copy_and_exact_retired_objects(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "schema.sql"
            canonical = (
                "CREATE TABLE catalog_dev.exercise_muscle ();\n"
                "CREATE TABLE catalog_dev.muscle ();\n"
                "CREATE TABLE catalog_dev.muscle_alias ();\n"
            )
            path.write_text(canonical + "COPY catalog_dev.muscle FROM stdin;\n", encoding="utf-8")
            with self.assertRaisesRegex(module.BaselineContractError, "forbidden_content"):
                module.validate_plain_schema(path)
            path.write_text(canonical + "ALTER TABLE catalog_dev.food_nutrient OWNER TO lifeswitch_owner;\n")
            with self.assertRaisesRegex(module.BaselineContractError, "forbidden_content"):
                module.validate_plain_schema(path)


if __name__ == "__main__":
    unittest.main()
