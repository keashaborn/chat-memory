from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException, Request

from seebx.capabilities.search.execution import (
    SearchExecutionRequestV1,
    _source_policy_violation_headers,
    execute_search_plan_v1,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class SearchExecutionRouterV1Tests(unittest.TestCase):
    def test_legacy_search_router_wrappers_are_retired(self) -> None:
        self.assertEqual(
            execute_search_plan_v1.__module__,
            "seebx.capabilities.search.execution",
        )
        repository = Path(__file__).resolve().parents[1]
        for relative_path in (
            "rag_engine/current_news_router.py",
            "rag_engine/search_execution_router_v1.py",
            "rag_engine/trusted_web_router.py",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertFalse((repository / relative_path).exists())

    def test_current_news_policy_violation_preserves_route(self) -> None:
        headers = _source_policy_violation_headers(
            route="current_news",
            exc=HTTPException(
                status_code=502,
                detail="current_news_source_policy_violation",
            ),
        )

        self.assertIsNotNone(headers)
        assert headers is not None
        self.assertEqual(headers["X-VS-Search-Route"], "current_news")
        self.assertEqual(headers["X-VS-Web-Searched"], "1")
        self.assertEqual(
            headers["cache-control"],
            "private, no-store, max-age=0, must-revalidate",
        )

    def test_unrelated_upstream_error_is_not_rewritten(self) -> None:
        self.assertIsNone(
            _source_policy_violation_headers(
                route="current_news",
                exc=HTTPException(
                    status_code=503,
                    detail="current_news_provider_unavailable",
                ),
            )
        )


class SearchExecutionRouterV1IntegrationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_source_policy_failure_reaches_bff_with_route_headers(
        self,
    ) -> None:
        payload = SearchExecutionRequestV1.model_validate(
            {
                "user_id": str(ACTOR),
                "query": "What happened with OpenAI today?",
                "channel": "text",
                "persist_transcript": False,
            }
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/search/execute",
                "headers": [],
            }
        )
        request.state.request_id = "citation-repair-test"
        current_news = AsyncMock(
            side_effect=HTTPException(
                status_code=502,
                detail="current_news_source_policy_violation",
            )
        )

        with (
            patch(
                "seebx.capabilities.search.execution."
                "require_web_search_actor_v1",
                new=AsyncMock(return_value=str(ACTOR)),
            ),
            patch(
                "seebx.capabilities.search.execution.current_news_query",
                new=current_news,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await execute_search_plan_v1(payload, request)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            raised.exception.headers["X-VS-Search-Route"],
            "current_news",
        )
        self.assertEqual(
            raised.exception.headers["X-VS-Web-Searched"],
            "1",
        )
        self.assertEqual(
            request.state.server_search_budget_v1.max_searches,
            4,
        )
        self.assertEqual(
            request.state.server_search_budget_v1.max_sources,
            10,
        )


if __name__ == "__main__":
    unittest.main()
