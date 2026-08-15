from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from rag_engine.governed_memory.preference_correction_interpreter import (
    FLEXIBLE_CORRECTIONS_ENABLED_ENV,
    InterpretedPreferenceRetractionV1,
    OpenAIPreferenceCorrectionInterpreterV1,
    PreferenceCorrectionClaimV1,
    PreferenceCorrectionInterpretationUnavailableV1,
    openai_preference_correction_interpreter_from_environment_v1,
    potential_preference_correction_v1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
CLAIM = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


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


def claim(
    value: str = "celesta",
    *,
    claim_id: UUID = CLAIM,
) -> PreferenceCorrectionClaimV1:
    return PreferenceCorrectionClaimV1(
        claim_id=claim_id,
        lifecycle_state="active",
        current_value=value,
    )


class PreferenceCorrectionInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_language_correction_is_strictly_bound(self) -> None:
        client = FakeClient(
            {
                "action": "correct",
                "previous_value": "celesta",
                "replacement_value": "vibraphone",
            }
        )
        interpreter = OpenAIPreferenceCorrectionInterpreterV1(client=client)
        command = await interpreter.interpret(
            owner_user_id=OWNER,
            message="Actually, I prefer the vibraphone now.",
            claims=(claim(),),
        )
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.previous_literal, "celesta")
        self.assertEqual(command.replacement_literal, "vibraphone")
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5-mini-2025-08-07")
        self.assertFalse(call["store"])
        self.assertRegex(str(call["safety_identifier"]), r"^mc1_[0-9a-f]{60}$")
        payload = json.loads(call["input"][1]["content"])
        self.assertEqual(payload["message"], "Actually, I prefer the vibraphone now.")
        self.assertEqual(
            payload["current_preferences"],
            [{"current_value": "celesta", "lifecycle_state": "active"}],
        )
        self.assertNotIn(str(OWNER), call["input"][1]["content"])
        self.assertNotIn(str(CLAIM), call["input"][1]["content"])

    async def test_natural_language_retraction_is_strictly_bound(self) -> None:
        client = FakeClient(
            {
                "action": "retract",
                "previous_value": "genmaicha",
                "replacement_value": None,
            }
        )
        interpreter = OpenAIPreferenceCorrectionInterpreterV1(client=client)
        command = await interpreter.interpret(
            owner_user_id=OWNER,
            message="I no longer have a go-to natural-memory test tea.",
            claims=(
                claim("genmaicha"),
                claim(
                    "vibraphone",
                    claim_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                ),
            ),
        )
        self.assertIsInstance(command, InterpretedPreferenceRetractionV1)
        assert isinstance(command, InterpretedPreferenceRetractionV1)
        self.assertEqual(command.previous_literal, "genmaicha")
        payload = json.loads(client.responses.calls[0]["input"][1]["content"])
        self.assertEqual(
            payload["contract"],
            "owner_preference_lifecycle_interpretation_v2",
        )

    async def test_none_and_unknown_target_never_create_command(self) -> None:
        cases = (
            {
                "action": "none",
                "previous_value": None,
                "replacement_value": None,
            },
            {
                "action": "correct",
                "previous_value": "unknown",
                "replacement_value": "vibraphone",
            },
        )
        for output in cases:
            with self.subTest(output=output):
                interpreter = OpenAIPreferenceCorrectionInterpreterV1(
                    client=FakeClient(output)
                )
                self.assertIsNone(
                    await interpreter.interpret(
                        owner_user_id=OWNER,
                        message="I changed this preference now.",
                        claims=(claim(),),
                    )
                )

    async def test_provider_failure_is_typed_and_has_no_fallback(self) -> None:
        interpreter = OpenAIPreferenceCorrectionInterpreterV1(
            client=FakeClient(RuntimeError("synthetic provider failure"))
        )
        with self.assertRaises(PreferenceCorrectionInterpretationUnavailableV1):
            await interpreter.interpret(
                owner_user_id=OWNER,
                message="Change this preference now.",
                claims=(claim(),),
            )

    async def test_non_candidate_and_empty_claims_make_no_provider_call(self) -> None:
        client = FakeClient(
            {
                "action": "none",
                "previous_value": None,
                "replacement_value": None,
            }
        )
        interpreter = OpenAIPreferenceCorrectionInterpreterV1(client=client)
        self.assertIsNone(
            await interpreter.interpret(
                owner_user_id=OWNER,
                message="What is my preferred instrument?",
                claims=(claim(),),
            )
        )
        self.assertIsNone(
            await interpreter.interpret(
                owner_user_id=OWNER,
                message="Actually, I prefer vibraphone now.",
                claims=(),
            )
        )
        self.assertEqual(client.responses.calls, [])

    def test_candidate_gate_is_broad_not_an_exact_command_parser(self) -> None:
        for message in (
            "Actually, I prefer vibraphone now.",
            "Celesta was wrong; vibraphone fits me better.",
            "Can you update my instrument preference to vibraphone?",
            "Vibraphone has replaced celesta for me.",
            "Use vibraphone instead.",
            "I prefer vibraphone these days.",
            "I no longer have a go-to natural-memory test tea.",
        ):
            with self.subTest(message=message):
                self.assertTrue(potential_preference_correction_v1(message))
        self.assertFalse(
            potential_preference_correction_v1(
                "What is my preferred instrument?"
            )
        )

    def test_feature_flag_defaults_off_and_rejects_invalid_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                openai_preference_correction_interpreter_from_environment_v1()
            )
        with patch.dict(
            os.environ,
            {FLEXIBLE_CORRECTIONS_ENABLED_ENV: "invalid"},
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                openai_preference_correction_interpreter_from_environment_v1()


if __name__ == "__main__":
    unittest.main()
