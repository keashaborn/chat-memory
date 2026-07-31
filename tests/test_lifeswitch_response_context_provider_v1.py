from __future__ import annotations

import datetime as dt
import unittest
import uuid

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
    LifeSwitchResponseContextProviderV1,
    PostgresRestrictedLifeSwitchReadSessionV1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)


ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
THREAD = uuid.UUID("22222222-2222-4222-8222-222222222222")
NOW = dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc)


def snapshot(message: str):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        current_request_id="request-123",
        current_message=message,
    )


class SpyRestrictedSession:
    def __init__(self, *, timezone_available: bool = False) -> None:
        self.calls = []
        self.timezone_available = timezone_available

    async def select(self, **kwargs):
        self.calls.append(kwargs)
        query = kwargs["query"]
        plan = create_lifeswitch_data_plan_v1(query, today=NOW.date())
        return LifeSwitchPreparedContextV1.create(
            status="TIMEZONE_UNAVAILABLE",
            timezone_source="unavailable",
            database_accessed=True,
            data_plan=plan,
        )


class FakeReadTransaction:
    def __init__(self, conn, kwargs):
        self.conn = conn
        self.kwargs = kwargs

    async def __aenter__(self):
        self.conn.transaction_calls.append(self.kwargs)

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeReadConnection:
    def __init__(self):
        self.transaction_calls = []
        self.execute_calls = []
        self.fetchrow_calls = []

    def transaction(self, **kwargs):
        return FakeReadTransaction(self, kwargs)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return None


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class LifeSwitchResponseContextProviderV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_off_performs_zero_database_session_calls(self) -> None:
        session = SpyRestrictedSession()
        provider = LifeSwitchResponseContextProviderV1(
            session,
            utc_clock=lambda: NOW,
        )

        result = await provider.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(
                "Who won America's Next Top Model in 2015?"
            ),
        )

        self.assertEqual(result.status, "OFF")
        self.assertFalse(result.database_accessed)
        self.assertEqual(session.calls, [])

    async def test_active_intent_uses_one_restricted_session(self) -> None:
        session = SpyRestrictedSession()
        provider = LifeSwitchResponseContextProviderV1(
            session,
            utc_clock=lambda: NOW,
        )
        source = snapshot("Was I low on protein Monday?")

        result = await provider.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=source,
        )

        self.assertEqual(result.status, "TIMEZONE_UNAVAILABLE")
        self.assertTrue(result.database_accessed)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0]["authenticated_actor_user_id"],
            ACTOR,
        )
        self.assertEqual(session.calls[0]["conversation_snapshot"], source)

    async def test_actor_mismatch_fails_before_database_access(self) -> None:
        session = SpyRestrictedSession()
        provider = LifeSwitchResponseContextProviderV1(
            session,
            utc_clock=lambda: NOW,
        )

        with self.assertRaisesRegex(ValueError, "actor differs"):
            await provider.prepare(
                authenticated_actor_user_id=uuid.uuid4(),
                conversation_snapshot=snapshot("How am I doing?"),
            )

        self.assertEqual(session.calls, [])

    async def test_postgres_session_uses_restricted_repeatable_read_and_clock(self) -> None:
        conn = FakeReadConnection()
        session = PostgresRestrictedLifeSwitchReadSessionV1(
            FakePool(conn),
            utc_clock=lambda: NOW,
        )
        source = snapshot("Was I low on protein Monday?")

        result = await session.select(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=source,
            query=source.messages[-1].content,
        )

        self.assertEqual(result.status, "TIMEZONE_UNAVAILABLE")
        self.assertEqual(
            conn.transaction_calls,
            [{"isolation": "repeatable_read", "readonly": True}],
        )
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertIn("app.lifeswitch_owner_id", sql)
        self.assertIn("set local role lifeswitch_chat_reader_v1", sql)
        self.assertEqual(len(conn.fetchrow_calls), 1)
        self.assertEqual(result.data_plan.window.start_date, dt.date(2026, 7, 27))


if __name__ == "__main__":
    unittest.main()
