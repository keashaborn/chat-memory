from __future__ import annotations

import unittest
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from rag_engine.current_news_router import (
    CurrentNewsRequestV1,
    CurrentNewsResponseV1,
    apply_current_news_no_store_headers,
)
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class CurrentNewsRouterTests(unittest.TestCase):
    def test_public_contract_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            CurrentNewsRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "query": "What just happened with OpenAI?",
                    "allowed_domains": ["example.com"],
                    "model": "gpt-5.6",
                    "topic": "current_news",
                    "external_web_access": True,
                }
            )

    def test_public_contract_accepts_only_actor_and_query(self) -> None:
        request = CurrentNewsRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"query":"  What just   happened with OpenAI?  "}'
        )
        self.assertEqual(request.user_id, ACTOR)
        self.assertEqual(request.query, "What just happened with OpenAI?")

    def test_no_store_headers_are_mandatory(self) -> None:
        response = Response()
        apply_current_news_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_current_news_policy_routes_but_fetch_remains_disabled(self) -> None:
        decision = route_trusted_web_query(
            "What just happened with OpenAI and Hugging Face?"
        )
        response = CurrentNewsResponseV1(
            search_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_version=decision.policy_version,
            topic=decision.topic,
            disposition=TrustedWebDispositionV1.DECLINE,
            reason="current_news_fetch_not_enabled",
            searched=False,
            answer="Current news lookup is routed and policy-approved, but live news retrieval is not enabled yet.",
            sources=(),
        )
        self.assertEqual(response.topic, TrustedWebTopicV1.CURRENT_NEWS)
        self.assertFalse(response.searched)
        self.assertEqual(response.disposition, TrustedWebDispositionV1.DECLINE)
        self.assertEqual(response.sources, ())


if __name__ == "__main__":
    unittest.main()
