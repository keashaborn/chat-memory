from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import UUID

from seebx.adapters.lifeswitch_prior_provenance_postgres import (
    PostgresPriorLifeSwitchRestrictedReadSessionV1,
    PriorLifeSwitchPreparedContextV1,
    PriorLifeSwitchProvenanceProviderV1,
)
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR
from tests.test_prior_lifeswitch_provenance_v1 import bound_snapshot
from tests.test_response_orchestration_v0_2 import THREAD


CONTEXT_ID = UUID("99999999-9999-4999-8999-999999999999")


class TransactionBoundConnection:
    def __init__(self) -> None:
        self.local_settings: dict[str, str] | None = None
        self.transactions: list[dict[str, str]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self, **_kwargs):
        if self.local_settings is not None:
            raise AssertionError("nested transaction is not expected")
        self.local_settings = {}
        try:
            yield
        finally:
            self.transactions.append(dict(self.local_settings))
            self.local_settings = None

    async def execute(self, query: str, *args):
        if self.local_settings is None:
            raise AssertionError("database operation escaped a transaction")
        lowered = " ".join(query.lower().split())
        if "set_config('app.user_id'" in lowered:
            self.local_settings["app.user_id"] = str(args[0])
        elif "set_config('app.lifeswitch_owner_id'" in lowered:
            self.local_settings["app.lifeswitch_owner_id"] = str(args[0])
        elif lowered.startswith("set local role"):
            expected = str(ACTOR)
            if self.local_settings.get("app.user_id") != expected:
                raise AssertionError("read transaction lost app.user_id")
            if self.local_settings.get("app.lifeswitch_owner_id") != expected:
                raise AssertionError("read transaction lost app.lifeswitch_owner_id")
        return "OK"

    async def fetch(self, query: str, *args):
        if self.local_settings is None:
            raise AssertionError("database operation escaped a transaction")
        expected = str(ACTOR)
        if self.local_settings.get("app.user_id") != expected:
            raise AssertionError("query transaction lost app.user_id")
        if self.local_settings.get("app.lifeswitch_owner_id") != expected:
            raise AssertionError("query transaction lost LifeSwitch owner")
        self.fetch_calls.append((query, args))
        return []

    async def fetchval(self, query: str, *args):
        if self.local_settings is None:
            raise AssertionError("database operation escaped a transaction")
        lowered = " ".join(query.lower().split())
        if "begin_owner_read_context_v1" in lowered:
            if self.local_settings.get("app.user_id") != str(ACTOR):
                raise AssertionError("context transaction lacks app.user_id")
            if self.local_settings.get("app.lifeswitch_owner_id") != str(ACTOR):
                raise AssertionError("context transaction lacks LifeSwitch owner")
            return CONTEXT_ID
        if "end_owner_read_context_v1" in lowered:
            return True
        raise AssertionError(f"unexpected fetchval: {query}")


class Pool:
    def __init__(self, connection: TransactionBoundConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class Session:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def select(self, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private database detail")
        return PriorLifeSwitchPreparedContextV1.create(
            status="EMPTY", database_accessed=True
        )


def snapshot(message: str):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        current_request_id="prior-runtime-request",
        current_message=message,
    )


class PriorLifeSwitchRuntimeV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_off_performs_zero_database_reads(self) -> None:
        session = Session()
        result = await PriorLifeSwitchProvenanceProviderV1(session).prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot("What are my macros?"),
        )
        self.assertEqual(result.status, "OFF")
        self.assertFalse(result.database_accessed)
        self.assertEqual(session.calls, 0)

    async def test_failure_is_optional_and_content_free(self) -> None:
        session = Session(fail=True)
        result = await PriorLifeSwitchProvenanceProviderV1(session).prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(
                "Where did you get those earlier protein numbers?"
            ),
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertTrue(result.database_accessed)
        self.assertEqual(session.calls, 1)
        self.assertNotIn("private database detail", result.model_dump_json())

    async def test_owner_gucs_are_rebound_inside_read_transaction(self) -> None:
        connection = TransactionBoundConnection()
        session = PostgresPriorLifeSwitchRestrictedReadSessionV1(Pool(connection))
        bound_snapshot = snapshot("Where did you get those earlier protein numbers?")
        with patch(
            "seebx.adapters.lifeswitch_prior_provenance_postgres."
            "select_prior_lifeswitch_provenance_v1",
            new=AsyncMock(return_value=None),
        ) as selector:
            result = await session.select(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=bound_snapshot,
            )
        self.assertEqual(result.status, "EMPTY")
        self.assertEqual(len(connection.transactions), 3)
        expected = {
            "app.user_id": str(ACTOR),
            "app.lifeswitch_owner_id": str(ACTOR),
        }
        self.assertEqual(connection.transactions[0], expected)
        self.assertEqual(connection.transactions[1], expected)
        self.assertEqual(connection.transactions[2], {})
        selector.assert_awaited_once()
        self.assertEqual(connection.fetch_calls, [])

    async def test_bound_query_is_owned_by_postgres_adapter(self) -> None:
        connection = TransactionBoundConnection()
        session = PostgresPriorLifeSwitchRestrictedReadSessionV1(Pool(connection))
        source = bound_snapshot()
        with patch(
            "seebx.adapters.lifeswitch_prior_provenance_postgres."
            "select_prior_lifeswitch_provenance_v1",
            new=AsyncMock(return_value=None),
        ):
            result = await session.select(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=source,
            )
        self.assertEqual(result.status, "EMPTY")
        self.assertEqual(len(connection.fetch_calls), 1)
        query, args = connection.fetch_calls[0]
        self.assertIn("read_prior_answer_lifeswitch_provenance_v1", query)
        self.assertEqual(args[0], CONTEXT_ID)
        self.assertEqual(args[1], ACTOR)
        self.assertEqual(args[2], source.thread_id)
        self.assertEqual(args[3], source.cutoff_created_at)
        self.assertEqual(args[4], source.current_log_id)


if __name__ == "__main__":
    unittest.main()
