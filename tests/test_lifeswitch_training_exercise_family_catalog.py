from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "ops"
    / "sql"
    / "20260727_lifeswitch_training_exercise_family_v1.sql"
)
ROLLBACK = (
    ROOT
    / "ops"
    / "sql"
    / "20260727_lifeswitch_training_exercise_family_v1_rollback.sql"
)
ROUTER = ROOT / "rag_engine" / "catalog_router.py"


class TrainingExerciseFamilyCatalogContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.rollback = ROLLBACK.read_text(encoding="utf-8").lower()
        router_source = ROUTER.read_text(encoding="utf-8")
        match = re.search(
            r'@router\.get\("/exercises/browse"\).*?(?=\n@router\.)',
            router_source,
            flags=re.DOTALL,
        )
        if match is None:
            raise AssertionError("browse_exercises route not found")
        cls.route = match.group(0).lower()

    def test_migration_is_additive_to_existing_training_data(self) -> None:
        self.assertIn(
            "create table if not exists catalog_dev.exercise_family",
            self.sql,
        )
        self.assertIn(
            "create table if not exists catalog_dev.exercise_family_member",
            self.sql,
        )
        for forbidden in (
            "delete from",
            "truncate ",
            "drop table lifeswitch_training.",
            "alter table lifeswitch_training.",
            "alter table catalog_dev.exercise ",
            "update catalog_dev.exercise ",
        ):
            self.assertNotIn(forbidden, self.sql)

    def test_family_members_reference_existing_catalog_exercises(self) -> None:
        self.assertIn(
            "references catalog_dev.exercise(exercise_id)",
            self.sql,
        )
        self.assertIn("on delete restrict", self.sql)
        self.assertIn("exercise family seed references missing rows", self.sql)

    def test_initial_browse_taxonomy_contains_core_families(self) -> None:
        for family_slug in (
            "bench_press",
            "row",
            "pull_up",
            "shoulder_press",
            "squat",
            "split_squat_lunge",
            "deadlift",
            "hip_thrust_bridge",
            "core_stability",
        ):
            self.assertIn(f"('{family_slug}',", self.sql)

    def test_browse_route_is_read_only_and_public_catalog_bounded(self) -> None:
        self.assertIn('f.is_active=true', self.route)
        self.assertIn('e.is_active=true', self.route)
        self.assertIn('e.is_public=true', self.route)
        self.assertIn('"variants": []', self.route)
        self.assertNotIn("owner_user_id", self.route)
        self.assertNotIn("insert into", self.route)
        self.assertNotIn("update ", self.route)
        self.assertNotIn("delete from", self.route)

    def test_rollback_only_removes_new_taxonomy_tables(self) -> None:
        self.assertIn(
            "drop table if exists catalog_dev.exercise_family_member",
            self.rollback,
        )
        self.assertIn(
            "drop table if exists catalog_dev.exercise_family",
            self.rollback,
        )
        self.assertNotIn("lifeswitch_training.", self.rollback)
        self.assertNotIn("catalog_dev.exercise;", self.rollback)


if __name__ == "__main__":
    unittest.main()
