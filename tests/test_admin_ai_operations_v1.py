from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from seebx.adapters.ai_operations_postgres import (
    ACKNOWLEDGE_INCIDENT_SQL,
    LIST_INCIDENTS_SQL,
    RESOLVE_INCIDENT_SQL,
    SET_ACTOR_SQL,
    SET_CAPABILITY_SQL,
    PostgresAiOperationsRepository,
)
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.operations.ai_operations import (
    AiOperationsError,
    acknowledge_admin_ai_operations_incident_v1,
    list_admin_ai_operations_incidents_v1,
    resolve_admin_ai_operations_incident_v1,
)
from seebx.capabilities.operations.ai_operations_routes import (
    create_ai_operations_router,
)


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
CAPABILITY = ROOT / "seebx/capabilities/operations/ai_operations.py"
ADAPTER = ROOT / "seebx/adapters/ai_operations_postgres.py"
ROUTES = ROOT / "seebx/capabilities/operations/ai_operations_routes.py"
ACTOR = str(uuid4())
INCIDENT = str(uuid4())
DSN = "postgresql://private"


def incident(**overrides):
    value = {
        "incident_id": INCIDENT,
        "monitor_name": "trusted_web_monitor_v1",
        "state": "open",
        "severity": "warning",
        "is_drill": False,
        "observation_status": "violated",
        "first_seen_at": "2026-07-31T12:00:00+00:00",
        "last_seen_at": "2026-07-31T12:05:00+00:00",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "resolved_at": None,
        "resolved_by": None,
        "observation_count": 2,
        "reason_codes": ["fail_closed_rate"],
        "window_hours": 24,
        "request_count": 10,
        "completed_count": 8,
        "fail_closed_count": 2,
        "relevance_fail_closed_count": 1,
        "dependency_failure_count": 0,
        "fail_closed_rate": 0.2,
    }
    value.update(overrides)
    return value


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, result):
        self.result = result
        self.execute_calls = []
        self.fetchval_calls = []
        self.closed = False

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        if isinstance(self.result, BaseException):
            raise self.result
        return json.dumps(self.result)

    async def close(self):
        self.closed = True


def repository(result):
    connection = FakeConnection(result)

    async def connect(_dsn, **kwargs):
        if kwargs != {"command_timeout": 10, "timeout": 5}:
            raise AssertionError("unexpected connection bounds")
        return connection

    provider = PostgresConnectionProvider(
        DSN,
        connect_factory=connect,
        connect_kwargs={"command_timeout": 10, "timeout": 5},
    )
    return PostgresAiOperationsRepository(provider), connection


class UnexpectedRepository:
    def __getattr__(self, _name):
        raise AssertionError("repository should not be reached")


