from __future__ import annotations

import datetime as dt
import hashlib
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
)
from seebx.adapters.lifeswitch_context_postgres import (
    PostgresRestrictedLifeSwitchReadSessionV1,
)
from seebx.capabilities.conversation.lifeswitch_context import (
    LifeSwitchPreparedContextV1,
    LifeSwitchResponseContextProviderV1,
)
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)


ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
THREAD = uuid.UUID("22222222-2222-4222-8222-222222222222")
NOW = dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc)
CONTEXT = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def snapshot(message: str, *, request_id: str = "request-123"):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        current_request_id=request_id,
        current_message=message,
    )


def selected_current_plan_source():
    query = "What is my current plan?"
    source = snapshot(
        query,
        request_id="33333333-3333-4333-8333-333333333333",
    )
    plan = create_lifeswitch_data_plan_v1(query, today=NOW.date())
    trusted = TrustedLifeSwitchContextRequestV1.create(
        request_id=source.current_request_id,
        authenticated_actor_user_id=ACTOR,
        owner_user_id=ACTOR,
        thread_id=THREAD,
        conversation_snapshot_sha256=source.snapshot_sha256,
        owner_timezone="America/Chicago",
        query=query,
        data_plan=plan,
    )
    section = LifeSwitchContextSectionV1.create(
        projection="current_plan",
        status="AVAILABLE",
        window=None,
        record_count=1,
        source_relations=("lifeswitch_agentic.plan_versions",),
        payload={"primary_goal": "Maintain"},
    )
    envelope = create_lifeswitch_context_envelope_v1(
        request=trusted,
        plan_source="agentic_active",
        as_of_local_date=NOW.date(),
        sections=(section,),
        generated_at=NOW,
    )
    return source, trusted, envelope


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
        self.fetchval_calls = []

    def transaction(self, **kwargs):
        return FakeReadTransaction(self, kwargs)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return None

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        if "begin_owner_read_context_v1" in query:
            return CONTEXT
        if "end_owner_read_context_v1" in query:
            return True
        if "pg_current_snapshot" in query:
            return "123:456:"
        raise AssertionError(f"unexpected fetchval query: {query}")


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


class RecordingSelfShadowObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def observe(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("shadow failed")


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

    async def test_lifting_weight_progress_uses_restricted_session(self) -> None:
        session = SpyRestrictedSession()
        provider = LifeSwitchResponseContextProviderV1(
            session,
            utc_clock=lambda: NOW,
        )
        source = snapshot(
            "If you look at my progress with the different weights, "
            "I've been doing do you have any suggestions?"
        )

        result = await provider.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=source,
        )

        self.assertTrue(result.database_accessed)
        self.assertEqual(result.data_plan.intent, "LIFTING_PROGRESSION_SUMMARY")
        self.assertEqual(len(session.calls), 1)
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
            [
                {},
                {"isolation": "repeatable_read", "readonly": True},
                {},
            ],
        )
        sql = "\n".join(query for query, _ in conn.execute_calls)
        self.assertIn("set transaction read write", sql)
        self.assertIn("app.user_id", sql)
        self.assertIn("app.lifeswitch_owner_id", sql)
        self.assertIn("set local role lifeswitch_chat_reader", sql)
        self.assertEqual(len(conn.fetchrow_calls), 1)
        self.assertIn("read_owner_timezone_v1", conn.fetchrow_calls[0][0])
        self.assertEqual(conn.fetchrow_calls[0][1], (CONTEXT,))
        self.assertEqual(len(conn.fetchval_calls), 2)
        begin_query, begin_args = conn.fetchval_calls[0]
        self.assertIn("begin_owner_read_context_v1", begin_query)
        self.assertEqual(begin_args[0], ACTOR)
        self.assertEqual(begin_args[1], THREAD)
        self.assertEqual(
            begin_args[2],
            hashlib.sha256(b"request-123").hexdigest(),
        )
        self.assertEqual(begin_args[3], source.snapshot_sha256)
        self.assertIn("end_owner_read_context_v1", conn.fetchval_calls[1][0])
        self.assertEqual(conn.fetchval_calls[1][1], (CONTEXT,))
        self.assertEqual(result.data_plan.window.start_date, dt.date(2026, 7, 27))

    async def test_selected_result_runs_optional_shadow_in_same_transaction(self) -> None:
        conn = FakeReadConnection()
        observer = RecordingSelfShadowObserver()
        session = PostgresRestrictedLifeSwitchReadSessionV1(
            FakePool(conn),
            utc_clock=lambda: NOW,
            self_shadow_observer=observer,
        )
        source, trusted, envelope = selected_current_plan_source()

        with patch.object(
            session,
            "_owner_timezone",
            new=AsyncMock(return_value=("America/Chicago", "account_timezone")),
        ), patch(
            "seebx.adapters.lifeswitch_context_postgres."
            "LifeSwitchDomainContextProviderV1.select",
            new=AsyncMock(return_value=envelope),
        ):
            result = await session.select(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=source,
                query=source.messages[-1].content,
            )

        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(result.envelope, envelope)
        self.assertEqual(len(observer.calls), 1)
        observed = observer.calls[0]
        self.assertEqual(observed["request"], trusted)
        self.assertEqual(observed["envelope"], envelope)
        self.assertEqual(observed["context_snapshot_id"], CONTEXT)
        self.assertEqual(observed["evaluated_at"], NOW)
        self.assertEqual(
            observed["transaction_snapshot_digest"],
            hashlib.sha256(b"123:456:").hexdigest(),
        )
        snapshot_reads = [
            query
            for query, _ in conn.fetchval_calls
            if "pg_current_snapshot" in query
        ]
        self.assertEqual(snapshot_reads, ["select pg_current_snapshot()::text"])

    async def test_shadow_failure_does_not_change_selected_result(self) -> None:
        conn = FakeReadConnection()
        observer = RecordingSelfShadowObserver(fail=True)
        session = PostgresRestrictedLifeSwitchReadSessionV1(
            FakePool(conn),
            utc_clock=lambda: NOW,
            self_shadow_observer=observer,
        )
        source, _, envelope = selected_current_plan_source()

        with patch.object(
            session,
            "_owner_timezone",
            new=AsyncMock(return_value=("America/Chicago", "account_timezone")),
        ), patch(
            "seebx.adapters.lifeswitch_context_postgres."
            "LifeSwitchDomainContextProviderV1.select",
            new=AsyncMock(return_value=envelope),
        ), self.assertLogs(
            "seebx.adapters.lifeswitch_context_postgres",
            level="WARNING",
        ) as captured:
            result = await session.select(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=source,
                query=source.messages[-1].content,
            )

        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(result.envelope, envelope)
        self.assertEqual(len(observer.calls), 1)
        self.assertEqual(
            captured.output,
            [
                "WARNING:seebx.adapters.lifeswitch_context_postgres:"
                "LifeSwitch V2 self shadow observation unavailable"
            ],
        )


if __name__ == "__main__":
    unittest.main()
