from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "ops" / "sql"
EXPAND = SQL_DIR / "20260721_lifeswitch_training_observation_expand.sql"


class TrainingObservationIntegrityMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expand = EXPAND.read_text(encoding="utf-8")
        cls.expand_lower = cls.expand.lower()

    def test_expand_is_isolated_from_memory_and_nutrition(self) -> None:
        executable = "\n".join(
            line
            for line in self.expand_lower.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotRegex(executable, r"\bmemory\.")
        self.assertNotIn("qdrant", executable)
        self.assertNotIn("lifeswitch_nutrition.", executable)
        self.assertIn("lifeswitch_training.training_session", executable)
        self.assertIn("lifeswitch_training.conditioning_session_log", executable)

    def test_top_level_observations_receive_provenance_and_correction_fields(
        self,
    ) -> None:
        for table in ("training_session", "conditioning_session_log"):
            with self.subTest(table=table):
                table_block = self.expand_lower.split(
                    f"alter table lifeswitch_training.{table}", 1
                )[1].split(";", 1)[0]
                for column in (
                    "recorded_by_user_id",
                    "idempotency_key",
                    "source_snapshot",
                    "snapshot_schema_version",
                    "snapshot_quality",
                    "voided_at",
                    "voided_by_user_id",
                    "void_reason",
                ):
                    self.assertIn(column, table_block)

        self.assertIn("supersedes_training_session_id", self.expand_lower)
        self.assertIn("supersedes_conditioning_session_id", self.expand_lower)

    def test_strength_children_receive_explicit_capture_contract(self) -> None:
        set_block = self.expand_lower.split(
            "alter table lifeswitch_training.training_set_log", 1
        )[1].split(";", 1)[0]
        for column in (
            "capture_role",
            "load_unit",
            "source_snapshot",
            "snapshot_schema_version",
            "snapshot_quality",
        ):
            self.assertIn(column, set_block)

        segment_block = self.expand_lower.split(
            "alter table lifeswitch_training.training_set_log_segment", 1
        )[1].split(";", 1)[0]
        for column in (
            "owner_user_id",
            "load_unit",
            "source_snapshot",
            "snapshot_schema_version",
            "snapshot_quality",
        ):
            self.assertIn(column, segment_block)

        self.assertIn("'unknown'", self.expand_lower)
        self.assertIn("legacy_unknown", self.expand_lower)
        self.assertNotIn(
            "null legacy rows follow current library classification",
            self.expand_lower,
        )

    def test_conditioning_distance_becomes_typed_without_dropping_legacy_text(
        self,
    ) -> None:
        conditioning_block = self.expand_lower.split(
            "alter table lifeswitch_training.conditioning_session_log", 1
        )[1].split(";", 1)[0]
        self.assertIn("distance_value", conditioning_block)
        self.assertIn("distance_unit", conditioning_block)
        self.assertNotRegex(
            self.expand_lower,
            r"drop\s+column(?:\s+if\s+exists)?\s+distance\b",
        )

    def test_owner_bound_foreign_keys_cover_observation_graph(self) -> None:
        for name in (
            "training_set_log_session_owner_fkey",
            "training_set_segment_set_owner_fkey",
            "training_session_supersedes_owner_fkey",
            "conditioning_session_prescription_owner_fkey",
            "conditioning_session_supersedes_owner_fkey",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.expand_lower)
        self.assertIn("not valid", self.expand_lower)
        self.assertIn("validate constraint", self.expand_lower)

    def test_idempotency_and_single_replacement_are_unique(self) -> None:
        for index in (
            "ux_training_session_owner_idempotency",
            "ux_training_session_single_replacement",
            "ux_conditioning_session_owner_idempotency",
            "ux_conditioning_session_single_replacement",
        ):
            with self.subTest(index=index):
                self.assertIn(index, self.expand_lower)

    def test_legacy_rows_are_labeled_reconstructed_not_captured(self) -> None:
        self.assertIn("legacy_reconstructed", self.expand_lower)
        self.assertIn("legacy_unknown", self.expand_lower)
        self.assertIn("legacy_backfilled", self.expand_lower)
        self.assertIn("exercise_role_snapshot", self.expand_lower)
        self.assertIn("else 'unknown'", self.expand_lower)

    def test_transition_capture_and_audit_cover_all_observation_tables(self) -> None:
        for table in (
            "training_session",
            "training_set_log",
            "training_set_log_segment",
            "conditioning_session_log",
        ):
            with self.subTest(table=table):
                self.assertIn(f"trg_transition_capture_{table}", self.expand_lower)
                self.assertIn(f"trg_transition_audit_{table}", self.expand_lower)
        self.assertIn("legacy_updated", self.expand_lower)
        self.assertIn("legacy_deleted", self.expand_lower)
        self.assertNotIn(
            "training observations cannot be deleted",
            self.expand_lower,
        )

    def test_events_survive_legacy_deletes_and_are_append_only(self) -> None:
        self.assertIn(
            "create table if not exists "
            "lifeswitch_training.training_observation_event",
            self.expand_lower,
        )
        event_table = self.expand_lower.split(
            "create table if not exists "
            "lifeswitch_training.training_observation_event",
            1,
        )[1].split(");", 1)[0]
        self.assertNotIn("references lifeswitch_training", event_table)
        self.assertIn("observation_snapshot jsonb not null", event_table)
        self.assertIn("training observation events are append-only", self.expand_lower)
        self.assertIn("to_jsonb(old)", self.expand_lower)

    def test_truncate_is_rejected_for_observations_and_events(self) -> None:
        for table in (
            "training_session",
            "training_set_log",
            "training_set_log_segment",
            "conditioning_session_log",
            "training_observation_event",
        ):
            with self.subTest(table=table):
                self.assertRegex(
                    self.expand_lower,
                    rf"before\s+truncate\s+on\s+"
                    rf"lifeswitch_training\.{table}\s+"
                    rf"for\s+each\s+statement",
                )

    def test_current_views_use_leaf_nonvoid_observations(self) -> None:
        self.assertIn("training_session_current_v", self.expand_lower)
        self.assertIn("conditioning_session_current_v", self.expand_lower)
        self.assertIn(
            "replacement.supersedes_training_session_id = s.training_session_id",
            self.expand_lower,
        )
        self.assertIn(
            "replacement.supersedes_conditioning_session_id = "
            "c.conditioning_session_log_id",
            self.expand_lower,
        )
        self.assertGreaterEqual(self.expand_lower.count("voided_at is null"), 2)


if __name__ == "__main__":
    unittest.main()
