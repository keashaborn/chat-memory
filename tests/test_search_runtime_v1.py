from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import Response

from seebx.capabilities.search.runtime import (
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

    def test_error_codes_never_expose_unbounded_messages(self) -> None:
        self.assertEqual(
            safe_search_error_code(RuntimeError("bounded_code")),
            "bounded_code",
        )
        self.assertEqual(
            safe_search_error_code(RuntimeError("unsafe message with spaces")),
            "RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()
