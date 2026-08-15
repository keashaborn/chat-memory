from __future__ import annotations

from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.chat_memory_ingest_coordination_v1 import (
    ChatMemoryIngestCoordinationErrorV1,
    coordinate_chat_memory_ingest_v1,
)


ROOT = Path(__file__).resolve().parents[2]
OWNER = UUID("11111111-1111-4111-8111-111111111111")
MESSAGE = UUID("22222222-2222-4222-8222-222222222222")
THREAD = UUID("33333333-3333-4333-8333-333333333333")
REQUEST_ID = "44444444-4444-4444-8444-444444444444"


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, outcome: object = "released") -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> None:
        self.calls.append((query, args))

    async def fetchval(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        return self.outcome


class ChatMemoryIngestCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_is_owner_message_thread_and_hash_bound(self) -> None:
        connection = FakeConnection()
        outcome = await coordinate_chat_memory_ingest_v1(
            connection,
            owner_user_id=OWNER,
            message_id=MESSAGE,
            request_id=REQUEST_ID,
            thread_id=THREAD,
            message="ordinary chat",
            resolution="release",
        )
        self.assertEqual(outcome, "released")
        self.assertEqual(connection.calls[0][1], (str(OWNER),))
        self.assertEqual(
            connection.calls[1][1][0:3],
            (MESSAGE, REQUEST_ID, THREAD),
        )
        self.assertRegex(str(connection.calls[1][1][3]), r"^[0-9a-f]{64}$")
        self.assertEqual(connection.calls[1][1][4], "release")

    async def test_request_id_alone_can_validate_exact_logged_source(self) -> None:
        connection = FakeConnection("validated")
        outcome = await coordinate_chat_memory_ingest_v1(
            connection,
            owner_user_id=OWNER,
            message_id=None,
            request_id=REQUEST_ID,
            thread_id=THREAD,
            message="replace the old preference",
            resolution="validate",
        )
        self.assertEqual(outcome, "validated")
        self.assertEqual(connection.calls[1][1][0:3], (None, REQUEST_ID, THREAD))

    async def test_suppress_accepts_terminal_receipts_only(self) -> None:
        for outcome in ("absent", "replayed", "suppressed"):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    await coordinate_chat_memory_ingest_v1(
                        FakeConnection(outcome),
                        owner_user_id=OWNER,
                        message_id=MESSAGE,
                        request_id=REQUEST_ID,
                        thread_id=THREAD,
                        message="replace the old preference",
                        resolution="suppress",
                    ),
                    outcome,
                )
        with self.assertRaises(ChatMemoryIngestCoordinationErrorV1):
            await coordinate_chat_memory_ingest_v1(
                FakeConnection("claimed"),
                owner_user_id=OWNER,
                message_id=MESSAGE,
                request_id=REQUEST_ID,
                thread_id=THREAD,
                message="replace the old preference",
                resolution="suppress",
            )

    async def test_missing_message_and_request_identifiers_fails_closed(self) -> None:
        with self.assertRaises(ChatMemoryIngestCoordinationErrorV1):
            await coordinate_chat_memory_ingest_v1(
                FakeConnection(),
                owner_user_id=OWNER,
                message_id=None,
                request_id=None,
                thread_id=THREAD,
                message="replace the old preference",
                resolution="validate",
            )

    def test_source_migration_is_bounded_and_reversible(self) -> None:
        migration = (
            ROOT
            / "ops/governed_memory/source_migrations"
            / "0003_chat_memory_command_coordination"
        )
        forward = (migration / "forward.pgsql").read_text()
        rollback = (migration / "rollback.pgsql").read_text()
        self.assertIn("NEW.created_at + interval '120 seconds'", forward)
        self.assertIn("resolve_chat_memory_command", forward)
        self.assertIn("session_user <> 'brains_app'", forward)
        self.assertIn("source.owner_user_id = actor", forward)
        self.assertIn("source.thread_id = p_thread_id", forward)
        self.assertIn("source.request_id = p_request_id", forward)
        self.assertIn("INTO STRICT source_row", forward)
        self.assertIn("p_content_sha256", forward)
        self.assertIn("p_resolution IS NULL", forward)
        self.assertIn("eligibility_decision = 'skip_zero_call'", forward)
        self.assertIn("TO memory_ingest_writer", forward)
        self.assertNotIn("GRANT SELECT", forward)
        self.assertNotIn("GRANT UPDATE", forward)
        self.assertIn("DROP TRIGGER defer_chat_memory_ingest_until_response", rollback)
        self.assertIn("DROP FUNCTION memory_ingest_private.resolve_chat_memory_command", rollback)


if __name__ == "__main__":
    unittest.main()
