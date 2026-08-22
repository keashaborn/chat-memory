from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from seebx.adapters.zep_sync_postgres import (
    PostgresZepSyncRepository,
    enqueue_zep_turn,
)


ROOT = Path(__file__).resolve().parents[1]
JOB = UUID("11111111-1111-4111-8111-111111111111")
LEASE = UUID("22222222-2222-4222-8222-222222222222")
OWNER = UUID("33333333-3333-4333-8333-333333333333")
THREAD = UUID("44444444-4444-4444-8444-444444444444")
USER_MESSAGE = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        return None


class Connection:
    def __init__(self, row=None, *, fetchval=None, command="UPDATE 1") -> None:
        self.row = row
        self.fetchval_value = fetchval
        self.command = command
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return Transaction()

    async def fetchrow(self, sql: str, *args: object):
        self.calls.append((sql, args))
        return self.row

    async def fetchval(self, sql: str, *args: object):
        self.calls.append((sql, args))
        return self.fetchval_value

    async def execute(self, sql: str, *args: object):
        self.calls.append((sql, args))
        return self.command


class ConnectionContext:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_: object):
        return None


class Provider:
    def __init__(self, connection: Connection) -> None:
        self.value = connection

    def connection(self):
        return ConnectionContext(self.value)


class ZepSyncPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_stores_only_stable_identifiers(self) -> None:
        connection = Connection(fetchval=JOB)
        result = await enqueue_zep_turn(
            connection,
            owner_user_id=OWNER,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=JOB,
        )
        self.assertEqual(result, JOB)
        sql, arguments = connection.calls[0]
        self.assertIn("conversation_sync_private.zep_turn_outbox", sql)
        self.assertNotIn("text", sql.casefold().split("from", 1)[0])
        self.assertEqual(arguments, (OWNER, THREAD, USER_MESSAGE, JOB))

    async def test_claim_rehydrates_content_from_canonical_chat_rows(self) -> None:
        connection = Connection(
            row={
                "job_id": JOB,
                "lease_token": LEASE,
                "owner_user_id": OWNER,
                "thread_id": THREAD,
                "user_message_id": USER_MESSAGE,
                "assistant_message_id": JOB,
                "attempt_count": 2,
                "user_message": "user text",
                "assistant_message": "assistant text",
                "user_created_at": NOW,
                "assistant_created_at": NOW,
            }
        )
        repository = PostgresZepSyncRepository(Provider(connection))
        claimed = await repository.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.job_id, JOB)
        self.assertEqual(claimed.user_message, "user text")
        self.assertEqual(claimed.attempt_count, 2)
        sql = connection.calls[0][0]
        self.assertIn("FOR UPDATE OF item SKIP LOCKED", sql)
        self.assertIn("prior_user.created_at", sql)
        self.assertIn("'failed_terminal'", sql)
        self.assertIn("conversation.chat_log", sql)

    async def test_lost_lease_fails_closed(self) -> None:
        repository = PostgresZepSyncRepository(
            Provider(Connection(command="UPDATE 0"))
        )
        from seebx.capabilities.conversation.zep_sync import ZepTurnSyncJob

        value = ZepTurnSyncJob(
            job_id=JOB,
            lease_token=LEASE,
            owner_user_id=OWNER,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=JOB,
            user_message="user text",
            assistant_message="assistant text",
            user_created_at=NOW,
            assistant_created_at=NOW,
            attempt_count=1,
        )
        with self.assertRaisesRegex(RuntimeError, "zep_sync_lease_lost"):
            await repository.mark_completed(value)


class ZepSyncMigrationTests(unittest.TestCase):
    def test_migration_is_private_content_free_and_restart_safe(self) -> None:
        migration = (
            ROOT / "ops/sql/20260818_conversation_zep_sync_outbox_v1.sql"
        ).read_text()
        self.assertIn("conversation_sync_private.zep_turn_outbox", migration)
        self.assertIn("TO brains_app", migration)
        self.assertIn("lease_expires_at", migration)
        self.assertIn("failed_terminal", migration)
        self.assertIn("ON DELETE CASCADE", migration)
        self.assertNotIn("GRANT SELECT,INSERT,UPDATE,DELETE", migration)
        self.assertIn("GRANT INSERT (", migration)
        self.assertIn("GRANT UPDATE (", migration)
        self.assertNotIn("message_text", migration)
        self.assertNotIn("user_message text", migration)
        self.assertNotIn("assistant_message text", migration)

    def test_rollback_refuses_to_discard_ledger_rows(self) -> None:
        rollback = (
            ROOT
            / "ops/sql/20260818_conversation_zep_sync_outbox_v1_rollback.sql"
        ).read_text()
        self.assertIn("refuses to discard ledger rows", rollback)
        self.assertLess(rollback.index("EXISTS ("), rollback.index("DROP TABLE"))


if __name__ == "__main__":
    unittest.main()
