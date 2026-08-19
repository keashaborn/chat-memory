from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "chat-history-migrations/0003_zep_outbox_cutover"


class ChatHistoryZepOutboxCutoverV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward = (MIGRATION / "forward.pgsql").read_text(
            encoding="utf-8"
        ).lower()
        cls.rollback = (MIGRATION / "rollback.pgsql").read_text(
            encoding="utf-8"
        ).lower()
        cls.package = json.loads(
            (MIGRATION / "package.json").read_text(encoding="utf-8")
        )

    def test_forward_uses_only_canonical_zep_outbox(self) -> None:
        self.assertGreaterEqual(
            self.forward.count("conversation_sync_private.zep_turn_outbox"),
            7,
        )
        self.assertNotIn(
            "from memory_ingest_private.memory_ingest_outbox",
            self.forward,
        )
        self.assertNotIn(
            "delete from memory_ingest_private.memory_ingest_outbox",
            self.forward,
        )
        self.assertEqual(self.forward.count("and outbox.state = 'processing'"), 2)
        self.assertNotIn("outbox.message_id", self.forward)
        self.assertEqual(self.forward.count("outbox.user_message_id"), 4)
        self.assertEqual(self.forward.count("outbox.assistant_message_id"), 4)

    def test_forward_removes_all_legacy_capture_triggers(self) -> None:
        for trigger in (
            "chat_attachments_serialize_source_erasure",
            "chat_log_serialize_source_erasure",
            "threads_serialize_source_erasure",
            "response_transcript_serialize_source_erasure",
        ):
            self.assertIn(f"drop trigger if exists {trigger}", self.forward)

    def test_rollback_restores_legacy_functions_and_triggers(self) -> None:
        self.assertGreaterEqual(
            self.rollback.count("memory_ingest_private.memory_ingest_outbox"),
            7,
        )
        self.assertEqual(self.rollback.count("create trigger "), 4)
        self.assertIn("serialize_attachment_source_erasure()", self.rollback)
        self.assertIn("serialize_chat_source_erasure()", self.rollback)
        self.assertIn("serialize_thread_source_erasure()", self.rollback)
        self.assertIn("serialize_response_transcript_source_erasure()", self.rollback)

    def test_package_hashes_bind_exact_sql(self) -> None:
        for key in ("forward", "rollback"):
            path = MIGRATION / self.package[key]["path"]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                self.package[key]["sha256"],
            )
        self.assertTrue(
            self.package["scope"]["legacy_memory_ingest_capture_disabled"]
        )
        self.assertFalse(self.package["scope"]["governed_memory_rows_mutated"])


if __name__ == "__main__":
    unittest.main()
