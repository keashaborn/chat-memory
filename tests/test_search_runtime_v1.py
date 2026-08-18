from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import Response

from seebx.adapters.search_audit import open_search_audit_session
from seebx.capabilities.search.runtime import (
    SearchAuditStoreUnavailableError,
    SearchRateLimitExceededError,
    SearchRuntimeConfigurationError,
    apply_search_no_store_headers,
    safe_search_error_code,
    search_postgres_dsn_from_env,
    search_runtime_settings_from_env,
)


class SearchRuntimeV1Tests(unittest.TestCase):
    def test_requires_database_and_server_secret_together(self) -> None:
        invalid_settings = (
            {},
            {"POSTGRES_DSN": "postgresql://local/search"},
            {"VS_SERVICE_TOKEN": "x" * 32},
            {
                "POSTGRES_DSN": "postgresql://local/search",
                "VS_SERVICE_TOKEN": "short",
            },
        )
        for environment in invalid_settings:
            with self.subTest(environment=sorted(environment)):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(SearchRuntimeConfigurationError):
                        search_runtime_settings_from_env()

    def test_returns_normalized_server_owned_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DSN": "  postgresql://local/search  ",
                "VS_SERVICE_TOKEN": "  " + "x" * 32 + "  ",
            },
            clear=True,
        ):
            settings = search_runtime_settings_from_env()

        self.assertEqual(
            settings.postgres_dsn,
            "postgresql://local/search",
        )
        self.assertEqual(settings.safety_secret, "x" * 32)

    def test_transcript_dsn_reader_is_independently_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(search_postgres_dsn_from_env(), "")

    def test_no_store_headers_are_shared(self) -> None:
        response = Response()
        apply_search_no_store_headers(response)

        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(
            response.headers["x-content-type-options"],
            "nosniff",
        )

    def test_capability_runtime_is_sql_free(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/search/runtime.py").read_text()
        self.assertNotIn("asyncpg", source)
        self.assertNotIn(".execute(", source)
        self.assertNotIn(".fetchval(", source)
        self.assertTrue((root / "seebx/adapters/search_audit.py").is_file())

    def test_error_codes_never_expose_unbounded_messages(self) -> None:
        self.assertEqual(
            safe_search_error_code(RuntimeError("bounded_code")),
            "bounded_code",
        )
        self.assertEqual(
            safe_search_error_code(RuntimeError("unsafe message with spaces")),
            "RuntimeError",
        )


class SearchAuditSessionV1Tests(unittest.IsolatedAsyncioTestCase):
    def _open_arguments(self) -> dict[str, object]:
        return {
            "postgres_dsn": "postgresql://local/search",
            "actor_user_id": uuid4(),
            "requests_per_minute": 5,
            "search_id": uuid4(),
            "request_id": "request-1",
            "query": "bounded query",
            "policy_version": "policy-v1",
            "topic": "current_news",
            "disposition": "search",
            "allowed_domains": ("example.com",),
        }

    async def test_open_finish_and_close_share_one_owned_connection(self) -> None:
        connection = AsyncMock()
        connect = AsyncMock(return_value=connection)
        acquire = AsyncMock(return_value=True)
        start = AsyncMock()
        finish = AsyncMock()
        arguments = self._open_arguments()

        with (
            patch(
                "seebx.adapters.search_audit.asyncpg.connect",
                connect,
            ),
            patch(
                "seebx.adapters.search_audit.acquire_trusted_web_rate_limit_v1",
                acquire,
            ),
            patch(
                "seebx.adapters.search_audit.start_trusted_web_audit_v1",
                start,
            ),
            patch(
                "seebx.adapters.search_audit.finish_trusted_web_audit_v1",
                finish,
            ),
        ):
            session = await open_search_audit_session(**arguments)
            await session.finish(status="completed", latency_ms=11)
            await session.close()
            await session.close()

        connect.assert_awaited_once_with(
            "postgresql://local/search",
            command_timeout=15,
        )
        acquire.assert_awaited_once_with(
            connection,
            actor_user_id=arguments["actor_user_id"],
            requests_per_minute=5,
        )
        self.assertEqual(
            start.await_args.kwargs["search_id"],
            arguments["search_id"],
        )
        self.assertEqual(
            start.await_args.kwargs["query_hash"],
            "f1ade0a4523592cbbc760a1c2eb905e507e99d9cfbc264252d1d2bdc5ca99a73",
        )
        finish.assert_awaited_once_with(
            connection,
            search_id=arguments["search_id"],
            status="completed",
            latency_ms=11,
            provider_response_id=None,
            sources=(),
            cited_sources=(),
            admitted_sources=(),
            rejected_source_reasons=(),
            error_code=None,
        )
        connection.close.assert_awaited_once_with()

    async def test_connect_failure_has_a_stable_domain_error(self) -> None:
        connect = AsyncMock(side_effect=OSError("database unavailable"))
        with patch(
            "seebx.adapters.search_audit.asyncpg.connect",
            connect,
        ):
            with self.assertRaises(SearchAuditStoreUnavailableError):
                await open_search_audit_session(**self._open_arguments())

    async def test_rate_limit_denial_closes_without_starting_an_audit(self) -> None:
        connection = AsyncMock()
        start = AsyncMock()
        with (
            patch(
                "seebx.adapters.search_audit.asyncpg.connect",
                AsyncMock(return_value=connection),
            ),
            patch(
                "seebx.adapters.search_audit.acquire_trusted_web_rate_limit_v1",
                AsyncMock(return_value=False),
            ),
            patch(
                "seebx.adapters.search_audit.start_trusted_web_audit_v1",
                start,
            ),
        ):
            with self.assertRaises(SearchRateLimitExceededError):
                await open_search_audit_session(**self._open_arguments())

        start.assert_not_awaited()
        connection.close.assert_awaited_once_with()

    async def test_audit_start_failure_closes_and_preserves_the_error(self) -> None:
        connection = AsyncMock()
        with (
            patch(
                "seebx.adapters.search_audit.asyncpg.connect",
                AsyncMock(return_value=connection),
            ),
            patch(
                "seebx.adapters.search_audit.acquire_trusted_web_rate_limit_v1",
                AsyncMock(return_value=True),
            ),
            patch(
                "seebx.adapters.search_audit.start_trusted_web_audit_v1",
                AsyncMock(side_effect=ValueError("audit insert failed")),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "audit insert failed"):
                await open_search_audit_session(**self._open_arguments())

        connection.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
