from __future__ import annotations

import json
import unittest
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_orchestration_shadow_runtime_v0_2 import (
    ResponseShadowRuntimeResultV0_2,
    SHADOW_ALLOWLIST,
    SHADOW_FLAG,
    ShadowRuntimeErrorCode,
    ShadowRuntimeStatus,
    evaluate_response_shadow_v0_2,
)
ACTOR = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_ACTOR = "557ea042-cb82-48f8-9429-472e96c957ef"
THREAD = "d776c8ef-7f3d-45b2-8820-4be87b7ca19d"

def snapshot(
    message: str = "What time is it?",
    *,
    actor: str = ACTOR,
    request_id: str = "shadow-request",
):
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=UUID(actor),
        thread_id=UUID(THREAD),
        current_request_id=request_id,
        current_message=message,
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


class FakeModerations:
    def __init__(self, *, flagged: bool = False, error: Exception | None = None):
        self.flagged = flagged
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        values = categories(**({"violence": True} if self.flagged else {}))
        return {"results": [{"flagged": self.flagged, "categories": values}]}


class FakeChatCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        raise AssertionError("shadow runtime called the generation model")


class FakeClient:
    def __init__(self, *, flagged: bool = False, error: Exception | None = None):
        self.moderations = FakeModerations(flagged=flagged, error=error)
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeChatCompletions()
        self.option_calls: list[dict[str, Any]] = []

    def with_options(self, **kwargs: Any) -> "FakeClient":
        self.option_calls.append(kwargs)
        return self


def enabled_env(actor: str = ACTOR) -> dict[str, str]:
    return {SHADOW_FLAG: "1", SHADOW_ALLOWLIST: actor}


class ResponseOrchestrationShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_does_no_provider_work(self) -> None:
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=None,
            request_field_names=(),
            openai_client=None,
            environ={},
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.DISABLED)
        self.assertIsNone(result.trace)
        self.assertFalse(result.response_influence)

    async def test_nonallowlisted_actor_does_no_provider_work(self) -> None:
        client = FakeClient()
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=None,
            request_field_names=(),
            openai_client=client,
            environ=enabled_env(OTHER_ACTOR),
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.ACTOR_NOT_ALLOWLISTED)
        self.assertEqual(client.moderations.calls, [])
        self.assertEqual(client.chat.completions.calls, [])

    async def test_allowlisted_shadow_calls_only_moderation(self) -> None:
        client = FakeClient()
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(request_id="shadow-evaluated"),
            request_field_names=("mix", "roleplay", "vantage_id"),
            openai_client=client,
            environ=enabled_env(),
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.EVALUATED)
        self.assertIsNotNone(result.trace)
        self.assertEqual(len(client.moderations.calls), 1)
        self.assertEqual(client.chat.completions.calls, [])
        self.assertEqual(result.generation_model_calls, 0)
        self.assertEqual(result.governed_memory_selection_calls, 0)
        self.assertFalse(result.response_influence)

    async def test_moderation_flag_changes_shadow_mode_only(self) -> None:
        client = FakeClient(flagged=True)
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(
                "Explain Fractal Monism.", request_id="shadow-flagged"
            ),
            request_field_names=("fm_lens",),
            openai_client=client,
            environ=enabled_env(),
        )
        assert result.trace is not None
        self.assertEqual(result.trace.response_mode, "HIGH_STAKES")
        self.assertEqual(result.trace.fm_level, "OFF")
        self.assertFalse(result.response_influence)
        self.assertEqual(client.chat.completions.calls, [])

    async def test_moderation_failure_is_a_valid_uncertain_shadow_result(self) -> None:
        client = FakeClient(error=TimeoutError("private moderation detail"))
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(
                "Explain Fractal Monism.",
                request_id="shadow-moderation-error",
            ),
            request_field_names=(),
            openai_client=client,
            environ=enabled_env(),
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.EVALUATED)
        assert result.trace is not None
        self.assertEqual(result.trace.high_stakes_gate, "uncertain")
        self.assertIn(
            "openai_moderation_api_error",
            result.trace.safety_reason_codes,
        )
        self.assertNotIn("private moderation detail", result.model_dump_json())

    async def test_trace_and_result_never_contain_message_or_actor(self) -> None:
        sentinel = "PRIVATE-SHADOW-RUNTIME-SENTINEL"
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(sentinel, request_id="shadow-private"),
            request_field_names=(),
            openai_client=FakeClient(),
            environ=enabled_env(),
        )
        serialized = result.model_dump_json()
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn(ACTOR, serialized)
        self.assertNotIn("actor_user_id", serialized)

    async def test_malformed_allowlist_fails_before_provider_call(self) -> None:
        client = FakeClient()
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=None,
            request_field_names=(),
            openai_client=client,
            environ={SHADOW_FLAG: "1", SHADOW_ALLOWLIST: f"{ACTOR},{ACTOR}"},
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.ERROR)
        self.assertEqual(result.error_code, ShadowRuntimeErrorCode.INVALID_ALLOWLIST)
        self.assertEqual(client.moderations.calls, [])

    async def test_cross_owner_snapshot_fails_before_provider_call(self) -> None:
        client = FakeClient()
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(
                actor=OTHER_ACTOR,
                request_id="shadow-owner-mismatch",
            ),
            request_field_names=(),
            openai_client=client,
            environ=enabled_env(),
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.ERROR)
        self.assertEqual(
            result.error_code,
            ShadowRuntimeErrorCode.INVALID_TRUSTED_INPUT,
        )
        self.assertEqual(client.moderations.calls, [])

    async def test_non_snapshot_object_fails_before_provider_call(self) -> None:
        client = FakeClient()
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot="not-a-trusted-snapshot",  # type: ignore[arg-type]
            request_field_names=(),
            openai_client=client,
            environ=enabled_env(),
        )
        self.assertEqual(result.status, ShadowRuntimeStatus.ERROR)
        self.assertEqual(
            result.error_code,
            ShadowRuntimeErrorCode.INVALID_TRUSTED_INPUT,
        )
        self.assertEqual(client.moderations.calls, [])

    async def test_result_manifest_rejects_rehashed_omission_or_tampering(self) -> None:
        result = await evaluate_response_shadow_v0_2(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(request_id="shadow-manifest"),
            request_field_names=(),
            openai_client=FakeClient(),
            environ=enabled_env(),
        )
        tampered = result.model_dump(mode="json")
        tampered["response_influence"] = True
        with self.assertRaises(ValidationError):
            ResponseShadowRuntimeResultV0_2.model_validate(tampered)


if __name__ == "__main__":
    unittest.main()
