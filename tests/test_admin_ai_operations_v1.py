from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
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
ROUTES = ROOT / "seebx/capabilities/operations/ai_operations_routes.py"
ACTOR = str(uuid4())
INCIDENT = str(uuid4())


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


def connector(result):
    conn = FakeConnection(result)

    async def connect(_dsn, **kwargs):
        if kwargs != {"command_timeout": 10, "timeout": 5}:
            raise AssertionError("unexpected connection bounds")
        return conn

    return connect, conn


class FakePostgresError(RuntimeError):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class AdminAiOperationsV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_list_is_actor_and_inspector_bound(self):
        connect, conn = connector(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident()],
            }
        )
        payload = await list_admin_ai_operations_incidents_v1(
            dsn="postgresql://private",
            actor_user_id=ACTOR,
            state="open",
            limit=25,
            connect=connect,
        )

        self.assertEqual(payload["schema"], "admin_ai_operations_incidents_v1")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(
            conn.execute_calls,
            [
                (
                    "SELECT set_config('app.user_id',$1,true)",
                    (ACTOR,),
                ),
                (
                    "SELECT set_config('app.ai_operations_capability',$1,true)",
                    ("inspector.view",),
                ),
            ],
        )
        self.assertIn("list_monitor_incidents_v1", conn.fetchval_calls[0][0])
        self.assertEqual(conn.fetchval_calls[0][1], ("open", 25))
        self.assertTrue(conn.closed)

    async def test_mutations_bind_actor_and_manager_capability(self):
        for action, call, function_name in (
            (
                "acknowledged",
                acknowledge_admin_ai_operations_incident_v1,
                "acknowledge_monitor_incident_v1",
            ),
            (
                "resolved",
                resolve_admin_ai_operations_incident_v1,
                "resolve_monitor_incident_v1",
            ),
        ):
            connect, conn = connector(
                {
                    "contract_version": "ai_operations_monitor_mutation_v1",
                    "action": action,
                    "incident_id": INCIDENT,
                    "state": action,
                }
            )
            payload = await call(
                dsn="postgresql://private",
                actor_user_id=ACTOR,
                incident_id=INCIDENT,
                connect=connect,
            )
            self.assertEqual(payload["action"], action)
            self.assertEqual(
                conn.execute_calls[1][1],
                ("incident.manage",),
            )
            self.assertIn(function_name, conn.fetchval_calls[0][0])
            self.assertEqual(conn.fetchval_calls[0][1], (INCIDENT, ACTOR))
            self.assertTrue(conn.closed)

    async def test_inputs_are_bounded_before_connect(self):
        async def unexpected_connect(*_args, **_kwargs):
            raise AssertionError("database should not be reached")

        for kwargs in (
            {"state": "all", "limit": 50},
            {"state": None, "limit": 0},
            {"state": None, "limit": 101},
        ):
            with self.assertRaises(ValueError):
                await list_admin_ai_operations_incidents_v1(
                    dsn="postgresql://private",
                    actor_user_id=ACTOR,
                    connect=unexpected_connect,
                    **kwargs,
                )
        with self.assertRaises(ValueError):
            await acknowledge_admin_ai_operations_incident_v1(
                dsn="postgresql://private",
                actor_user_id=ACTOR,
                incident_id="not-a-uuid",
                connect=unexpected_connect,
            )

    async def test_database_errors_are_safely_mapped(self):
        for sqlstate, code, status in (
            ("P0002", "monitor_incident_not_found", 404),
            ("22023", "invalid_incident_transition", 409),
            ("XX000", "ai_operations_unavailable", 500),
        ):
            connect, conn = connector(FakePostgresError(sqlstate))
            with self.assertRaises(AiOperationsError) as ctx:
                await acknowledge_admin_ai_operations_incident_v1(
                    dsn="postgresql://private",
                    actor_user_id=ACTOR,
                    incident_id=INCIDENT,
                    connect=connect,
                )
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.status_code, status)
            self.assertTrue(conn.closed)

    async def test_unexpected_metadata_is_rejected(self):
        connect, _conn = connector(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident(source_url="https://example.invalid")],
            }
        )
        with self.assertRaises(AiOperationsError) as ctx:
            await list_admin_ai_operations_incidents_v1(
                dsn="postgresql://private",
                actor_user_id=ACTOR,
                connect=connect,
            )
        self.assertEqual(ctx.exception.code, "ai_operations_contract_invalid")

        connect, _conn = connector(
            {
                "contract_version": "ai_operations_monitor_inbox_v1",
                "items": [incident(incident_id=None)],
            }
        )
        with self.assertRaises(AiOperationsError) as ctx:
            await list_admin_ai_operations_incidents_v1(
                dsn="postgresql://private",
                actor_user_id=ACTOR,
                connect=connect,
            )
        self.assertEqual(ctx.exception.code, "ai_operations_contract_invalid")

    def test_routes_have_one_capability_owner_and_no_root_wrappers(self):
        app_source = APP.read_text(encoding="utf-8")
        routes = ROUTES.read_text(encoding="utf-8")

        self.assertIn(
            '@router.get("/admin/ai-operations/incidents")',
            routes,
        )
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
        self.assertIn("create_ai_operations_router", app_source)
        self.assertNotIn('@app.get("/admin/ai-operations/incidents")', app_source)
        self.assertNotIn("_require_ai_operations_actor", app_source)


class AdminAiOperationsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(create_ai_operations_router("postgresql://private"))
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
            dsn="postgresql://private",
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
