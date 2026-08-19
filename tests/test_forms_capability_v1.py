from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from seebx.capabilities.forms import routes
from seebx.capabilities.forms.routes import CreateEntryRequest


ROOT = Path(__file__).resolve().parents[1]
OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
VERSION = "33333333-3333-3333-3333-333333333333"


def request(actor: str | None) -> Request:
    headers = [] if actor is None else [(b"x-vs-actor-user-id", actor.encode())]
    return Request({"type": "http", "headers": headers})


class FakeConnection:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.fetchrow_calls = []
        self.closed = False

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def fetch(self, _query, *_args):
        return self.rows

    async def close(self):
        self.closed = True


class FormsCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_flag_defaults_off_and_accepts_explicit_true(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(routes.forms_enabled())
        with patch.dict(os.environ, {"SEEBX_FORMS_ENABLED": "true"}):
            self.assertTrue(routes.forms_enabled())

    def test_router_preserves_six_frontend_contracts(self):
        app = FastAPI()
        app.include_router(routes.router, prefix="/forms")
        contracts = {
            (route.path, frozenset(route.methods or set()))
            for route in app.routes
            if route.path.startswith("/forms")
        }
        expected = {
            ("/forms/publish", frozenset({"POST"})),
            ("/forms/templates/{owner_user_id}", frozenset({"GET"})),
            ("/forms/versions/{version_id}", frozenset({"GET"})),
            ("/forms/entries", frozenset({"POST"})),
            ("/forms/entries/list", frozenset({"GET"})),
            (
                "/forms/templates/{owner_user_id}/{template_id}",
                frozenset({"DELETE"}),
            ),
        }
        self.assertEqual(contracts, expected)

    def test_app_mount_is_default_off_and_explicit(self):
        source = (ROOT / "app.py").read_text()
        self.assertIn("if forms_enabled():", source)
        self.assertIn('app.include_router(forms_router, prefix="/forms")', source)

    def test_router_uses_isolated_database_and_no_platform_dsn(self):
        source = (ROOT / "seebx/capabilities/forms/routes.py").read_text()
        self.assertIn("connect_lifeswitch", source)
        self.assertIn("require_actor", source)
        self.assertIn("require_request_actor", source)
        self.assertNotIn("POSTGRES_DSN", source)
        self.assertNotIn("vb_form_", source)

    async def test_list_rejects_actor_owner_mismatch_before_database(self):
        connect = AsyncMock()
        denied = HTTPException(status_code=403, detail="supabase_actor_owner_mismatch")
        with (
            patch.object(routes, "require_actor", AsyncMock(side_effect=denied)),
            patch.object(routes, "connect_lifeswitch", connect),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.list_templates(request(OTHER), OWNER)
        self.assertEqual(caught.exception.status_code, 403)
        connect.assert_not_awaited()

    async def test_template_list_serializes_database_uuids(self):
        template_id = uuid.uuid4()
        version_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        conn = FakeConnection(
            rows=[
                {
                    "template_id": template_id,
                    "name": "Daily check-in",
                    "status": "published",
                    "created_at": now,
                    "latest_version_id": version_id,
                    "latest_version": 2,
                    "latest_version_created_at": now,
                }
            ]
        )
        with (
            patch.object(routes, "require_actor", AsyncMock(return_value=OWNER)),
            patch.object(
                routes,
                "connect_lifeswitch",
                AsyncMock(return_value=conn),
            ),
        ):
            result = await routes.list_templates(request(OWNER), OWNER)
        self.assertEqual(result[0].template_id, str(template_id))
        self.assertEqual(result[0].latest_version_id, str(version_id))
        self.assertEqual(result[0].latest_version, 2)
        self.assertTrue(conn.closed)

    async def test_version_requires_actor_and_binds_query_to_actor(self):
        denied = HTTPException(status_code=401, detail="missing_supabase_access_token")
        with patch.object(
            routes,
            "require_request_actor",
            AsyncMock(side_effect=denied),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.get_version(request(None), VERSION)
        self.assertEqual(caught.exception.status_code, 401)

        conn = FakeConnection(row=None)
        with (
            patch.object(
                routes,
                "require_request_actor",
                AsyncMock(return_value=OWNER),
            ),
            patch.object(
                routes,
                "connect_lifeswitch",
                AsyncMock(return_value=conn),
            ),
        ):
            with self.assertRaises(HTTPException) as missing:
                await routes.get_version(request(OWNER), VERSION)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(conn.fetchrow_calls[0][1][1], OWNER)
        self.assertIn("v.owner_user_id=$2::uuid", conn.fetchrow_calls[0][0])
        self.assertTrue(conn.closed)

    async def test_entry_owner_mismatch_is_rejected_before_database(self):
        payload = CreateEntryRequest(
            owner_user_id=OWNER,
            subject_id="subject-1",
            template_version_id=VERSION,
            data={"value": 1},
        )
        connect = AsyncMock()
        denied = HTTPException(status_code=403, detail="supabase_actor_owner_mismatch")
        with (
            patch.object(routes, "require_actor", AsyncMock(side_effect=denied)),
            patch.object(routes, "connect_lifeswitch", connect),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.create_entry(request(OTHER), payload)
        self.assertEqual(caught.exception.status_code, 403)
        connect.assert_not_awaited()

    def test_missing_json_schema_dependency_fails_closed(self):
        with patch.object(routes, "jsonschema", None):
            with self.assertRaises(HTTPException) as caught:
                routes._validate_json_schema({"type": "object"})
        self.assertEqual(caught.exception.status_code, 503)

    @unittest.skipIf(routes.jsonschema is None, "jsonschema not installed")
    def test_json_schema_and_entry_validation(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        routes._validate_json_schema(schema)
        routes._validate_entry(schema, {"value": 3})
        with self.assertRaises(HTTPException) as caught:
            routes._validate_entry(schema, {"value": "three"})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail, "schema_validation_failed")

        with self.assertRaises(HTTPException) as invalid_schema:
            routes._validate_json_schema({"type": "not-a-json-schema-type"})
        self.assertEqual(invalid_schema.exception.status_code, 422)
        self.assertEqual(invalid_schema.exception.detail, "invalid_json_schema")

    def test_schema_migration_forces_owner_rls_and_private_grants(self):
        migration = (
            ROOT / "ops/sql/20260819_lifeswitch_forms_v1.sql"
        ).read_text()
        self.assertEqual(
            migration.count("FORCE ROW LEVEL SECURITY"),
            3,
        )
        self.assertEqual(
            migration.count("FOR ALL TO lifeswitch_app, lifeswitch_owner"),
            3,
        )
        self.assertEqual(migration.count("WITH CHECK ("), 3)
        self.assertIn(
            "REVOKE ALL ON SCHEMA lifeswitch_forms FROM PUBLIC",
            migration,
        )
        self.assertNotIn(" TO anon", migration)
        self.assertNotIn(" TO authenticated", migration)
        self.assertIn(
            "FOREIGN KEY (form_template_id, owner_user_id)",
            migration,
        )
        self.assertIn(
            "FOREIGN KEY (form_version_id, owner_user_id)",
            migration,
        )


if __name__ == "__main__":
    unittest.main()
