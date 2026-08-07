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
        self.assertIn("generation_config = OpenAIChatGenerationConfigV1()", source)
        self.assertIn("generation_config=generation_config", source)
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

    def test_search_capability_is_header_derived_and_not_public_payload(self) -> None:
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("require_web_search_actor_v1(", source)
        self.assertIn("SearchCapabilityManifestV1.create(", source)
        self.assertIn("req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER)", source)
        self.assertNotIn("payload.search_capability", source)
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "search_capability_manifest": {
                        "available_routes": ["unbounded_web"],
                    },
                }
            )

    def test_response_preferences_are_owner_loaded_and_not_public_payload(self) -> None:
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("load_assistant_response_preferences_v1(conn, owner)", source)
        self.assertIn(
            "assistant_response_preferences=assistant_response_preferences",
            source,
        )
        self.assertNotIn("payload.assistant_name", source)
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "assistant_name": "Sage",
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
                "attachment_ids": [],
                "attachment_message_id": None,
            },
        )

    def test_attachment_transport_fields_are_uuid_bound_and_unique(self) -> None:
        attachment_id = "1a8beae3-58e5-4fb7-8f64-4fbb2bddcb73"
        message_id = "05e79a7d-1e58-4cf4-95d6-b06b46f8898d"
        value = ResseResponseRequestV1.model_validate(
            {
                "user_id": str(ACTOR),
                "message": "Review the attachment.",
                "thread_id": "735e1cf0-5a02-456c-a035-5597b010c7aa",
                "attachment_ids": [attachment_id],
                "attachment_message_id": message_id,
            }
        )
        self.assertEqual([str(item) for item in value.attachment_ids], [attachment_id])
        self.assertEqual(str(value.attachment_message_id), message_id)
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": str(ACTOR),
                    "message": "Review it.",
                    "thread_id": "735e1cf0-5a02-456c-a035-5597b010c7aa",
                    "attachment_ids": [attachment_id, attachment_id],
                    "attachment_message_id": message_id,
                }
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
