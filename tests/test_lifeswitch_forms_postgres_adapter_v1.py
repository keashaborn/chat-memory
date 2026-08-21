from __future__ import annotations

import ast
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request

from seebx.adapters.lifeswitch_forms_postgres import (
    FormsTemplateNotFoundError,
    PostgresLifeSwitchFormsRepository,
    lifeswitch_forms_repository,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transactions_entered += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.transactions_exited += 1


class FakeConnection:
    def __init__(self, *, fetchrows=None, fetchvals=None, fetches=None, executes=None):
        self.fetchrows = list(fetchrows or [])
        self.fetchvals = list(fetchvals or [])
        self.fetches = list(fetches or [])
        self.executes = list(executes or [])
        self.calls = []
        self.transactions_entered = 0
        self.transactions_exited = 0
        self.closed = False

    def transaction(self):
        self.calls.append(("transaction",))
        return FakeTransaction(self)

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.fetchrows.pop(0)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.fetchvals.pop(0)

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetches.pop(0)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return self.executes.pop(0) if self.executes else "INSERT 0 1"

    async def close(self):
        self.closed = True


class FormsPostgresAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_is_the_only_forms_database_effect_owner(self):
        methods = {"execute", "fetch", "fetchrow", "fetchval", "transaction"}

        def effects(relative: str) -> list[tuple[str, int]]:
            tree = ast.parse((ROOT / relative).read_text())
            return [
                (node.func.attr, node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ]

        self.assertEqual(effects("seebx/capabilities/forms/routes.py"), [])
        self.assertEqual(
            len(effects("seebx/adapters/lifeswitch_forms_postgres.py")),
            15,
        )

    async def test_publish_new_template_is_atomic_owner_bound_and_deterministic(self):
        connection = FakeConnection(fetchrows=[None], executes=["INSERT 0 1", "INSERT 0 1"])
        repository = PostgresLifeSwitchFormsRepository(connection)
        template_id = uuid.uuid4()
        version_id = uuid.uuid4()
        version = await repository.publish_form(
            owner=OWNER,
            template_id=template_id,
            version_id=version_id,
            name="Daily",
            json_schema={"type": "object"},
            ui_schema={"order": ["a"]},
            metadata={"z": 2, "a": 1},
        )
        self.assertEqual(version, 1)
        self.assertEqual(connection.transactions_entered, 1)
        self.assertEqual(connection.transactions_exited, 1)
        fetchrow = next(call for call in connection.calls if call[0] == "fetchrow")
        self.assertIn("owner_user_id=$2::uuid", fetchrow[1])
        self.assertEqual(fetchrow[2], (template_id, OWNER))
        version_insert = [call for call in connection.calls if call[0] == "execute"][-1]
        self.assertEqual(version_insert[2][4], '{"type":"object"}')
        self.assertEqual(version_insert[2][6], '{"a":1,"z":2}')

    async def test_publish_existing_template_increments_locked_version(self):
        connection = FakeConnection(
            fetchrows=[{"form_template_id": uuid.uuid4()}],
            fetchvals=[7],
            executes=["UPDATE 1", "INSERT 0 1"],
        )
        repository = PostgresLifeSwitchFormsRepository(connection)
        version = await repository.publish_form(
            owner=OWNER,
            template_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            name="Daily",
            json_schema={},
            ui_schema={},
            metadata={},
        )
        self.assertEqual(version, 7)
        self.assertTrue(any(call[0] == "fetchval" for call in connection.calls))
        self.assertTrue(any("updated_at=now()" in call[1] for call in connection.calls if call[0] == "execute"))

    async def test_reads_bind_owner_and_dynamic_filters_inside_adapter(self):
        version_id = uuid.uuid4()
        connection = FakeConnection(fetches=[[], []], fetchrows=[None, None])
        repository = PostgresLifeSwitchFormsRepository(connection)
        self.assertEqual(await repository.list_templates(OWNER), [])
        self.assertIsNone(await repository.get_version(owner=OWNER, version_id=version_id))
        self.assertIsNone(await repository.get_entry_version(owner=OWNER, version_id=version_id))
        self.assertEqual(
            await repository.list_entries(
                owner=OWNER,
                subject_id="subject-1",
                version_id=version_id,
                limit=25,
            ),
            [],
        )
        list_call = [call for call in connection.calls if call[0] == "fetch"][-1]
        self.assertIn("subject_id=$2", list_call[1])
        self.assertIn("form_version_id=$3", list_call[1])
        self.assertIn("limit $4", list_call[1])
        self.assertEqual(list_call[2], (OWNER, "subject-1", version_id, 25))
        for call in connection.calls:
            if call[0] in {"fetch", "fetchrow"}:
                self.assertIn("owner_user_id", call[1])

    async def test_create_entry_serializes_data_and_binds_owner(self):
        connection = FakeConnection(executes=["INSERT 0 1"])
        repository = PostgresLifeSwitchFormsRepository(connection)
        entry_id = uuid.uuid4()
        version_id = uuid.uuid4()
        occurred_at = datetime.now(timezone.utc)
        await repository.create_entry(
            entry_id=entry_id,
            owner=OWNER,
            subject_id="subject-1",
            version_id=version_id,
            occurred_at=occurred_at,
            data={"z": 2, "a": 1},
        )
        execute = next(call for call in connection.calls if call[0] == "execute")
        self.assertIn("owner_user_id", execute[1])
        self.assertEqual(execute[2], (entry_id, OWNER, "subject-1", version_id, occurred_at, '{"a":1,"z":2}'))

    async def test_delete_returns_cascade_counts_or_fails_closed(self):
        template_id = uuid.uuid4()
        connection = FakeConnection(
            fetchrows=[{"version_count": 2, "entry_count": 5}],
            executes=["DELETE 1"],
        )
        repository = PostgresLifeSwitchFormsRepository(connection)
        deleted = await repository.delete_template(owner=OWNER, template_id=template_id)
        self.assertEqual((deleted.entries, deleted.versions, deleted.templates), (5, 2, 1))
        self.assertEqual(connection.transactions_entered, 1)

        missing = FakeConnection(
            fetchrows=[{"version_count": 0, "entry_count": 0}],
            fetchvals=[None],
        )
        with self.assertRaises(FormsTemplateNotFoundError):
            await PostgresLifeSwitchFormsRepository(missing).delete_template(
                owner=OWNER,
                template_id=template_id,
            )
        self.assertFalse(any(call[0] == "execute" for call in missing.calls))

    async def test_repository_context_closes_connection(self):
        connection = FakeConnection()
        request = Request({"type": "http", "headers": []})

        async def connect(received_request):
            self.assertIs(received_request, request)
            return connection

        async with lifeswitch_forms_repository(
            request,
            connection_factory=connect,
        ) as repository:
            self.assertIsInstance(repository, PostgresLifeSwitchFormsRepository)
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
