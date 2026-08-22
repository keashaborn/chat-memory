from __future__ import annotations

import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from seebx.adapters.conversation_export import (
    ConversationExportRepositoryError,
    PostgresConversationExportRepository,
)
from seebx.capabilities.conversation.export import (
    ConversationExportArtifact,
    ConversationExportError,
    ConversationExportService,
    EXPORT_SCHEMA_VERSION,
)

from seebx.capabilities.conversation.export_routes import (
    create_conversation_export_router,
)

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
THREAD = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
MESSAGE = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        return False


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.counts = {
            "threads": 1,
            "messages": 1,
            "attachments": 1,
            "web_transcripts": 1,
        }

    def transaction(self, **kwargs: object) -> Transaction:
        self.transaction_options = kwargs
        return Transaction()

    async def fetchval(self, query: str):
        if "current_user" in query:
            return "brains_app"
        if "transaction_read_only" in query:
            return "on"
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: object):
        self.queries.append((query, args))
        return self.counts

    async def fetch(self, query: str, *args: object):
        self.queries.append((query, args))
        if "FROM conversation.threads" in query:
            return [{"id": THREAD, "title": "T", "created_at": NOW}]
        if "FROM conversation.chat_log" in query:
            return [{"id": MESSAGE, "thread_id": THREAD, "text": "hello"}]
        if "FROM conversation.chat_attachments" in query:
            return [
                {
                    "id": UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                    "thread_id": THREAD,
                    "message_id": MESSAGE,
                    "content": "attachment text",
                }
            ]
        if "trusted_web.response_transcript_v1" in query:
            return [{"response_id": UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")}]
        raise AssertionError(query)


class FakeProvider:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.owner = None

    @asynccontextmanager
    async def owner_connection(self, owner: UUID):
        self.owner = owner
        yield self.connection


class ConversationExportRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_is_owner_scoped_repeatable_and_complete(self) -> None:
        connection = FakeConnection()
        provider = FakeProvider(connection)
        result = await PostgresConversationExportRepository(  # type: ignore[arg-type]
            provider
        ).export_owner(OWNER)
        self.assertEqual(provider.owner, OWNER)
        self.assertEqual(
            connection.transaction_options,
            {"isolation": "repeatable_read", "readonly": True},
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"], connection.counts)
        self.assertEqual(result["attachments"][0]["content"], "attachment text")
        sql = "\n".join(query for query, _ in connection.queries)
        self.assertNotIn("conversation_sync_private", sql)
        self.assertNotIn("lifeswitch_usage", sql)
        self.assertTrue(
            all(args == (OWNER,) for _, args in connection.queries)
        )

    async def test_count_drift_fails_closed(self) -> None:
        connection = FakeConnection()
        connection.counts["messages"] = 2
        with self.assertRaisesRegex(
            ConversationExportRepositoryError,
            "snapshot_count_mismatch",
        ):
            await PostgresConversationExportRepository(  # type: ignore[arg-type]
                FakeProvider(connection)
            ).export_owner(OWNER)


class FakeRepository:
    async def export_owner(self, owner: UUID):
        return {
            "status": "complete",
            "counts": {
                "threads": 1,
                "messages": 1,
                "attachments": 0,
                "web_transcripts": 0,
            },
            "threads": [{"id": THREAD, "created_at": NOW}],
            "messages": [{"id": MESSAGE, "text": "hello"}],
            "attachments": [],
            "trusted_web_transcripts": [],
        }


class FakeMemory:
    def __init__(self, status: str = "complete") -> None:
        self.status = status

    async def export_owner_memory(self, owner: UUID):
        return {
            "status": self.status,
            "provider": "zep",
            "user": {"user_id": f"lifeswitch-user-{owner}"},
            "threads": [],
            "graph": {
                "nodes": [],
                "edges": [],
                "observations": [],
                "thread_summaries": [],
            },
            "raw_episode_basis": "thread_messages",
        }


class ConversationExportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_hash_bound_complete_artifact(self) -> None:
        service = ConversationExportService(
            repository=FakeRepository(),
            memory_runtime=FakeMemory(),
            clock=lambda: NOW,
        )
        artifact = await service.build(OWNER)
        payload = json.loads(artifact.body)
        self.assertEqual(payload["schema_version"], EXPORT_SCHEMA_VERSION)
        self.assertEqual(payload["owner_user_id"], str(OWNER))
        self.assertEqual(payload["generated_at"], "2026-08-19T12:00:00Z")
        self.assertEqual(payload["completeness"]["zep_memory"], "complete")
        self.assertNotIn("zep_turn_outbox", artifact.body.decode())
        self.assertNotIn("ai_usage_event_v1", artifact.body.decode())
        import hashlib

        self.assertEqual(artifact.sha256, hashlib.sha256(artifact.body).hexdigest())

    async def test_unknown_memory_status_fails_closed(self) -> None:
        service = ConversationExportService(
            repository=FakeRepository(),
            memory_runtime=FakeMemory("partial"),
            clock=lambda: NOW,
        )
        with self.assertRaises(ConversationExportError) as raised:
            await service.build(OWNER)
        self.assertEqual(raised.exception.code, "conversation_export_incomplete")
        self.assertEqual(raised.exception.status_code, 503)


class ConversationExportRouteTests(unittest.TestCase):
    def _client(self, service: object) -> TestClient:
        app = FastAPI()
        app.include_router(create_conversation_export_router(service))  # type: ignore[arg-type]
        return TestClient(app)

    def test_success_is_downloadable_hash_bound_and_no_store(self) -> None:
        service = SimpleNamespace(
            build=AsyncMock(
                return_value=ConversationExportArtifact(
                    body=b'{"status":"complete"}',
                    sha256="a" * 64,
                    generated_at="2026-08-19T12:00:00Z",
                )
            )
        )
        identity = SimpleNamespace(actor_user_id=str(OWNER))
        with patch(
            "seebx.capabilities.conversation.export_routes."
            "require_verified_supabase_request_identity",
            new=AsyncMock(return_value=identity),
        ):
            response = self._client(service).get(
                "/conversation/export",
                headers={"x-request-id": "request-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "complete"})
        self.assertEqual(response.headers["x-content-sha256"], "a" * 64)
        self.assertEqual(response.headers["x-request-id"], "request-1")
        self.assertEqual(
            response.headers["cache-control"],
            "private, no-store, max-age=0, must-revalidate",
        )
        self.assertIn(
            "lifeswitch-conversation-memory-20260819T120000Z.json",
            response.headers["content-disposition"],
        )
        service.build.assert_awaited_once_with(OWNER)

    def test_auth_failure_is_no_store(self) -> None:
        service = SimpleNamespace(build=AsyncMock())
        with patch(
            "seebx.capabilities.conversation.export_routes."
            "require_verified_supabase_request_identity",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=401,
                    detail="invalid_supabase_access_token",
                )
            ),
        ):
            response = self._client(service).get("/conversation/export")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "invalid_supabase_access_token",
        )
        self.assertIn("no-store", response.headers["cache-control"])
        service.build.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
