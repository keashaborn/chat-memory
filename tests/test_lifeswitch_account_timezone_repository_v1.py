from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import UUID

from starlette.requests import Request

from seebx.adapters.lifeswitch_timezone_postgres import (
    AccountTimezoneRepositoryConflict,
    AccountTimezoneRepositoryInvalid,
    AccountTimezoneRepositoryOwnerDenied,
    AccountTimezoneRepositoryUnavailable,
    PostgresAccountTimezoneRepository,
    _translate,
    account_timezone_repository,
)


ACTOR = UUID("673d64a3-c4ba-4d1c-89e3-e0c579022fad")
UPDATED_AT = datetime(2026, 8, 20, tzinfo=timezone.utc)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.executed = []
        self.fetched = []
        self.transactions = []
        self.closed = False

    def transaction(self, **options):
        self.transactions.append(options)
        return Transaction()

    async def execute(self, sql, *arguments):
        self.executed.append((sql, arguments))

    async def fetchrow(self, sql, *arguments):
        self.fetched.append((sql, arguments))
        return self.row

    async def close(self):
        self.closed = True


def row(*, changed: bool):
    return {
        "timezone_name": "America/Chicago",
        "timezone_source": "account_setting",
        "revision": 3,
        "updated_at": UPDATED_AT,
        "changed": changed,
    }


class AccountTimezoneRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_is_owner_bound_readonly_and_maps_record(self) -> None:
        connection = FakeConnection(row(changed=False))
        repository = PostgresAccountTimezoneRepository(connection)

        value = await repository.get(ACTOR)

        self.assertEqual(connection.transactions, [{"readonly": True}])
        self.assertEqual(len(connection.executed), 3)
        self.assertEqual(connection.executed[0][1], (str(ACTOR),))
        self.assertEqual(connection.executed[1][1], (str(ACTOR),))
        self.assertIn("set local role", connection.executed[2][0])
        self.assertIn("read_account_timezone_setting_v1", connection.fetched[0][0])
        self.assertEqual(connection.fetched[0][1], (ACTOR,))
        self.assertEqual(value.timezone_name, "America/Chicago")
        self.assertEqual(value.revision, 3)
        self.assertFalse(value.changed)

    async def test_put_preserves_writer_arguments_and_changed_flag(self) -> None:
        connection = FakeConnection(row(changed=True))
        repository = PostgresAccountTimezoneRepository(connection)

        value = await repository.put(ACTOR, "America/Chicago", 2, "a" * 64)

        self.assertEqual(connection.transactions, [{}])
        self.assertIn("write_account_timezone_setting_v1", connection.fetched[0][0])
        self.assertEqual(
            connection.fetched[0][1],
            (ACTOR, "America/Chicago", 2, "a" * 64),
        )
        self.assertTrue(value.changed)

    async def test_context_owns_connection_lifecycle(self) -> None:
        connection = FakeConnection(row(changed=False))
        request = Request({"type": "http", "headers": []})

        async def connect(value):
            self.assertIs(value, request)
            return connection

        async with account_timezone_repository(
            request,
            connection_factory=connect,
        ) as repository:
            self.assertIsInstance(repository, PostgresAccountTimezoneRepository)
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)

    def test_postgres_errors_map_to_stable_repository_errors(self) -> None:
        cases = (
            ("40001", AccountTimezoneRepositoryConflict),
            ("23505", AccountTimezoneRepositoryConflict),
            ("22023", AccountTimezoneRepositoryInvalid),
            ("42501", AccountTimezoneRepositoryOwnerDenied),
            ("08006", AccountTimezoneRepositoryUnavailable),
        )
        for sqlstate, expected in cases:
            with self.subTest(sqlstate=sqlstate):
                error = _translate(SimpleNamespace(sqlstate=sqlstate))
                self.assertIsInstance(error, expected)


if __name__ == "__main__":
    unittest.main()
