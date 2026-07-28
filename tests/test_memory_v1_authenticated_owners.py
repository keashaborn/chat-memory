from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
import uuid

from scripts.memory_v1_authenticated_owners import (
    explicit_owners,
    loopback_dsn,
    resolve_authenticated_owners,
)


class AuthenticatedOwnerResolverTest(unittest.TestCase):
    def test_explicit_owner_scope_is_sorted_and_deduplicated(self) -> None:
        first = "22222222-2222-4222-8222-222222222222"
        second = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            explicit_owners([first, second, first]),
            [uuid.UUID(second), uuid.UUID(first)],
        )

    def test_invalid_uuid_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid UUID"):
            explicit_owners(["not-a-uuid"])

    def test_owner_discovery_rejects_non_loopback_database(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "loopback-only"):
            loopback_dsn("postgresql://brains_app@example.test/memory")


class AuthenticatedOwnerResolverAsyncTest(unittest.IsolatedAsyncioTestCase):
    @patch(
        "scripts.memory_v1_authenticated_owners.asyncpg.connect",
        new_callable=AsyncMock,
    )
    async def test_owner_discovery_disables_ssl_home_probing(
        self,
        connect: AsyncMock,
    ) -> None:
        owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
        conn = AsyncMock()
        conn.fetchval.return_value = "brains_app"
        conn.fetch.return_value = [{"owner_user_id": owner}]
        connect.return_value = conn

        self.assertEqual(
            await resolve_authenticated_owners(
                "postgresql://brains_app@127.0.0.1:5432/memory",
                [],
            ),
            [owner],
        )
        connect.assert_awaited_once_with(
            "postgresql://brains_app@127.0.0.1:5432/memory",
            command_timeout=30,
            ssl=False,
        )
        conn.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
