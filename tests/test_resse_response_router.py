from __future__ import annotations

import unittest
from pathlib import Path
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from rag_engine.resse_response_router import (
    ResseResponseRequestV1,
    apply_no_store_headers,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
ROOT = Path(__file__).resolve().parents[1]


class ResseResponseRouterTests(unittest.TestCase):
    def test_normal_chat_generation_is_backend_owned(self) -> None:
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("generation_config=OpenAIChatGenerationConfigV1()", source)
        self.assertNotIn("OPENAI_CHAT_MODEL", source)
        self.assertNotIn("normalize_chat_model", source)

    def test_public_request_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Explain Fractal Monism.",
                    "response_mode": "FM_EXPLICIT",
                    "fm_level": "EXPLICIT",
                    "vantage_id": "RESSE",
                    "model": "gpt-5.1",
                }
            )

    def test_public_request_accepts_only_transport_fields(self) -> None:
        value = ResseResponseRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"message":"Hello.","thread_id":null,"no_store":true}'
        )
        self.assertIsNone(value.thread_id)
        self.assertEqual(
            value.model_dump(mode="json"),
            {
                "user_id": str(ACTOR),
                "message": "Hello.",
                "thread_id": None,
                "no_store": True,
                "include_inspection": False,
            },
        )

    def test_public_request_keeps_non_uuid_transport_fields_strict(self) -> None:
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate_json(
                '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
                '"message":"Hello.","no_store":"true"}'
            )

    def test_no_store_response_headers_are_explicit(self) -> None:
        response = Response()
        apply_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")


if __name__ == "__main__":
    unittest.main()
