from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from seebx.adapters import openai as adapter


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        adapter._client_cache.clear()

    def tearDown(self) -> None:
        adapter._client_cache.clear()

    def test_model_normalization_is_openai_only_and_allowlisted(self) -> None:
        self.assertEqual(adapter.normalize_chat_model("gpt-4.1-mini"), "gpt-4.1-mini")
        self.assertEqual(
            adapter.normalize_chat_model("openrouter:some-model", "gpt-4.1"),
            "gpt-4.1",
        )
        self.assertEqual(adapter.normalize_chat_model("unknown", "gpt-4o"), "gpt-4o")

    def test_missing_api_key_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing OPENAI_API_KEY"):
                adapter.get_openai_client()

    def test_optional_client_is_none_without_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(adapter.get_optional_openai_client())

    def test_optional_client_uses_shared_cache_when_configured(self) -> None:
        shared_client = object()
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-secret"},
            clear=True,
        ):
            with patch.object(adapter, "OpenAI", return_value=shared_client):
                self.assertIs(adapter.get_optional_openai_client(), shared_client)
                self.assertIs(adapter.get_openai_client(), shared_client)

    def test_non_openai_provider_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Only OpenAI provider is enabled"):
            adapter._get_client("other")

    def test_client_is_cached_without_exposing_key_in_cache_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-secret",
                "OPENAI_BASE_URL": "https://api.openai.test/v1",
            },
            clear=True,
        ):
            with patch.object(adapter, "OpenAI", return_value=object()) as constructor:
                first = adapter.get_openai_client()
                second = adapter.get_openai_client()

        self.assertIs(first, second)
        constructor.assert_called_once_with(
            api_key="test-secret",
            base_url="https://api.openai.test/v1",
        )
        cache_key = next(iter(adapter._client_cache))
        self.assertEqual(cache_key[0], "https://api.openai.test/v1")
        self.assertNotIn("test-secret", repr(cache_key))

    def test_key_rotation_creates_a_new_client(self) -> None:
        first_client = object()
        second_client = object()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "first-key"}, clear=True):
            with patch.object(
                adapter,
                "OpenAI",
                side_effect=[first_client, second_client],
            ) as constructor:
                first = adapter.get_openai_client()
                os.environ["OPENAI_API_KEY"] = "second-key"
                second = adapter.get_openai_client()

        self.assertIs(first, first_client)
        self.assertIs(second, second_client)
        self.assertEqual(constructor.call_count, 2)
        self.assertNotIn("first-key", repr(adapter._client_cache))
        self.assertNotIn("second-key", repr(adapter._client_cache))


if __name__ == "__main__":
    unittest.main()
