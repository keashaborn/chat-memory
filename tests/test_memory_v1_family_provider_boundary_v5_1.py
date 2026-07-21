from __future__ import annotations

import json
import unittest

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalProviderAdapterError,
    _deterministic_policy_packet,
    _structured_result,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    sha256_text,
)


class FamilyProviderBoundaryV51Test(unittest.TestCase):
    @staticmethod
    def source(text: str) -> TrustedExtractionSource:
        return TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=sha256_text(text),
            source_recorded_at="2026-07-21T12:00:00Z",
            content=text,
        )

    def test_pet_profile_weight_is_not_structured_application_data(self) -> None:
        text = (
            "After Neko died, I got a white male Maine coon cat. "
            "He’s currently about 18 pounds. He is not deaf."
        )
        self.assertIsNone(_deterministic_policy_packet(self.source(text)))

    def test_training_weight_remains_structured_application_data(self) -> None:
        text = "I completed three squat sets at 225 pounds."
        packet, code = _deterministic_policy_packet(self.source(text)) or (None, None)
        self.assertIsNotNone(packet)
        self.assertEqual(code, "structured_domain")

    def test_markdown_fenced_json_is_normalized_deterministically(self) -> None:
        payload = {"entity_mentions": [], "observations": [], "deferrals": []}
        result = _structured_result(
            {
                "id": "local-test",
                "model": "qwen3-8b-local-extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "```json\n" + json.dumps(payload) + "\n```"
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        self.assertEqual(result.parsed, payload)
        self.assertEqual(
            result.content_normalization, "markdown_json_fence_removed"
        )

    def test_truncated_json_is_retryable_before_json_parsing(self) -> None:
        with self.assertRaises(LocalProviderAdapterError) as caught:
            _structured_result(
                {
                    "model": "qwen3-8b-local-extractor",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "{\"observations\":["},
                        }
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "local_incomplete_response")
        self.assertTrue(caught.exception.retryable)

    def test_stopped_malformed_json_remains_rejected(self) -> None:
        with self.assertRaises(LocalProviderAdapterError) as caught:
            _structured_result(
                {
                    "model": "qwen3-8b-local-extractor",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "not-json"},
                        }
                    ],
                }
            )
        self.assertEqual(
            caught.exception.code, "local_structured_content_invalid"
        )
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
