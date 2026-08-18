from __future__ import annotations

from contextlib import asynccontextmanager
import importlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")


def request(actor: str | None = OWNER) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if actor is not None:
        headers.append((b"x-vs-actor-user-id", actor.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        }
    )


class ThreadHttpIdentityTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_requested_owner_requires_canonical_actor_authority(self) -> None:
        authority = AsyncMock(return_value=OWNER)
        with patch.object(self.backend, "require_actor", new=authority):
            denied, actor = await self.backend._require_actor_for_user(
                request(),
                OWNER,
            )

        self.assertIsNone(denied)
        self.assertEqual(actor, OWNER)
        authority.assert_awaited_once()
        self.assertEqual(authority.await_args.args[1], OWNER)

    async def test_requested_owner_preserves_authority_refusal(self) -> None:
        authority = AsyncMock(
            side_effect=HTTPException(
                status_code=401,
                detail="missing_or_invalid_supabase_bearer",
            )
        )
        with patch.object(self.backend, "require_actor", new=authority):
            denied, actor = await self.backend._require_actor_for_user(
                request(),
                OWNER,
            )

        self.assertIsNone(actor)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            json.loads(denied.body),
            {
                "status": "unauthorized",
                "detail": "missing_or_invalid_supabase_bearer",
            },
        )

    async def test_thread_lookup_uses_verified_actor_before_database(self) -> None:
        observed: list[UUID] = []
        connection = object()

        @asynccontextmanager
        async def owner_connection(owner):
            observed.append(UUID(str(owner)))
            yield connection

        authority = AsyncMock(return_value=OWNER)
        lookup = AsyncMock(return_value=True)
        with (
            patch.object(
                self.backend,
                "require_request_actor",
                new=authority,
            ),
            patch.object(
                self.backend.POSTGRES,
                "owner_connection",
                new=owner_connection,
            ),
            patch.object(
                self.backend,
                "thread_belongs_to_owner",
                new=lookup,
            ),
        ):
            denied, actor = await self.backend._require_actor_for_thread(
                request(),
                THREAD,
            )

        self.assertIsNone(denied)
        self.assertEqual(actor, OWNER)
        self.assertEqual(observed, [UUID(OWNER)])
        authority.assert_awaited_once()
        lookup.assert_awaited_once_with(
            connection,
            owner_user_id=UUID(OWNER),
            thread_id=THREAD,
        )

    async def test_thread_authority_failure_prevents_database_access(self) -> None:
        authority = AsyncMock(
            side_effect=HTTPException(
                status_code=401,
                detail="invalid_supabase_access_token",
            )
        )

        def database_bomb(*args, **kwargs):
            raise AssertionError("database reached before actor authority")

        with (
            patch.object(
                self.backend,
                "require_request_actor",
                new=authority,
            ),
            patch.object(
                self.backend.POSTGRES,
                "owner_connection",
                new=database_bomb,
            ),
        ):
            denied, actor = await self.backend._require_actor_for_thread(
                request(),
                THREAD,
            )

        self.assertIsNone(actor)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            json.loads(denied.body)["detail"],
            "invalid_supabase_access_token",
        )


if __name__ == "__main__":
    unittest.main()
