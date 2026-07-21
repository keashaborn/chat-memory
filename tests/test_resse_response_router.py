from __future__ import annotations

import unittest
from uuid import UUID

from pydantic import ValidationError

from rag_engine.resse_response_router import ResseResponseRequestV1


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class ResseResponseRouterTests(unittest.TestCase):
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
        value = ResseResponseRequestV1(
            user_id=ACTOR,
            message="Hello.",
            no_store=True,
        )
        self.assertIsNone(value.thread_id)
        self.assertEqual(
            value.model_dump(mode="json"),
            {
                "user_id": str(ACTOR),
                "message": "Hello.",
                "thread_id": None,
                "no_store": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
