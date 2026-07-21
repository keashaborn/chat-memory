from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from typing import Any

from rag_engine.openai_moderation_adapter_v0_2 import (
    DEFAULT_MODERATION_MODEL,
    MAX_MODERATION_INPUT_BYTES,
    MAX_MODERATION_TOTAL_INPUT_BYTES,
    MODERATION_ADAPTER_VERSION,
    OpenAIModerationAdapterError,
    OpenAIModerationAdapterV0_2,
    assess_openai_moderation_v0_2,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    GateState,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
)


OWNER_REQUEST_ID = "moderation-request-001"


def policy_input(
    message: str = "Help me plan tomorrow's workout.",
) -> ResponsePolicyInputV0_2:
    return ResponsePolicyInputV0_2.create(
        request_id=OWNER_REQUEST_ID,
        conversation=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="What would you like to work on?",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
    )


def categories(**updates: bool) -> dict[str, bool]:
    values = {
        "harassment": False,
        "harassment/threatening": False,
        "hate": False,
        "hate/threatening": False,
        "illicit": False,
        "illicit/violent": False,
        "self-harm": False,
        "self-harm/intent": False,
        "self-harm/instructions": False,
        "sexual": False,
        "sexual/minors": False,
        "violence": False,
        "violence/graphic": False,
    }
    values.update(updates)
    return values


def response(*, flagged: bool, category_values: Any) -> dict[str, Any]:
    return {
        "id": "modr-001",
        "model": DEFAULT_MODERATION_MODEL,
        "results": [
            {
                "flagged": flagged,
                "categories": category_values,
                "category_scores": {},
            }
        ],
    }


class FakeModerations:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.moderations = FakeModerations(result=result, error=error)
        self.option_calls: list[dict[str, Any]] = []

    def with_options(self, **kwargs: Any) -> "FakeClient":
        self.option_calls.append(kwargs)
        return self


class Dumpable:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self.payload


