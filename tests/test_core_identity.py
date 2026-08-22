from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException
from starlette.requests import Request

from seebx.adapters.supabase import VerifiedSupabaseIdentity
from seebx.core.identity import (
    TEXT_AUTHORITY,
    VOICE_AUTHORITY,
    actor_authority,
    require_actor,
    require_request_actor,
    require_actor_context,
    require_verified_actor_matches_owner,
)


OWNER = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
SESSION = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))


def request(*, actor: str = OWNER) -> Request:
    headers = [(b"x-vs-actor-user-id", actor.encode())]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


class ActorAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    @patch(
        "seebx.core.identity.require_verified_supabase_actor",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_text_requires_verified_supabase_actor(
        self,
        turn_id,
        verified_actor,
    ) -> None:
        turn_id.return_value = None
        verified_actor.return_value = OWNER
        req = request()
        self.assertEqual(await require_actor(req, OWNER), OWNER)
        self.assertEqual(actor_authority(req), TEXT_AUTHORITY)
        verified_actor.assert_awaited_once_with(req, OWNER)

    @patch(
        "seebx.core.identity.require_verified_supabase_request_identity",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_text_request_actor_uses_verified_supabase_identity(
        self,
        turn_id,
        verified_identity,
    ) -> None:
        turn_id.return_value = None
        verified_identity.return_value = VerifiedSupabaseIdentity(
            actor_user_id=OWNER,
            session_id=SESSION,
            authentication_manifest_sha256="a" * 64,
        )
        req = request()
        self.assertEqual(await require_request_actor(req), OWNER)
        verified_identity.assert_awaited_once_with(req)

    @patch(
        "seebx.core.identity.require_verified_supabase_identity",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_text_context_preserves_verified_supabase_manifest(
        self,
        turn_id,
        verified_identity,
    ) -> None:
        turn_id.return_value = None
        verified_identity.return_value = VerifiedSupabaseIdentity(
            actor_user_id=OWNER,
            session_id=SESSION,
            authentication_manifest_sha256="a" * 64,
        )
        req = request()
        context = await require_actor_context(req, OWNER)
        self.assertEqual(context.owner_user_id, uuid.UUID(OWNER))
        self.assertEqual(context.session_id, uuid.UUID(SESSION))
        self.assertEqual(context.authentication_manifest_sha256, "a" * 64)
        self.assertEqual(context.authority, TEXT_AUTHORITY)
        verified_identity.assert_awaited_once_with(req, OWNER)

    @patch(
        "seebx.core.identity.require_active_voice_session",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_voice_requires_active_owner_bound_lease(
        self,
        turn_id,
        active_session,
    ) -> None:
        turn_id.return_value = uuid.uuid4()
        active_session.return_value = uuid.UUID(SESSION)
        req = request()
        self.assertEqual(await require_actor(req, OWNER), OWNER)
        self.assertEqual(actor_authority(req), VOICE_AUTHORITY)
        active_session.assert_awaited_once_with(req, OWNER)

    @patch(
        "seebx.core.identity.require_active_voice_session",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_voice_request_actor_requires_active_owner_bound_lease(
        self,
        turn_id,
        active_session,
    ) -> None:
        turn_id.return_value = uuid.uuid4()
        active_session.return_value = uuid.UUID(SESSION)
        req = request()
        self.assertEqual(await require_request_actor(req), OWNER)
        active_session.assert_awaited_once_with(req, OWNER)

    @patch(
        "seebx.core.identity.require_active_voice_session",
        new_callable=AsyncMock,
    )
    @patch("seebx.core.identity.voice_turn_id_from_request")
    async def test_voice_context_binds_owner_and_session(
        self,
        turn_id,
        active_session,
    ) -> None:
        turn_id.return_value = uuid.uuid4()
        active_session.return_value = uuid.UUID(SESSION)
        context = await require_actor_context(request(), OWNER)
        self.assertEqual(context.owner_user_id, uuid.UUID(OWNER))
        self.assertEqual(context.session_id, uuid.UUID(SESSION))
        self.assertEqual(context.authority, VOICE_AUTHORITY)
        material = json.dumps(
            {
                "authority": VOICE_AUTHORITY,
                "owner_user_id": OWNER,
                "schema": "response-memory-actor-context-v1",
                "session_id": SESSION,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            context.authentication_manifest_sha256,
            hashlib.sha256(material).hexdigest(),
        )


class VerifiedDatabaseOwnerBindingTests(unittest.TestCase):
    def test_verified_actor_can_bind_to_database_derived_owner(self) -> None:
        self.assertEqual(
            require_verified_actor_matches_owner(OWNER, OWNER),
            OWNER,
        )

    def test_verified_actor_cannot_bind_to_another_owner(self) -> None:
        with self.assertRaises(HTTPException) as mismatch:
            require_verified_actor_matches_owner(OWNER, SESSION)
        self.assertEqual(mismatch.exception.status_code, 403)
        self.assertEqual(mismatch.exception.detail, "actor_owner_mismatch")


if __name__ == "__main__":
    unittest.main()
