from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from rag_engine.governed_memory.response_relevance import (
    OpenAIResponseRelevanceGateV1,
    ResponseRelevanceUnavailableV1,
)
from tests.memory._fixtures import CLAIM_A, OWNER_A, make_claim_row


class FakeResponses:
    def __init__(self, output: dict[str, object] | Exception) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        if isinstance(self.output, Exception):
            raise self.output
        output_type = kwargs["text_format"]
        return SimpleNamespace(output_parsed=output_type(**self.output))


class FakeClient:
    def __init__(self, output: dict[str, object] | Exception) -> None:
        self.responses = FakeResponses(output)
        self.options: list[dict[str, object]] = []

    def with_options(self, **kwargs: object) -> "FakeClient":
        self.options.append(dict(kwargs))
        return self


class ResponseRelevanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_directly_relevant_candidate_is_retained(self) -> None:
        client = FakeClient({"decisions": ["relevant"]})
        gate = OpenAIResponseRelevanceGateV1(client=client)
        result = await gate.relevant_claim_ids(
            owner_user_id=OWNER_A,
            query="What is my preferred validation instrument?",
            claims=(make_claim_row(object_literal="vibraphone"),),
        )
        self.assertEqual(result, (CLAIM_A,))
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5-mini-2025-08-07")
        self.assertFalse(call["store"])
        self.assertRegex(str(call["safety_identifier"]), r"^mr1_[0-9a-f]{60}$")
        payload = json.loads(call["input"][1]["content"])
        self.assertEqual(
            payload["contract"],
            "governed_memory_response_relevance_v1",
        )
        self.assertEqual(payload["candidates"][0]["object_value"], "vibraphone")
        self.assertNotIn(str(OWNER_A), call["input"][1]["content"])
        self.assertNotIn(str(CLAIM_A), call["input"][1]["content"])

    async def test_unrelated_candidate_is_rejected(self) -> None:
        gate = OpenAIResponseRelevanceGateV1(
            client=FakeClient({"decisions": ["irrelevant"]})
        )
        result = await gate.relevant_claim_ids(
            owner_user_id=OWNER_A,
            query="What is my preferred validation flower?",
            claims=(make_claim_row(object_literal="vibraphone"),),
        )
        self.assertEqual(result, ())

    async def test_provider_failure_and_malformed_count_are_typed(self) -> None:
        clients = (
            FakeClient(RuntimeError("synthetic provider failure")),
            FakeClient({"decisions": ["relevant", "irrelevant"]}),
        )
        for client in clients:
            with self.subTest(client=client), self.assertRaises(
                ResponseRelevanceUnavailableV1
            ):
                await OpenAIResponseRelevanceGateV1(
                    client=client
                ).relevant_claim_ids(
                    owner_user_id=OWNER_A,
                    query="What is my preferred validation flower?",
                    claims=(make_claim_row(object_literal="vibraphone"),),
                )


if __name__ == "__main__":
    unittest.main()
