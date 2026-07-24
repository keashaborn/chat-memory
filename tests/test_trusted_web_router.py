from __future__ import annotations

import unittest
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from rag_engine.trusted_web_router import (
    TrustedWebRequestV1,
    apply_trusted_web_no_store_headers,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class TrustedWebRouterTests(unittest.TestCase):
    def test_public_contract_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            TrustedWebRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "query": "Does creatine help strength?",
                    "allowed_domains": ["example.com"],
                    "model": "gpt-5.6",
                    "topic": "supplements",
                    "external_web_access": True,
                }
            )

    def test_public_contract_accepts_only_actor_and_query(self) -> None:
        request = TrustedWebRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"query":"  Does   creatine help strength?  "}'
        )
        self.assertEqual(request.user_id, ACTOR)
        self.assertEqual(request.query, "Does creatine help strength?")

    def test_no_store_headers_are_mandatory(self) -> None:
        response = Response()
        apply_trusted_web_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
