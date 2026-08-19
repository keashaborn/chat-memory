from __future__ import annotations

from contextlib import asynccontextmanager
import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
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
            cls.thread_routes = importlib.import_module(
                "seebx.capabilities.conversation.thread_routes"
            )
            cls.erasure_routes = importlib.import_module(
                "seebx.capabilities.conversation.erasure_routes"
            )

    def test_canonical_router_owns_exact_thread_lifecycle_surface(self) -> None:
        application = FastAPI()
        application.include_router(
            self.thread_routes.create_thread_lifecycle_router(
                self.backend.POSTGRES,
                title_client=None,
            )
        )
        actual = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in application.routes
            if route.path.startswith("/threads")
        }
        expected = {
            ("/threads/new", ("POST",)),
            ("/threads/list/{user_id}", ("GET",)),
            ("/threads/active/{user_id}", ("GET",)),
            ("/threads/active", ("POST",)),
            ("/threads/active/{user_id}", ("DELETE",)),
            ("/threads/{thread_id}/messages", ("GET",)),
            ("/threads/{thread_id}/rename", ("POST",)),
            ("/threads/{thread_id}/pin", ("POST",)),
            ("/threads/{thread_id}/auto-title", ("POST",)),
            ("/threads/{thread_id}/archive", ("POST",)),
        }
        self.assertEqual(actual, expected)

    def test_missing_bearer_fails_before_thread_database_access(self) -> None:
        application = FastAPI()
        application.include_router(
            self.thread_routes.create_thread_lifecycle_router(
                self.backend.POSTGRES,
                title_client=None,
            )
        )

        def database_bomb(*args, **kwargs):
            raise AssertionError("database reached before actor authority")

        with patch.object(
            self.backend.POSTGRES,
            "owner_connection",
            new=database_bomb,
        ):
            response = TestClient(application).post(
                "/threads/new",
                headers={"x-vs-actor-user-id": OWNER},
                json={"user_id": OWNER, "title": "Synthetic thread"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {
                "status": "unauthorized",
                "detail": "missing_or_invalid_supabase_bearer",
            },
        )

    async def test_requested_owner_requires_canonical_actor_authority(self) -> None:
        authority = AsyncMock(return_value=OWNER)
        with patch.object(self.thread_routes, "require_actor", new=authority):
            denied, actor = await self.thread_routes._require_actor_for_user(
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
        with patch.object(self.thread_routes, "require_actor", new=authority):
            denied, actor = await self.thread_routes._require_actor_for_user(
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
                self.thread_routes,
                "require_request_actor",
                new=authority,
            ),
            patch.object(
                self.backend.POSTGRES,
                "owner_connection",
                new=owner_connection,
            ),
            patch.object(
                self.thread_routes,
                "thread_belongs_to_owner",
                new=lookup,
            ),
        ):
            denied, actor = await self.thread_routes._require_actor_for_thread(
                request(),
                THREAD,
                self.backend.POSTGRES,
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
                self.thread_routes,
                "require_request_actor",
                new=authority,
            ),
            patch.object(
                self.backend.POSTGRES,
                "owner_connection",
                new=database_bomb,
            ),
        ):
            denied, actor = await self.thread_routes._require_actor_for_thread(
                request(),
                THREAD,
                self.backend.POSTGRES,
            )

        self.assertIsNone(actor)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            json.loads(denied.body)["detail"],
            "invalid_supabase_access_token",
        )

    def test_canonical_router_owns_exact_erasure_surface(self) -> None:
        application = FastAPI()
        application.include_router(
            self.erasure_routes.create_conversation_erasure_router(
                self.backend.CONVERSATION_ERASURE
            )
        )
        actual = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in application.routes
            if route.path.startswith(("/chat-history", "/memory", "/user"))
            or route.path in {
                "/threads/{thread_id}",
                "/threads/{thread_id}/messages/{message_id}/truncate",
            }
        }
        expected = {
            ("/chat-history/clear", ("POST",)),
            ("/memory/chat-and-zep/clear", ("DELETE",)),
            (
                "/threads/{thread_id}/messages/{message_id}/truncate",
                ("DELETE",),
            ),
            ("/threads/{thread_id}", ("DELETE",)),
            ("/user/{user_id}/data", ("DELETE",)),
            ("/user/{user_id}/recent", ("DELETE",)),
        }
        self.assertEqual(actual, expected)

    def test_history_clear_identity_failure_prevents_erasure(self) -> None:
        authority = AsyncMock(
            side_effect=HTTPException(
                status_code=401,
                detail="invalid_supabase_access_token",
            )
        )
        erasure = AsyncMock()
        application = FastAPI()
        application.include_router(
            self.erasure_routes.create_conversation_erasure_router(
                self.backend.CONVERSATION_ERASURE
            )
        )
        with (
            patch.object(
                self.erasure_routes,
                "require_verified_supabase_request_identity",
                new=authority,
            ),
            patch.object(
                self.backend.CONVERSATION_ERASURE,
                "clear_history",
                new=erasure,
            ),
        ):
            response = TestClient(application).post(
                "/chat-history/clear",
                headers={"x-vs-actor-user-id": OWNER},
                json={
                    "scope": "all",
                    "confirmation": "CLEAR CHAT HISTORY",
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "invalid_supabase_access_token",
        )
        erasure.assert_not_awaited()

    def test_full_erasure_uses_verified_supabase_owner(self) -> None:
        authority = AsyncMock(
            return_value=SimpleNamespace(actor_user_id=OWNER)
        )
        erasure = AsyncMock(return_value=SimpleNamespace(
            operation_id=UUID("6240822d-ac9a-4096-95aa-e2b24d36ef50"),
            deleted_message_count=2,
            deleted_thread_count=1,
            deleted_outbox_count=0,
            receipt_sha256="a" * 64,
            completed_at="2026-08-18T00:00:00Z",
        ))
        application = FastAPI()
        application.include_router(
            self.erasure_routes.create_conversation_erasure_router(
                self.backend.CONVERSATION_ERASURE
            )
        )
        with (
            patch.object(
                self.erasure_routes,
                "require_verified_supabase_request_identity",
                new=authority,
            ),
            patch.object(
                self.backend.CONVERSATION_ERASURE,
                "clear_all_chat_and_memory",
                new=erasure,
            ),
        ):
            response = TestClient(application).delete(
                "/memory/chat-and-zep/clear",
                headers={
                    "authorization": "Bearer synthetic",
                    "x-vs-actor-user-id": OWNER,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        authority.assert_awaited_once()
        erasure.assert_awaited_once()
        self.assertEqual(
            erasure.await_args.kwargs["owner_user_id"],
            UUID(OWNER),
        )


if __name__ == "__main__":
    unittest.main()
