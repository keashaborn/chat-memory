from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

from pydantic import ValidationError

from rag_engine.lifeswitch_chat_runtime_v1 import (
    LazyPostgresRestrictedLifeSwitchReadSessionV1,
    LifeSwitchChatPoolManagerV1,
    LifeSwitchChatRuntimeSettingsV1,
    LoggingLifeSwitchSelfShadowInspectionSinkV1,
)
from rag_engine.lifeswitch_coaching_self_shadow_observer_v2 import (
    LifeSwitchSelfShadowObserverV2,
)
from rag_engine.lifeswitch_coaching_self_shadow_runner_v2 import (
    SelfShadowInspectionV1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")


class FakePool:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakePoolManager:
    def __init__(self, settings: LifeSwitchChatRuntimeSettingsV1) -> None:
        self.settings = settings
        self.value = object()
        self.calls = 0

    async def pool(self):
        self.calls += 1
        return self.value


def snapshot():
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=OWNER_A,
        thread_id=UUID("33333333-3333-4333-8333-333333333333"),
        current_request_id="44444444-4444-4444-8444-444444444444",
        current_message="What is my current plan?",
    )


def safe_inspection() -> SelfShadowInspectionV1:
    return SelfShadowInspectionV1(
        source_status="SELECTED",
        source_projection="current_plan",
        target_projection_id="plan.current.v1",
        target_status="available",
        serialized_projection_bytes=100,
        source_record_count=1,
        present_field_count=1,
        explicit_zero_field_count=0,
        missing_field_count=0,
        absent_field_count=0,
        in_progress_field_count=0,
        incomplete_field_count=0,
        corrected_field_count=0,
        deleted_field_count=0,
    )


