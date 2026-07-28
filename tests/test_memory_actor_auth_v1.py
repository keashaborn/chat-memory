from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
import uuid

from starlette.requests import Request

from rag_engine.memory_actor_auth_v1 import (
    TEXT_AUTHORITY,
    VOICE_AUTHORITY,
    memory_actor_authority_v1,
    require_memory_actor_v1,
)


OWNER = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))


def request(*, actor: str = OWNER) -> Request:
    headers = [(b"x-vs-actor-user-id", actor.encode())]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


class MemoryActorAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    @patch(
        "rag_engine.memory_actor_auth_v1.require_verified_supabase_actor",
        new_callable=AsyncMock,
    )
    @patch("rag_engine.memory_actor_auth_v1.voice_turn_id_from_request")
    async def test_text_requires_verified_supabase_actor(
        self,
        turn_id,
        verified_actor,
    ) -> None:
        turn_id.return_value = None
        verified_actor.return_value = OWNER
        req = request()
        self.assertEqual(await require_memory_actor_v1(req, OWNER), OWNER)
        self.assertEqual(memory_actor_authority_v1(req), TEXT_AUTHORITY)
        verified_actor.assert_awaited_once_with(req, OWNER)

    @patch(
        "rag_engine.memory_actor_auth_v1.require_active_voice_session",
        new_callable=AsyncMock,
    )
    @patch("rag_engine.memory_actor_auth_v1.voice_turn_id_from_request")
    async def test_voice_requires_active_owner_bound_lease(
        self,
        turn_id,
        active_session,
    ) -> None:
        turn_id.return_value = uuid.uuid4()
        req = request()
        self.assertEqual(await require_memory_actor_v1(req, OWNER), OWNER)
        self.assertEqual(memory_actor_authority_v1(req), VOICE_AUTHORITY)
        active_session.assert_awaited_once_with(req, OWNER)


if __name__ == "__main__":
    unittest.main()
