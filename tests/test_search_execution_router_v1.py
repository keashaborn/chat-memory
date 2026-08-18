from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from seebx.adapters.search_transcript import (
    SearchTranscriptStoreUnavailableError,
    persist_search_exchange_with_dsn,
)
from seebx.capabilities.search.execution import (
    SearchExecutionRequestV1,
    _source_policy_violation_headers,
    execute_search_plan_v1,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class SearchExecutionRouterV1Tests(unittest.TestCase):
    def test_search_capabilities_contain_no_direct_postgres_effects(self) -> None:
        search_root = (
            Path(__file__).resolve().parents[1]
            / "seebx"
            / "capabilities"
            / "search"
        )
        source = "\n".join(path.read_text() for path in search_root.glob("*.py"))
        self.assertNotIn("import asyncpg", source)
        self.assertNotIn("asyncpg.connect", source)
        self.assertNotIn(".fetchrow(", source)
        self.assertNotIn(".execute(", source)

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

    def persistent_request(self) -> tuple[SearchExecutionRequestV1, Request]:
        payload = SearchExecutionRequestV1.model_validate(
            {
                "user_id": str(ACTOR),
                "thread_id": "d776c8ef-7f3d-45b2-8820-4be87b7ca19d",
                "query": "What happened with OpenAI today?",
                "channel": "text",
                "persist_transcript": True,
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
        request.state.request_id = "transcript-store-test"
        return payload, request

    def searched_provider_result(self) -> dict[str, object]:
        return {
            "searched": True,
            "answer": "A sourced answer.",
            "search_id": "49c59ba0-e188-40f8-932d-51fa6b84e157",
            "sources": [{"url": "https://status.openai.com"}],
            "provider_consulted_source_count": 1,
        }

    async def test_missing_transcript_dsn_preserves_public_503(self) -> None:
        payload, request = self.persistent_request()

        with (
            patch(
                "seebx.capabilities.search.execution."
                "require_web_search_actor_v1",
                new=AsyncMock(return_value=str(ACTOR)),
            ),
            patch(
                "seebx.capabilities.search.execution.current_news_query",
                new=AsyncMock(return_value=self.searched_provider_result()),
            ),
            patch(
                "seebx.capabilities.search.execution."
                "search_postgres_dsn_from_env",
                return_value=None,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await execute_search_plan_v1(payload, request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "search_transcript_store_unconfigured",
        )

    async def test_unavailable_transcript_store_preserves_public_503(
        self,
    ) -> None:
        payload, request = self.persistent_request()

        with (
            patch(
                "seebx.capabilities.search.execution."
                "require_web_search_actor_v1",
                new=AsyncMock(return_value=str(ACTOR)),
            ),
            patch(
                "seebx.capabilities.search.execution.current_news_query",
                new=AsyncMock(return_value=self.searched_provider_result()),
            ),
            patch(
                "seebx.capabilities.search.execution."
                "search_postgres_dsn_from_env",
                return_value="postgresql://search-runtime",
            ),
            patch(
                "seebx.capabilities.search.execution."
                "persist_search_exchange_with_dsn",
                new=AsyncMock(
                    side_effect=SearchTranscriptStoreUnavailableError(
                        "search_transcript_store_unavailable"
                    )
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await execute_search_plan_v1(payload, request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "search_transcript_store_unavailable",
        )


class SearchTranscriptAdapterTests(unittest.IsolatedAsyncioTestCase):
    def persistence_arguments(self) -> dict[str, object]:
        return {
            "postgres_dsn": "postgresql://search-runtime",
            "owner_user_id": ACTOR,
            "thread_id": UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d"),
            "request_id": "request-web-001",
            "query": "What happened with OpenAI today?",
            "answer": "A sourced answer.",
            "search_id": UUID("49c59ba0-e188-40f8-932d-51fa6b84e157"),
            "route": "current_news",
            "policy_version": "search_decision_v1_2",
            "decision": "live",
            "cited_sources": [{"url": "https://status.openai.com"}],
            "admitted_sources": [{"url": "https://status.openai.com"}],
            "consulted_source_count": 1,
        }

    async def test_adapter_connects_persists_and_closes(self) -> None:
        connection = AsyncMock()
        answer_id = uuid4()
        connect = AsyncMock(return_value=connection)
        persist = AsyncMock(return_value=answer_id)

        with (
            patch(
                "seebx.adapters.search_transcript.asyncpg.connect",
                new=connect,
            ),
            patch(
                "seebx.adapters.search_transcript.persist_search_exchange",
                new=persist,
            ),
        ):
            result = await persist_search_exchange_with_dsn(
                **self.persistence_arguments()
            )

        self.assertEqual(result, answer_id)
        connect.assert_awaited_once_with(
            "postgresql://search-runtime",
            command_timeout=15,
        )
        persist.assert_awaited_once()
        self.assertIs(persist.await_args.args[0], connection)
        connection.close.assert_awaited_once_with()

    async def test_adapter_closes_after_persistence_failure(self) -> None:
        connection = AsyncMock()
        persist = AsyncMock(side_effect=RuntimeError("write failed"))

        with (
            patch(
                "seebx.adapters.search_transcript.asyncpg.connect",
                new=AsyncMock(return_value=connection),
            ),
            patch(
                "seebx.adapters.search_transcript.persist_search_exchange",
                new=persist,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                await persist_search_exchange_with_dsn(
                    **self.persistence_arguments()
                )

        connection.close.assert_awaited_once_with()

    async def test_connection_failure_has_stable_adapter_error(self) -> None:
        with patch(
            "seebx.adapters.search_transcript.asyncpg.connect",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ):
            with self.assertRaisesRegex(
                SearchTranscriptStoreUnavailableError,
                "search_transcript_store_unavailable",
            ):
                await persist_search_exchange_with_dsn(
                    **self.persistence_arguments()
                )


if __name__ == "__main__":
    unittest.main()