class LifeSwitchChatRuntimeV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_default_is_off_without_credentials_or_canary(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1.from_environment({})

        self.assertEqual(settings.mode, "off")
        self.assertIsNone(settings.dsn)
        self.assertFalse(settings.enabled_for(OWNER_A))
        self.assertEqual(settings.self_shadow_mode, "off")
        self.assertFalse(settings.self_shadow_enabled_for(OWNER_A))

    def test_active_modes_require_complete_server_configuration(self) -> None:
        with self.assertRaises(ValidationError):
            LifeSwitchChatRuntimeSettingsV1(mode="on")
        with self.assertRaises(ValidationError):
            LifeSwitchChatRuntimeSettingsV1(
                mode="canary",
                dsn="postgresql://example.invalid/db",
            )

    def test_canary_is_owner_scoped_and_not_client_controlled(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1.from_environment(
            {
                "LIFESWITCH_CHAT_CONTEXT_MODE": "canary",
                "LIFESWITCH_CHAT_POSTGRES_DSN": "postgresql://example.invalid/db",
                "LIFESWITCH_CHAT_CANARY_OWNER_IDS": str(OWNER_A),
            }
        )

        self.assertTrue(settings.enabled_for(OWNER_A))
        self.assertFalse(settings.enabled_for(OWNER_B))

    def test_self_shadow_requires_active_lifeswitch_context(self) -> None:
        with self.assertRaises(ValidationError):
            LifeSwitchChatRuntimeSettingsV1(self_shadow_mode="on")

    def test_self_shadow_canary_is_independently_owner_scoped(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1.from_environment(
            {
                "LIFESWITCH_CHAT_CONTEXT_MODE": "on",
                "LIFESWITCH_CHAT_POSTGRES_DSN": "postgresql://example.invalid/db",
                "LIFESWITCH_COACHING_CONTEXT_V2_SHADOW_MODE": "canary",
                "LIFESWITCH_COACHING_CONTEXT_V2_SHADOW_CANARY_OWNER_IDS": str(
                    OWNER_A
                ),
            }
        )

        self.assertTrue(settings.enabled_for(OWNER_A))
        self.assertTrue(settings.enabled_for(OWNER_B))
        self.assertTrue(settings.self_shadow_enabled_for(OWNER_A))
        self.assertFalse(settings.self_shadow_enabled_for(OWNER_B))

    def test_self_shadow_on_still_respects_base_context_canary(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1(
            mode="canary",
            dsn="postgresql://example.invalid/db",
            canary_owner_ids=frozenset({OWNER_A}),
            self_shadow_mode="on",
        )

        self.assertTrue(settings.self_shadow_enabled_for(OWNER_A))
        self.assertFalse(settings.self_shadow_enabled_for(OWNER_B))

    def test_invalid_or_unbound_self_shadow_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            LifeSwitchChatRuntimeSettingsV1.from_environment(
                {
                    "LIFESWITCH_CHAT_CONTEXT_MODE": "on",
                    "LIFESWITCH_CHAT_POSTGRES_DSN": "postgresql://example.invalid/db",
                    "LIFESWITCH_COACHING_CONTEXT_V2_SHADOW_MODE": "invalid",
                }
            )
        with self.assertRaises(ValidationError):
            LifeSwitchChatRuntimeSettingsV1(
                mode="on",
                dsn="postgresql://example.invalid/db",
                self_shadow_mode="canary",
            )
        with self.assertRaises(ValidationError):
            LifeSwitchChatRuntimeSettingsV1(
                mode="on",
                dsn="postgresql://example.invalid/db",
                self_shadow_canary_owner_ids=frozenset({OWNER_A}),
            )

    async def test_off_creates_no_pool(self) -> None:
        calls: list[dict[str, Any]] = []

        async def factory(**kwargs: Any) -> FakePool:
            calls.append(kwargs)
            return FakePool()

        manager = LifeSwitchChatPoolManagerV1(
            LifeSwitchChatRuntimeSettingsV1(),
            pool_factory=factory,
        )

        with self.assertRaises(RuntimeError):
            await manager.pool()
        self.assertEqual(calls, [])

    async def test_active_pool_is_dedicated_bounded_and_reused(self) -> None:
        calls: list[dict[str, Any]] = []
        pool = FakePool()

        async def factory(**kwargs: Any) -> FakePool:
            calls.append(kwargs)
            return pool

        settings = LifeSwitchChatRuntimeSettingsV1(
            mode="on",
            dsn="postgresql://example.invalid/db",
            pool_max_size=3,
        )
        manager = LifeSwitchChatPoolManagerV1(settings, pool_factory=factory)

        self.assertIs(await manager.pool(), pool)
        self.assertIs(await manager.pool(), pool)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["min_size"], 0)
        self.assertEqual(calls[0]["max_size"], 3)
        self.assertIn("init", calls[0])
        self.assertIn("reset", calls[0])
        self.assertEqual(
            calls[0]["server_settings"]["application_name"],
            "brains_lifeswitch_chat_context_v1",
        )

        await manager.close()
        self.assertEqual(pool.closed, 1)

    async def test_lazy_session_injects_shadow_only_for_authorized_owner(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1(
            mode="on",
            dsn="postgresql://example.invalid/db",
            self_shadow_mode="canary",
            self_shadow_canary_owner_ids=frozenset({OWNER_A}),
        )
        manager = FakePoolManager(settings)
        prepared = object()

        with patch(
            "rag_engine.lifeswitch_chat_runtime_v1."
            "PostgresRestrictedLifeSwitchReadSessionV1"
        ) as session_type:
            session_type.return_value.select = AsyncMock(return_value=prepared)
            result = await LazyPostgresRestrictedLifeSwitchReadSessionV1(
                manager
            ).select(
                authenticated_actor_user_id=OWNER_A,
                conversation_snapshot=snapshot(),
                query="What is my current plan?",
            )

        self.assertIs(result, prepared)
        self.assertEqual(manager.calls, 1)
        observer = session_type.call_args.kwargs["self_shadow_observer"]
        self.assertIsInstance(observer, LifeSwitchSelfShadowObserverV2)

    async def test_lazy_session_omits_shadow_for_unauthorized_owner(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1(
            mode="on",
            dsn="postgresql://example.invalid/db",
            self_shadow_mode="canary",
            self_shadow_canary_owner_ids=frozenset({OWNER_B}),
        )
        manager = FakePoolManager(settings)

        with patch(
            "rag_engine.lifeswitch_chat_runtime_v1."
            "PostgresRestrictedLifeSwitchReadSessionV1"
        ) as session_type:
            session_type.return_value.select = AsyncMock(return_value=object())
            await LazyPostgresRestrictedLifeSwitchReadSessionV1(manager).select(
                authenticated_actor_user_id=OWNER_A,
                conversation_snapshot=snapshot(),
                query="What is my current plan?",
            )

        self.assertIsNone(session_type.call_args.kwargs["self_shadow_observer"])

    async def test_logging_sink_emits_only_content_free_inspection(self) -> None:
        inspection = safe_inspection()
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            await LoggingLifeSwitchSelfShadowInspectionSinkV1().record(inspection)

        self.assertEqual(len(captured.output), 1)
        self.assertIn("lifeswitch_self_s1_shadow", captured.output[0])
        self.assertIn("plan.current.v1", captured.output[0])
        self.assertNotIn(str(OWNER_A), captured.output[0])
        self.assertNotIn(str(OWNER_B), captured.output[0])


if __name__ == "__main__":
    unittest.main()
