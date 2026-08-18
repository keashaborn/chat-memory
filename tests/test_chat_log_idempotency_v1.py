from __future__ import annotations

import importlib
import json
import os
import unittest
from collections.abc import Mapping
from unittest.mock import AsyncMock, patch
from uuid import UUID

from starlette.requests import Request


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
SUBMISSION = UUID("7240822d-ac9a-4096-95aa-e2b24d36ef50")


def request(body: Mapping[str, object]) -> Request:
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive() -> Mapping[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": encoded,
            "more_body": False,
        }

    value = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/log",
            "headers": [(b"authorization", b"Bearer a.b.c")],
        },
        receive,
    )
    value.state.request_id = "synthetic-request"
    return value


class FakeTransaction:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeConnection:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, object]] = {}
        self.insert_count = 0
        self.transactions: list[FakeTransaction] = []

    def transaction(self) -> FakeTransaction:
        value = FakeTransaction()
        self.transactions.append(value)
        return value

    async def fetchrow(self, query: str, *args: object) -> object | None:
        if "FROM threads" in query:
            return {"owner_user_id": OWNER}
        if "FROM public.chat_log" in query:
            submission = UUID(str(args[0]))
            row = self.rows.get(submission)
            if row is None:
                return None
            if "thread_id IS NOT DISTINCT FROM" not in query:
                return {"id": submission}
            _, owner, thread, source, text = args
            if (
                row["owner"] == owner
                and row["thread"] == thread
                and row["source"] == source
                and row["text"] == text
            ):
                return {"id": submission}
            return None
        raise AssertionError(query)

    async def execute(self, query: str, *args: object) -> str:
        if "INSERT INTO chat_log" in query:
            submission = UUID(str(args[0]))
            self.rows[submission] = {
                "owner": UUID(str(args[1])),
                "thread": args[7],
                "source": args[4],
                "text": args[5],
            }
            self.insert_count += 1
        return "OK"

    async def close(self) -> None:
        return None


class ChatLogSubmissionIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                )
            },
            clear=False,
        ):
            cls.backend = importlib.import_module("app")

    def body(self, text: str = "Tell me about my animals.") -> dict[str, object]:
        return {
            "user_id": str(OWNER),
            "thread_id": str(THREAD),
            "source": "frontend/chat:user",
            "text": text,
            "tags": ["user", "chat"],
            "submission_id": str(SUBMISSION),
        }

    async def call(self, connection: FakeConnection, body: Mapping[str, object]):
        with (
            patch.object(
                self.backend,
                "require_actor",
                new=AsyncMock(return_value=str(OWNER)),
            ),
            patch.object(
                self.backend.POSTGRES,
                "connect_factory",
                new=AsyncMock(return_value=connection),
            ),
        ):
            return await self.backend.log_chat(request(body))

    async def test_failed_response_retry_replays_one_user_message(self) -> None:
        connection = FakeConnection()
        first = await self.call(connection, self.body())
        second = await self.call(connection, self.body())
        self.assertEqual(first["id"], str(SUBMISSION))
        self.assertEqual(second["id"], str(SUBMISSION))
        self.assertTrue(second["replayed"])
        self.assertEqual(connection.insert_count, 1)

    async def test_submission_id_reuse_for_different_text_conflicts(self) -> None:
        connection = FakeConnection()
        await self.call(connection, self.body())
        response = await self.call(connection, self.body("Different intent."))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["detail"], "submission_conflict")
        self.assertEqual(connection.insert_count, 1)

    async def test_invalid_submission_id_fails_before_database(self) -> None:
        connection = FakeConnection()
        body = self.body()
        body["submission_id"] = "not-a-uuid"
        response = await self.call(connection, body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["detail"], "invalid_submission_id")
        self.assertEqual(connection.insert_count, 0)


if __name__ == "__main__":
    unittest.main()
