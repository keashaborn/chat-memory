from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from rag_engine.web_transcript_persistence_v1 import (
    WEB_ASSISTANT_SOURCE,
    WEB_USER_SOURCE,
    WebTranscriptPersistenceError,
    persist_web_exchange_v1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
SEARCH = UUID("49c59ba0-e188-40f8-932d-51fa6b84e157")


class FakeTransaction:
    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeConnection:
    def __init__(self, *, owns_thread: bool = True) -> None:
        self.owns_thread = owns_thread
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> bool:
        return self.owns_thread


class WebTranscriptPersistenceV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_is_atomic_memory_ineligible_and_ordered(self) -> None:
        conn = FakeConnection()
        answer_id = await persist_web_exchange_v1(
            conn,
            owner_user_id=OWNER,
            thread_id=THREAD,
            request_id="request-web-001",
            query="What happened with OpenAI today?",
            answer="A sourced answer.",
            search_id=SEARCH,
            route="current_news",
            policy_version="search_decision_v1_2",
            decision="live",
            cited_sources=[{"url": "https://status.openai.com"}],
            admitted_sources=[{"url": "https://status.openai.com"}],
            consulted_source_count=1,
        )
        self.assertIsInstance(answer_id, UUID)
        statements = "\n".join(sql for sql, _ in conn.execute_calls)
        self.assertIn("INSERT INTO public.chat_log", statements)
        self.assertIn(
            "INSERT INTO trusted_web.response_transcript_v1",
            statements,
        )
        self.assertIn("interval '1 microsecond'", statements)
        self.assertNotIn("qdrant", statements.lower())
        chat_args = conn.execute_calls[1][1]
        self.assertEqual(chat_args[3], WEB_USER_SOURCE)
        self.assertEqual(chat_args[9], WEB_ASSISTANT_SOURCE)
        self.assertIn("memory_ineligible", chat_args[5])
        self.assertIn("memory_ineligible", chat_args[11])

    async def test_missing_owner_thread_fails_closed(self) -> None:
        conn = FakeConnection(owns_thread=False)
        with self.assertRaises(WebTranscriptPersistenceError):
            await persist_web_exchange_v1(
                conn,
                owner_user_id=OWNER,
                thread_id=THREAD,
                request_id="request-web-002",
                query="query",
                answer="answer",
                search_id=SEARCH,
                route="current_news",
                policy_version="search_decision_v1_2",
                decision="live",
                cited_sources=[],
                admitted_sources=[],
                consulted_source_count=0,
            )


if __name__ == "__main__":
    unittest.main()
