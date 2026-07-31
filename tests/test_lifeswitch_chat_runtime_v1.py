from __future__ import annotations

import unittest
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from rag_engine.lifeswitch_chat_runtime_v1 import (
    LifeSwitchChatPoolManagerV1,
    LifeSwitchChatRuntimeSettingsV1,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")


class FakePool:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class LifeSwitchChatRuntimeV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_default_is_off_without_credentials_or_canary(self) -> None:
        settings = LifeSwitchChatRuntimeSettingsV1.from_environment({})

        self.assertEqual(settings.mode, "off")
        self.assertIsNone(settings.dsn)
        self.assertFalse(settings.enabled_for(OWNER_A))

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


if __name__ == "__main__":
    unittest.main()
