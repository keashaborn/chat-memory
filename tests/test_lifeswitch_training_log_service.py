from __future__ import annotations

import json
import sys
import types
import unittest
import uuid

try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError:
    asyncpg_stub = types.ModuleType("asyncpg")

    class PostgresError(Exception):
        pass

    asyncpg_stub.PostgresError = PostgresError
    sys.modules["asyncpg"] = asyncpg_stub

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    fastapi_stub = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, *, status_code: int, detail: str, headers=None) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers

    fastapi_stub.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi_stub

from seebx.capabilities.training import logs as service


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.result = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    async def fetchval(self, sql: str, *args: object) -> uuid.UUID:
        self.calls.append((sql, args))
        return self.result


class TrainingLogServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_actor_is_bound_transaction_locally(self) -> None:
        conn = FakeConnection()
        await service.set_transaction_actor(
            conn,
            actor_user_id="11111111-1111-4111-8111-111111111111",
        )

        sql, args = conn.calls[-1]
        self.assertEqual(
            sql,
            "select set_config('app.user_id', $1::text, true)",
        )
        self.assertEqual(args, ("11111111-1111-4111-8111-111111111111",))

    async def test_create_training_serializes_compact_json_intent(self) -> None:
        conn = FakeConnection()
        intent = {
            "name": "Push A",
            "load_unit": "lb",
            "sets": [{"exercise_id": "bench", "weight": 100, "reps": 8}],
        }

        result = await service.create_training_session(
            conn,
            intent=intent,
            idempotency_key="training-create-1",
        )

        self.assertEqual(result, conn.result)
        sql, args = conn.calls[-1]
        self.assertIn("create_training_session($1::jsonb, $2::text)", sql)
        self.assertEqual(json.loads(str(args[0])), intent)
        self.assertNotIn(": ", str(args[0]))
        self.assertNotIn(", ", str(args[0]))
        self.assertEqual(args[1], "training-create-1")

    async def test_correction_and_void_call_only_protected_writers(self) -> None:
        conn = FakeConnection()
        source_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

        await service.correct_training_session(
            conn,
            training_session_id=source_id,
            intent={"name": "corrected"},
            idempotency_key="training-correct-1",
        )
        await service.void_training_session(
            conn,
            training_session_id=source_id,
            reason="duplicate",
        )
        await service.correct_conditioning_session(
            conn,
            conditioning_session_log_id=source_id,
            intent={"name": "walk", "distance_value": 2.5, "distance_unit": "mi"},
            idempotency_key="conditioning-correct-1",
        )
        await service.void_conditioning_session(
            conn,
            conditioning_session_log_id=source_id,
            reason="duplicate",
        )

        sql_text = "\n".join(sql for sql, _args in conn.calls)
        self.assertIn("correct_training_session", sql_text)
        self.assertIn("void_training_session", sql_text)
        self.assertIn("correct_conditioning_session", sql_text)
        self.assertIn("void_conditioning_session", sql_text)
        self.assertNotIn("insert into", sql_text.lower())
        self.assertNotIn("update ", sql_text.lower())
        self.assertNotIn("delete from", sql_text.lower())


if __name__ == "__main__":
    unittest.main()
