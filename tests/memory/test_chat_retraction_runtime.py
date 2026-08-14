from __future__ import annotations

import json
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory_chat_retraction_v1 import (
    ChatRetractionError,
    ChatRetractionRuntimeV1,
    explicit_preference_retraction_target_v1,
    retraction_operation_id_v1,
)
from rag_engine.governed_memory_erasure_proxy_v1 import ProxyResult


CLAIM_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLAIM_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OUTBOX = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
REVISION = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
AUTHORIZATION = "Bearer a.b.c"
MESSAGE = (
    "I no longer have a preferred rebuild test animal. "
    "Retract the red panda preference."
)
ROOT = Path(__file__).resolve().parents[2]


def summary(
    claim_id: UUID,
    *,
    state: str = "active",
    kind: str = "literal",
) -> dict[str, object]:
    return {
        "claim_id": str(claim_id),
        "lifecycle_state": state,
        "revision_id": str(REVISION),
        "revision_number": 1,
        "revision_sha256": HASH_A,
        "current_state_sha256": HASH_B,
        "revision_fact_policy_sha256": HASH_C,
        "predicate_catalog_sha256": HASH_D,
        "selected_sha256": HASH_E,
        "selection_binding_sha256": HASH_A,
        "object_kind": kind,
        "predicate": "preference.personal",
        "epistemic_state": "supported",
        "sensitivity": "ordinary",
        "updated_at": "2030-01-02T12:00:00Z",
    }


def detail(
    claim_id: UUID,
    *,
    state: str = "active",
    literal: str = "red panda",
    kind: str = "literal",
) -> dict[str, object]:
    entity = kind == "entity"
    return {
        **summary(claim_id, state=state, kind=kind),
        "subject_entity_type": "self",
        "subject_entity_key": "self",
        "subject_display_name": None,
        "object_entity_type": "other" if entity else None,
        "object_entity_key": "local:synthetic-red-panda" if entity else None,
        "object_display_name": literal if entity else None,
        "object_literal": None if entity else literal,
    }


def result(value: object, *, status_code: int = 200) -> ProxyResult:
    return ProxyResult(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps(value, separators=(",", ":")).encode("utf-8"),
    )


class FakeTransport:
    def __init__(self, responses: list[ProxyResult]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> ProxyResult:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class ChatRetractionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, transport: FakeTransport) -> ChatRetractionRuntimeV1:
        return ChatRetractionRuntimeV1(
            service_token="synthetic-service-token",
            transport=transport,
        )

    async def test_ordinary_questions_and_hypotheticals_make_no_calls(self) -> None:
        transport = FakeTransport([])
        runtime = self.runtime(transport)
        for message in (
            "Your preferred rebuild test animal is the red panda.",
            "What is my preferred rebuild test animal?",
            "Should I retract the red panda preference?",
            "Do not retract the red panda preference.",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    await runtime.apply_if_requested(
                        message=message,
                        authorization=AUTHORIZATION,
                    )
                )
        self.assertEqual(transport.calls, [])

    async def test_explicit_unique_active_preference_is_retracted(self) -> None:
        transport = FakeTransport(
            [
                result([summary(CLAIM_A)]),
                result(detail(CLAIM_A)),
                result({"outcome": "retracted", "outbox_id": str(OUTBOX)}),
            ]
        )
        receipt = await self.runtime(transport).apply_if_requested(
            message=MESSAGE,
            authorization=AUTHORIZATION,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.claim_id, CLAIM_A)
        self.assertEqual(receipt.outcome, "retracted")
        self.assertEqual(
            [(call["method"], call["path"]) for call in transport.calls],
            [
                ("GET", "/memory/claims"),
                ("GET", f"/memory/claims/{CLAIM_A}"),
                ("POST", f"/memory/claims/{CLAIM_A}/retract"),
            ],
        )
        mutation = json.loads(bytes(transport.calls[-1]["body"]))
        self.assertEqual(
            mutation,
            {
                "expected_revision_sha256": HASH_A,
                "expected_state_sha256": HASH_B,
                "operation_id": str(
                    retraction_operation_id_v1(
                        claim_id=CLAIM_A,
                        message=MESSAGE,
                    )
                ),
            },
        )

    async def test_explicit_unique_entity_preference_is_retracted(self) -> None:
        transport = FakeTransport(
            [
                result([summary(CLAIM_A, kind="entity")]),
                result(detail(CLAIM_A, kind="entity")),
                result({"outcome": "retracted", "outbox_id": str(OUTBOX)}),
            ]
        )
        receipt = await self.runtime(transport).apply_if_requested(
            message=MESSAGE,
            authorization=AUTHORIZATION,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.claim_id, CLAIM_A)
        self.assertEqual(receipt.outcome, "retracted")
        self.assertEqual(transport.calls[-1]["method"], "POST")
        self.assertEqual(
            transport.calls[-1]["path"],
            f"/memory/claims/{CLAIM_A}/retract",
        )

    async def test_missing_or_ambiguous_target_fails_closed_without_post(self) -> None:
        cases = (
            (
                [summary(CLAIM_A)],
                [detail(CLAIM_A, literal="cobalt blue")],
                "memory_retraction_target_not_found",
            ),
            (
                [summary(CLAIM_A), summary(CLAIM_B)],
                [detail(CLAIM_A), detail(CLAIM_B)],
                "memory_retraction_target_ambiguous",
            ),
        )
        for summaries, details, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                transport = FakeTransport(
                    [result(summaries), *(result(item) for item in details)]
                )
                with self.assertRaises(ChatRetractionError) as raised:
                    await self.runtime(transport).apply_if_requested(
                        message=MESSAGE,
                        authorization=AUTHORIZATION,
                    )
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotIn(
                    "POST",
                    {str(call["method"]) for call in transport.calls},
                )

    async def test_already_retracted_unique_target_is_idempotent(self) -> None:
        transport = FakeTransport(
            [
                result([summary(CLAIM_A, state="retracted")]),
                result(detail(CLAIM_A, state="retracted")),
            ]
        )
        receipt = await self.runtime(transport).apply_if_requested(
            message=MESSAGE,
            authorization=AUTHORIZATION,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.outcome, "replayed")
        self.assertEqual(len(transport.calls), 2)

    async def test_malformed_successor_response_fails_closed(self) -> None:
        transport = FakeTransport(
            [
                ProxyResult(
                    status_code=200,
                    headers={"content-type": "text/plain"},
                    body=b"[]",
                )
            ]
        )
        with self.assertRaises(ChatRetractionError) as raised:
            await self.runtime(transport).apply_if_requested(
                message=MESSAGE,
                authorization=AUTHORIZATION,
            )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "memory_retraction_unavailable")

    def test_detection_and_router_integration_are_narrow(self) -> None:
        self.assertEqual(
            explicit_preference_retraction_target_v1(MESSAGE),
            "red panda",
        )
        route = (ROOT / "rag_engine/resse_response_router.py").read_text()
        function = route[route.index("async def resse_response_query(") :]
        self.assertIn("CHAT_RETRACTION_RUNTIME.apply_if_requested(", function)
        self.assertLess(
            function.index("CHAT_RETRACTION_RUNTIME.apply_if_requested("),
            function.index("openai_client = get_openai_client()"),
        )


if __name__ == "__main__":
    unittest.main()
