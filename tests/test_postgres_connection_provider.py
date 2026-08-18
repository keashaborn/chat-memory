from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.postgres import (
    OWNER_SESSION_SQL,
    READINESS_SQL,
    PostgresConnectionProvider,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
DSN = "postgresql://synthetic"


def connection(*, readiness: object = 1) -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(return_value="SELECT 1"),
        fetchval=AsyncMock(return_value=readiness),
        close=AsyncMock(return_value=None),
    )


class PostgresConnectionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_connection_sets_exact_session_and_closes(self) -> None:
        value = connection()
        connect = AsyncMock(return_value=value)
        provider = PostgresConnectionProvider(DSN, connect_factory=connect)

        async with provider.owner_connection(str(OWNER).upper()) as active:
            self.assertIs(active, value)

        connect.assert_awaited_once_with(DSN)
        value.execute.assert_awaited_once_with(OWNER_SESSION_SQL, str(OWNER))
        value.close.assert_awaited_once_with()

    async def test_invalid_owner_fails_closed_and_closes(self) -> None:
        value = connection()
        provider = PostgresConnectionProvider(
            DSN,
            connect_factory=AsyncMock(return_value=value),
        )

        with self.assertRaisesRegex(ValueError, "owner_user_id must be a UUID"):
            async with provider.owner_connection("not-a-uuid"):
                self.fail("invalid owner reached connection body")

        value.execute.assert_not_awaited()
        value.close.assert_awaited_once_with()

    async def test_connection_closes_when_caller_raises(self) -> None:
        value = connection()
        provider = PostgresConnectionProvider(
            DSN,
            connect_factory=AsyncMock(return_value=value),
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic caller failure"):
            async with provider.connection():
                raise RuntimeError("synthetic caller failure")

        value.close.assert_awaited_once_with()

    async def test_readiness_uses_shared_lifetime_boundary(self) -> None:
        value = connection(readiness=1)
        provider = PostgresConnectionProvider(
            DSN,
            connect_factory=AsyncMock(return_value=value),
        )

        self.assertEqual(await provider.readiness_value(), 1)
        value.fetchval.assert_awaited_once_with(READINESS_SQL)
        value.close.assert_awaited_once_with()

    def test_app_has_no_direct_connection_or_session_lifetime(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("asyncpg.connect", source)
        self.assertNotIn("_set_connection_actor", source)
        self.assertNotIn("await conn.close()", source)
        self.assertIn("POSTGRES = PostgresConnectionProvider(DSN)", source)


if __name__ == "__main__":
    unittest.main()