class FakePostgresError(RuntimeError):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class AdminAiOperationsV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_list_is_actor_and_inspector_bound(self):
        store, connection = repository(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident()],
            }
        )
        payload = await list_admin_ai_operations_incidents_v1(
            repository=store,
            actor_user_id=ACTOR,
            state="open",
            limit=25,
        )

        self.assertEqual(payload["schema"], "admin_ai_operations_incidents_v1")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(
            connection.execute_calls,
            [
                (SET_ACTOR_SQL, (ACTOR,)),
                (SET_CAPABILITY_SQL, ("inspector.view",)),
            ],
        )
        self.assertEqual(connection.fetchval_calls[0][0], LIST_INCIDENTS_SQL)
        self.assertEqual(connection.fetchval_calls[0][1], ("open", 25))
        self.assertTrue(connection.closed)

    async def test_mutations_bind_actor_and_manager_capability(self):
        for action, call, sql in (
            (
                "acknowledged",
                acknowledge_admin_ai_operations_incident_v1,
                ACKNOWLEDGE_INCIDENT_SQL,
            ),
            (
                "resolved",
                resolve_admin_ai_operations_incident_v1,
                RESOLVE_INCIDENT_SQL,
            ),
        ):
            store, connection = repository(
                {
                    "contract_version": "ai_operations_monitor_mutation_v1",
                    "action": action,
                    "incident_id": INCIDENT,
                    "state": action,
                }
            )
            payload = await call(
                repository=store,
                actor_user_id=ACTOR,
                incident_id=INCIDENT,
            )
            self.assertEqual(payload["action"], action)
            self.assertEqual(
                connection.execute_calls[1],
                (SET_CAPABILITY_SQL, ("incident.manage",)),
            )
            self.assertEqual(connection.fetchval_calls[0][0], sql)
            self.assertEqual(
                connection.fetchval_calls[0][1],
                (INCIDENT, ACTOR),
            )
            self.assertTrue(connection.closed)

    async def test_inputs_are_bounded_before_repository_access(self):
        unavailable = UnexpectedRepository()
        for kwargs in (
            {"state": "all", "limit": 50},
            {"state": None, "limit": 0},
            {"state": None, "limit": 101},
        ):
            with self.assertRaises(ValueError):
                await list_admin_ai_operations_incidents_v1(
                    repository=unavailable,
                    actor_user_id=ACTOR,
                    **kwargs,
                )
        with self.assertRaises(ValueError):
            await acknowledge_admin_ai_operations_incident_v1(
                repository=unavailable,
                actor_user_id=ACTOR,
                incident_id="not-a-uuid",
            )

    async def test_database_errors_are_safely_mapped(self):
        for sqlstate, code, status in (
            ("P0002", "monitor_incident_not_found", 404),
            ("22023", "invalid_incident_transition", 409),
            ("XX000", "ai_operations_unavailable", 500),
        ):
            store, connection = repository(FakePostgresError(sqlstate))
            with self.assertRaises(AiOperationsError) as ctx:
                await acknowledge_admin_ai_operations_incident_v1(
                    repository=store,
                    actor_user_id=ACTOR,
                    incident_id=INCIDENT,
                )
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.status_code, status)
            self.assertTrue(connection.closed)

    async def test_unexpected_metadata_is_rejected(self):
        store, _connection = repository(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident(source_url="https://example.invalid")],
            }
        )
        with self.assertRaises(AiOperationsError) as ctx:
            await list_admin_ai_operations_incidents_v1(
                repository=store,
                actor_user_id=ACTOR,
            )
        self.assertEqual(ctx.exception.code, "ai_operations_contract_invalid")

        store, _connection = repository(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident(incident_id=None)],
            }
        )
        with self.assertRaises(AiOperationsError) as ctx:
            await list_admin_ai_operations_incidents_v1(
                repository=store,
                actor_user_id=ACTOR,
            )
        self.assertEqual(ctx.exception.code, "ai_operations_contract_invalid")

    def test_capability_is_database_effect_free_and_adapter_owns_six_effects(self):
        methods = {"execute", "fetch", "fetchrow", "fetchval", "transaction"}

        def effects(path: Path) -> list[tuple[str, int]]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            return [
                (node.func.attr, node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ]

        self.assertEqual(effects(CAPABILITY), [])
        self.assertEqual(len(effects(ADAPTER)), 6)
        self.assertNotIn("asyncpg", CAPABILITY.read_text(encoding="utf-8"))

    def test_routes_have_one_capability_owner_and_composed_adapter(self):
        app_source = APP.read_text(encoding="utf-8")
        routes = ROUTES.read_text(encoding="utf-8")

        self.assertIn('@router.get("/admin/ai-operations/incidents")', routes)
        self.assertIn(
            '"/admin/ai-operations/incidents/{incident_id}/acknowledge"',
            routes,
        )
        self.assertIn(
            '"/admin/ai-operations/incidents/{incident_id}/resolve"',
            routes,
        )
        self.assertIn("require_verified_supabase_request_identity", routes)
        self.assertIn(
            'request.headers.get("x-vs-authorized-capability")',
            routes,
        )
        self.assertNotIn("BaseModel", routes)
        self.assertNotIn("actor_user_id:", routes)
        self.assertIn("PostgresAiOperationsRepository", app_source)
        self.assertIn("create_ai_operations_router(AI_OPERATIONS)", app_source)
        self.assertNotIn('@app.get("/admin/ai-operations/incidents")', app_source)
        self.assertNotIn("_require_ai_operations_actor", app_source)


class AdminAiOperationsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SimpleNamespace()
        app = FastAPI()
        app.include_router(create_ai_operations_router(self.repository))
        self.client = TestClient(app)

    def test_missing_verified_bearer_is_rejected(self) -> None:
        response = self.client.get(
            "/admin/ai-operations/incidents",
            headers={
                "x-vs-actor-user-id": ACTOR,
                "x-vs-authorized-capability": "inspector.view",
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"],
            "missing_or_invalid_supabase_bearer",
        )
        self.assertEqual(
            response.headers["cache-control"].split(",")[0],
            "private",
        )

    def test_verified_actor_and_capability_reach_operation(self) -> None:
        expected = {"ok": True, "items": []}
        with (
            patch(
                "seebx.capabilities.operations.ai_operations_routes."
                "require_verified_supabase_request_identity",
                new=AsyncMock(
                    return_value=SimpleNamespace(actor_user_id=ACTOR)
                ),
            ),
            patch(
                "seebx.capabilities.operations.ai_operations_routes."
                "list_admin_ai_operations_incidents_v1",
                new=AsyncMock(return_value=expected),
            ) as operation,
        ):
            response = self.client.get(
                "/admin/ai-operations/incidents?state=open&limit=25",
                headers={
                    "authorization": "Bearer synthetic-test-token",
                    "x-vs-actor-user-id": ACTOR,
                    "x-vs-authorized-capability": "inspector.view",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        operation.assert_awaited_once_with(
            repository=self.repository,
            actor_user_id=ACTOR,
            state="open",
            limit=25,
        )

    def test_verified_actor_without_capability_is_rejected(self) -> None:
        with patch(
            "seebx.capabilities.operations.ai_operations_routes."
            "require_verified_supabase_request_identity",
            new=AsyncMock(return_value=SimpleNamespace(actor_user_id=ACTOR)),
        ):
            response = self.client.get(
                "/admin/ai-operations/incidents",
                headers={
                    "authorization": "Bearer synthetic-test-token",
                    "x-vs-actor-user-id": ACTOR,
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "capability_required")


if __name__ == "__main__":
    unittest.main()