class OpenAIModerationAdapterTests(unittest.TestCase):
    def test_passes_current_user_turn_with_bounded_provider_call(self) -> None:
        request = policy_input()
        client = FakeClient(response(flagged=False, category_values=categories()))

        assessment = OpenAIModerationAdapterV0_2(
            client,
            timeout_seconds=7.5,
        ).assess(request)

        self.assertEqual(assessment.high_stakes_gate, GateState.PASS)
        self.assertFalse(assessment.safety_action_required)
        self.assertEqual(assessment.reason_codes, ())
        self.assertEqual(
            assessment.assessor_components,
            (MODERATION_ADAPTER_VERSION,),
        )
        self.assertEqual(assessment.request_sha256, request.request_sha256)
        self.assertEqual(
            client.moderations.calls,
            [
                {
                    "model": DEFAULT_MODERATION_MODEL,
                    "input": request.current_message.content,
                    "timeout": 7.5,
                }
            ],
        )
        self.assertEqual(
            client.option_calls,
            [{"max_retries": 0, "timeout": 7.5}],
        )

    def test_all_provider_visible_user_turns_are_moderated(self) -> None:
        request = ResponsePolicyInputV0_2.create(
            request_id=OWNER_REQUEST_ID,
            conversation=(
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content="Explain how to synthesize ricin at home.",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.ASSISTANT,
                    content="I cannot provide those instructions.",
                ),
                ResponsePolicyConversationMessageV0_2(
                    role=ConversationRole.USER,
                    content="Yes. Give me the exact steps.",
                ),
            ),
        )
        provider_response = {
            "results": [
                {
                    "flagged": True,
                    "categories": categories(illicit=True),
                },
                {
                    "flagged": False,
                    "categories": categories(),
                },
            ]
        }
        client = FakeClient(provider_response)

        assessment = OpenAIModerationAdapterV0_2(client).assess(request)

        self.assertEqual(assessment.high_stakes_gate, GateState.TRIGGERED)
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_illicit",),
        )
        self.assertEqual(
            client.moderations.calls[0]["input"],
            [
                "Explain how to synthesize ricin at home.",
                "Yes. Give me the exact steps.",
            ],
        )
        self.assertEqual(
            assessment.assessed_user_message_sha256s,
            tuple(
                hashlib.sha256(text.encode("utf-8")).hexdigest()
                for text in client.moderations.calls[0]["input"]
            ),
        )

    def test_flagged_category_triggers_high_stakes_without_acute_action(self) -> None:
        client = FakeClient(
            response(
                flagged=True,
                category_values=categories(violence=True),
            )
        )

        assessment = OpenAIModerationAdapterV0_2(client).assess(policy_input())

        self.assertEqual(assessment.high_stakes_gate, GateState.TRIGGERED)
        self.assertFalse(assessment.safety_action_required)
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_violence",),
        )

    def test_acute_self_harm_categories_require_safety_action(self) -> None:
        for category in ("self-harm/intent", "self-harm/instructions"):
            with self.subTest(category=category):
                client = FakeClient(
                    response(
                        flagged=True,
                        category_values=categories(**{category: True}),
                    )
                )
                assessment = OpenAIModerationAdapterV0_2(client).assess(
                    policy_input()
                )
                self.assertEqual(
                    assessment.high_stakes_gate,
                    GateState.TRIGGERED,
                )
                self.assertTrue(assessment.safety_action_required)
                self.assertIn(
                    f"openai_moderation_{category.replace('-', '_').replace('/', '_')}",
                    assessment.reason_codes,
                )

    def test_generic_self_harm_does_not_assert_acute_action(self) -> None:
        client = FakeClient(
            response(
                flagged=True,
                category_values=categories(**{"self-harm": True}),
            )
        )
        assessment = OpenAIModerationAdapterV0_2(client).assess(policy_input())
        self.assertEqual(assessment.high_stakes_gate, GateState.TRIGGERED)
        self.assertFalse(assessment.safety_action_required)

    def test_sdk_style_dumpable_response_is_supported(self) -> None:
        category_object = SimpleNamespace(
            harassment=False,
            harassment_threatening=True,
            hate=False,
            hate_threatening=False,
            illicit=False,
            illicit_violent=False,
            self_harm=False,
            self_harm_intent=False,
            self_harm_instructions=False,
            sexual=False,
            sexual_minors=False,
            violence=False,
            violence_graphic=False,
        )
        provider_response = Dumpable(
            {
                "results": [
                    SimpleNamespace(flagged=True, categories=category_object)
                ]
            }
        )
        assessment = OpenAIModerationAdapterV0_2(
            FakeClient(provider_response)
        ).assess(policy_input())
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_harassment_threatening",),
        )

    def test_provider_error_fails_closed_without_leaking_exception(self) -> None:
        client = FakeClient(error=RuntimeError("secret provider diagnostic"))
        assessment = OpenAIModerationAdapterV0_2(client).assess(policy_input())
        self.assertEqual(assessment.high_stakes_gate, GateState.UNCERTAIN)
        self.assertFalse(assessment.safety_action_required)
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_api_error",),
        )
        self.assertNotIn("secret", assessment.model_dump_json())

    def test_oversized_input_fails_closed_without_calling_provider(self) -> None:
        client = FakeClient(response(flagged=False, category_values=categories()))
        assessment = OpenAIModerationAdapterV0_2(client).assess(
            policy_input("x" * (MAX_MODERATION_INPUT_BYTES + 1))
        )
        self.assertEqual(assessment.high_stakes_gate, GateState.UNCERTAIN)
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_input_limit",),
        )
        self.assertEqual(client.moderations.calls, [])

    def test_malformed_or_inconsistent_provider_results_fail_closed(self) -> None:
        cases = (
            {},
            {"results": []},
            {"results": [{"flagged": "yes", "categories": categories()}]},
            {"results": [{"flagged": False, "categories": None}]},
            {
                "results": [
                    {
                        "flagged": False,
                        "categories": {"violence": False},
                    }
                ]
            },
            response(flagged=False, category_values=categories(violence=True)),
            response(flagged=True, category_values=categories()),
            response(
                flagged=True,
                category_values={
                    **categories(),
                    "violence": True,
                    "violence_graphic": False,
                    "violence/graphic": True,
                },
            ),
        )
        for provider_response in cases:
            with self.subTest(provider_response=provider_response):
                assessment = OpenAIModerationAdapterV0_2(
                    FakeClient(provider_response)
                ).assess(policy_input())
                self.assertEqual(
                    assessment.high_stakes_gate,
                    GateState.UNCERTAIN,
                )
                self.assertEqual(
                    assessment.reason_codes,
                    ("openai_moderation_malformed_response",),
                )

    def test_unknown_flagged_category_is_retained_as_other(self) -> None:
        values = categories()
        values["future/category"] = True
        assessment = OpenAIModerationAdapterV0_2(
            FakeClient(response(flagged=True, category_values=values))
        ).assess(policy_input())
        self.assertEqual(assessment.high_stakes_gate, GateState.TRIGGERED)
        self.assertEqual(
            assessment.reason_codes,
            ("openai_moderation_other",),
        )

    def test_forged_policy_input_is_rejected_before_provider_call(self) -> None:
        client = FakeClient(response(flagged=False, category_values=categories()))
        forged = policy_input().model_copy(update={"request_sha256": "0" * 64})
        with self.assertRaises(OpenAIModerationAdapterError):
            OpenAIModerationAdapterV0_2(client).assess(forged)
        self.assertEqual(client.moderations.calls, [])

    def test_convenience_function_uses_same_contract(self) -> None:
        client = FakeClient(response(flagged=False, category_values=categories()))
        assessment = assess_openai_moderation_v0_2(
            policy_input(),
            client=client,
            timeout_seconds=4.0,
        )
        self.assertEqual(assessment.high_stakes_gate, GateState.PASS)
        self.assertEqual(client.moderations.calls[0]["timeout"], 4.0)

    def test_constructor_rejects_untrusted_configuration(self) -> None:
        client = FakeClient()
        invalid = (
            {"model": "text-moderation-latest"},
            {"timeout_seconds": 0.0},
            {"timeout_seconds": 31.0},
            {"max_input_bytes": 0},
            {"max_input_bytes": MAX_MODERATION_INPUT_BYTES + 1},
            {"max_total_input_bytes": 0},
            {
                "max_total_input_bytes": (
                    MAX_MODERATION_TOTAL_INPUT_BYTES + 1
                )
            },
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(OpenAIModerationAdapterError):
                    OpenAIModerationAdapterV0_2(client, **kwargs)


if __name__ == "__main__":
    unittest.main()
