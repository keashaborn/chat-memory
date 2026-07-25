from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from starlette.requests import Request

from rag_engine.supabase_actor_auth import (
    SupabaseAuthSettings,
    require_verified_supabase_actor,
)


ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_ACTOR = "9062eaa7-1105-49af-9308-44d06b378c4d"
SESSION_ID = "228cc24e-31de-4ad3-9b93-f48fb542e48f"
ISSUER = "https://example.supabase.co/auth/v1"
SETTINGS = SupabaseAuthSettings(
    issuer=ISSUER,
    jwks_url=f"{ISSUER}/.well-known/jwks.json",
)


class FakeJwksClient:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self.public_key)


class SupabaseActorAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()

    def token(self, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "authenticated",
            "exp": now + 300,
            "iat": now - 5,
            "sub": ACTOR,
            "role": "authenticated",
            "session_id": SESSION_ID,
            "is_anonymous": False,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="ES256",
            headers={"kid": "test-key"},
        )

    def request(
        self,
        token: str | None,
        asserted_actor: str | None = ACTOR,
    ) -> Request:
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append(
                (b"authorization", f"Bearer {token}".encode("ascii"))
            )
        if asserted_actor is not None:
            headers.append(
                (b"x-vs-actor-user-id", asserted_actor.encode("ascii"))
            )
        return Request({"type": "http", "headers": headers})

    def verify(
        self,
        request: Request,
        owner: str = ACTOR,
    ) -> str:
        with (
            patch.dict(
                "os.environ",
                {
                    "SUPABASE_ISSUER": SETTINGS.issuer,
                    "SUPABASE_JWKS_URL": SETTINGS.jwks_url,
                },
                clear=False,
            ),
            patch(
                "rag_engine.supabase_actor_auth._jwks_client",
                return_value=FakeJwksClient(self.public_key),
            ),
        ):
            return asyncio.run(
                require_verified_supabase_actor(request, owner)
            )

    def test_verified_subject_is_the_actor_authority(self) -> None:
        self.assertEqual(self.verify(self.request(self.token())), ACTOR)

    def test_forged_actor_header_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.verify(
                self.request(self.token(), asserted_actor=OTHER_ACTOR)
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "actor_assertion_mismatch")

    def test_body_owner_mismatch_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.verify(self.request(self.token()), owner=OTHER_ACTOR)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            caught.exception.detail,
            "supabase_actor_owner_mismatch",
        )

    def test_missing_original_bearer_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.verify(self.request(None))
        self.assertEqual(caught.exception.status_code, 401)

    def test_expired_or_anonymous_tokens_are_rejected(self) -> None:
        for token in (
            self.token(exp=int(time.time()) - 120),
            self.token(is_anonymous=True),
        ):
            with self.subTest(token=token[-8:]):
                with self.assertRaises(HTTPException) as caught:
                    self.verify(self.request(token))
                self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
