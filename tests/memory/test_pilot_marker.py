from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.runtime.pilot_marker import (
    PilotStartDisposition,
    pilot_ever_started,
    pilot_marker_receipt_sha256,
    plan_pilot_start,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "governed-memory-migrations" / "0004_pilot_marker"
MANIFEST = ROOT / "governed-memory-migrations" / "manifest.json"
OPERATION = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)


class PilotMarkerTests(unittest.TestCase):
    def command(self) -> dict[str, object]:
        return {
            "authorization_receipt_sha256": "b" * 64,
            "operation_id": OPERATION,
            "pilot_contract_sha256": "a" * 64,
            "pilot_id": "governed_memory_pilot_9a54cf123493_000001",
            "started_at": NOW,
        }

    def test_absence_is_false_and_first_command_is_the_only_false_to_true_transition(self) -> None:
        self.assertFalse(pilot_ever_started(None))
        plan = plan_pilot_start(None, **self.command())
        self.assertEqual(plan.disposition, PilotStartDisposition.INSERT)
        self.assertTrue(pilot_ever_started(plan.marker.as_row))
        self.assertEqual(
            plan.marker.marker_receipt_sha256,
            pilot_marker_receipt_sha256(**self.command()),
        )

    def test_sql_absence_is_zero_rows_and_repository_none_is_false(self) -> None:
        forward = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8")
        read_function = forward.split(
            "CREATE FUNCTION memory_private.read_pilot_marker()",
            maxsplit=1,
        )[1].split("$function$;", maxsplit=1)[0]
        self.assertIn("WHERE value.pilot_ever_started", read_function)
        self.assertNotIn("UNION ALL", read_function)
        self.assertNotIn("SELECT false", read_function)
        self.assertFalse(pilot_ever_started(None))

    def test_exact_replay_is_stable_and_conflicting_replay_is_refused(self) -> None:
        first = plan_pilot_start(None, **self.command())
        replay = plan_pilot_start(first.marker.as_row, **self.command())
        self.assertEqual(replay.disposition, PilotStartDisposition.REPLAY)
        conflict = self.command()
        conflict["started_at"] = NOW + timedelta(seconds=1)
        with self.assertRaisesRegex(
            ContractViolation,
            "pilot_marker_conflicting_replay",
        ):
            plan_pilot_start(first.marker.as_row, **conflict)

    def test_migration_is_content_free_append_only_and_rollback_is_permanently_guarded(self) -> None:
        forward = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8")
        rollback = (MIGRATION / "rollback.pgsql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE memory.pilot_marker", forward)
        self.assertIn(
            "CONSTRAINT pilot_marker_singleton PRIMARY KEY (pilot_ever_started)",
            forward,
        )
        self.assertIn("CONSTRAINT pilot_marker_true_only CHECK (pilot_ever_started)", forward)
        self.assertIn("BEFORE UPDATE OR DELETE ON memory.pilot_marker", forward)
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT pilot_marker_singleton DO NOTHING",
            forward,
        )
        self.assertNotIn("ON CONFLICT (pilot_ever_started)", forward)
        self.assertIn("pilot marker conflicting replay", forward)
        self.assertNotIn("owner_user_id", forward)
        self.assertNotIn("message", forward.lower())
        self.assertNotIn("attachment", forward.lower())
        self.assertIn("IF EXISTS (SELECT 1 FROM memory.pilot_marker LIMIT 1)", rollback)
        self.assertNotIn("CASCADE", rollback.upper())

    def test_migration_package_binds_exact_sql_bytes(self) -> None:
        package = json.loads((MIGRATION / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name in ("forward", "rollback"):
            path = MIGRATION / package[name]["path"]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                package[name]["sha256"],
            )
        self.assertTrue(package["data_policy"]["content_free"])
        self.assertTrue(package["data_policy"]["append_only"])
        self.assertTrue(package["data_policy"]["false_to_true_only"])
        self.assertFalse(package["activation"]["production_database_applied"])
        self.assertEqual(
            package["status"],
            "isolated_candidate_disposable_validated_not_production_applied",
        )
        self.assertEqual(
            manifest["status"],
            "isolated_candidate_disposable_validated_not_production_applied",
        )
        self.assertTrue(package["activation"]["disposable_database_validated"])
        self.assertEqual(
            package["object_contract"],
            {
                "schema": "memory",
                "table": "memory.pilot_marker",
                "runtime_role": "governed_memory_worker",
                "content_free": True,
                "global_not_owner_bearing": True,
                "append_only": True,
                "forced_rls": True,
                "direct_runtime_table_access": False,
                "cascade_ddl": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
