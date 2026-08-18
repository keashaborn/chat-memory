from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from seebx.capabilities.search.authorization import (
    require_web_search_actor_v1,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search/execute",
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii"))
                for key, value in headers.items()
            ],
        }
    )


class WebSearchActorAuthV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_common_executor_requires_internal_assertion(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await require_web_search_actor_v1(
                request({}),
                OWNER,
                internal_assertion_required=True,
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_text_assertion_binds_actor_to_owner(self) -> None:
        req = request(
            {
                "x-vs-web-search-authorization": (
                    "supabase_fresh_web_search_v1"
                ),
                "x-vs-actor-user-id": OWNER,
            }
        )
        actor = await require_web_search_actor_v1(
            req,
            OWNER,
            internal_assertion_required=True,
        )
        self.assertEqual(actor, OWNER)

    async def test_voice_assertion_also_requires_active_lease(self) -> None:
        req = request(
            {
                "x-vs-web-search-authorization": (
                    "supabase_fresh_voice_lease_v1"
                ),
                "x-vs-actor-user-id": OWNER,
            }
        )
        with patch(
            "seebx.capabilities.search.authorization.require_active_voice_session",
            AsyncMock(return_value="lease"),
        ) as active_lease:
            actor = await require_web_search_actor_v1(
                req,
                OWNER,
                internal_assertion_required=True,
            )
        self.assertEqual(actor, OWNER)
        active_lease.assert_awaited_once_with(req, OWNER)

    async def test_invalid_assertion_fails_closed(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await require_web_search_actor_v1(
                request(
                    {
                        "x-vs-web-search-authorization": "browser_claimed",
                        "x-vs-actor-user-id": OWNER,
                    }
                ),
                OWNER,
                internal_assertion_required=True,
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_legacy_authorization_module_is_retired(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse(
            (repository / "rag_engine/web_search_actor_auth_v1.py").exists()
        )


if __name__ == "__main__":
    unittest.main()
