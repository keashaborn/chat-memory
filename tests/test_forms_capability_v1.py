from __future__ import annotations

import ast
import os
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


@asynccontextmanager
async def repository_context(repository):
    yield repository


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

    def test_router_has_no_database_effect_or_connection_ownership(self):
        path = ROOT / "seebx/capabilities/forms/routes.py"
        source = path.read_text()
        tree = ast.parse(source)
        forbidden = {"execute", "fetch", "fetchrow", "fetchval", "transaction"}
        effects = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in forbidden
        ]
        self.assertEqual(effects, [])
        self.assertIn("lifeswitch_forms_repository", source)
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotIn("POSTGRES_DSN", source)
        self.assertNotIn("vb_form_", source)

    async def test_list_rejects_actor_owner_mismatch_before_repository(self):
        repository_factory = Mock()
        denied = HTTPException(status_code=403, detail="supabase_actor_owner_mismatch")
        with (
            patch.object(routes, "require_actor", AsyncMock(side_effect=denied)),
            patch.object(routes, "lifeswitch_forms_repository", repository_factory),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.list_templates(request(OTHER), OWNER)
        self.assertEqual(caught.exception.status_code, 403)
        repository_factory.assert_not_called()

    async def test_template_list_serializes_repository_rows(self):
        template_id = uuid.uuid4()
        version_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        repository = Mock()
        repository.list_templates = AsyncMock(return_value=[{
            "template_id": template_id,
            "name": "Daily check-in",
            "status": "published",
            "created_at": now,
            "latest_version_id": version_id,
            "latest_version": 2,
            "latest_version_created_at": now,
        }])
        with (
            patch.object(routes, "require_actor", AsyncMock(return_value=OWNER)),
            patch.object(
                routes,
                "lifeswitch_forms_repository",
                side_effect=lambda _req: repository_context(repository),
            ),
        ):
            result = await routes.list_templates(request(OWNER), OWNER)
        self.assertEqual(result[0].template_id, str(template_id))
        self.assertEqual(result[0].latest_version_id, str(version_id))
        self.assertEqual(result[0].latest_version, 2)
        repository.list_templates.assert_awaited_once_with(OWNER)

    async def test_version_requires_actor_and_binds_repository_to_actor(self):
        denied = HTTPException(status_code=401, detail="missing_supabase_access_token")
        with patch.object(
            routes,
            "require_request_actor",
            AsyncMock(side_effect=denied),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.get_version(request(None), VERSION)
        self.assertEqual(caught.exception.status_code, 401)

        repository = Mock()
        repository.get_version = AsyncMock(return_value=None)
        with (
            patch.object(
                routes,
                "require_request_actor",
                AsyncMock(return_value=OWNER),
            ),
            patch.object(
                routes,
                "lifeswitch_forms_repository",
                side_effect=lambda _req: repository_context(repository),
            ),
        ):
            with self.assertRaises(HTTPException) as missing:
                await routes.get_version(request(OWNER), VERSION)
        self.assertEqual(missing.exception.status_code, 404)
        repository.get_version.assert_awaited_once_with(
            owner=OWNER,
            version_id=uuid.UUID(VERSION),
        )

    async def test_entry_owner_mismatch_is_rejected_before_repository(self):
        payload = CreateEntryRequest(
            owner_user_id=OWNER,
            subject_id="subject-1",
            template_version_id=VERSION,
            data={"value": 1},
        )
        repository_factory = Mock()
        denied = HTTPException(status_code=403, detail="supabase_actor_owner_mismatch")
        with (
            patch.object(routes, "require_actor", AsyncMock(side_effect=denied)),
            patch.object(routes, "lifeswitch_forms_repository", repository_factory),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.create_entry(request(OTHER), payload)
        self.assertEqual(caught.exception.status_code, 403)
        repository_factory.assert_not_called()

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
        migration = (ROOT / "ops/sql/20260819_lifeswitch_forms_v1.sql").read_text()
        self.assertEqual(migration.count("FORCE ROW LEVEL SECURITY"), 3)
        self.assertEqual(
            migration.count("FOR ALL TO lifeswitch_app, lifeswitch_owner"),
            3,
        )
        self.assertEqual(migration.count("WITH CHECK ("), 3)
        self.assertIn("REVOKE ALL ON SCHEMA lifeswitch_forms FROM PUBLIC", migration)
        self.assertNotIn(" TO anon", migration)
        self.assertNotIn(" TO authenticated", migration)
        self.assertIn("FOREIGN KEY (form_template_id, owner_user_id)", migration)
        self.assertIn("FOREIGN KEY (form_version_id, owner_user_id)", migration)


if __name__ == "__main__":
    unittest.main()
