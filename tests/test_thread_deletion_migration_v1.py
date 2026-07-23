from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "ops" / "sql" / "20260722_voice_thread_deletion_v1.sql"


class ThreadDeletionMigrationV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def test_grants_only_required_response_record_deletes(self) -> None:
        self.assertIn("grant delete", self.sql)
        self.assertIn("memory.assistant_transcript_attestation_v1", self.sql)
        self.assertIn("memory.final_answer_memory_binding_v1", self.sql)
        self.assertIn("to brains_app", self.sql)
        self.assertNotIn("grant all", self.sql)

    def test_requires_existing_governed_lifecycle(self) -> None:
        self.assertIn("memory.transition_evidence_lifecycle", self.sql)
        self.assertIn("memory v1 evidence lifecycle is required", self.sql)

    def test_adds_owner_thread_telemetry_delete_index(self) -> None:
        self.assertIn("telemetry_event_actor_thread_idx", self.sql)
        self.assertIn("(actor_user_id,thread_id)", self.sql)


if __name__ == "__main__":
    unittest.main()
