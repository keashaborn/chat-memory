from __future__ import annotations

import ast
import json
import os
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from seebx.capabilities.measurements import routes


ROOT = Path(__file__).resolve().parents[1]
OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
ENTRY = "33333333-3333-3333-3333-333333333333"


def request(actor: str | None) -> Request:
    headers = [] if actor is None else [(b"x-vs-actor-user-id", actor.encode())]
    return Request({"type": "http", "headers": headers})


@asynccontextmanager
async def repository_context(repository):
    yield repository


class MeasurementsCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_router_preserves_three_frontend_contracts(self):
        app = FastAPI()
        app.include_router(routes.router, prefix="/lifeswitch/measurements")
        contracts = {
            (route.path, frozenset(route.methods or set()))
            for route in app.routes
            if route.path.startswith("/lifeswitch/measurements")
        }
        self.assertEqual(
            contracts,
            {
                ("/lifeswitch/measurements/entries", frozenset({"GET"})),
                ("/lifeswitch/measurements/entries/create", frozenset({"POST"})),
                (
                    "/lifeswitch/measurements/entries/{measurement_entry_id}/deactivate",
                    frozenset({"POST"}),
                ),
            },
        )

    def test_capability_has_zero_database_effects_or_connection_ownership(self):
        path = ROOT / "seebx/capabilities/measurements/routes.py"
        source = path.read_text()
        tree = ast.parse(source)
        methods = {"execute", "fetch", "fetchrow", "fetchval", "transaction"}
        effects = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in methods
        ]
        self.assertEqual(effects, [])
        self.assertIn("lifeswitch_measurements_repository", source)
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotIn("PEOPLE_SCHEMA", source)

    async def test_owner_mismatch_fails_before_repository(self):
        repository_factory = Mock()
        denied = HTTPException(status_code=403, detail="owner_mismatch")
        with (
            patch.object(routes, "require_actor_matches_owner", Mock(side_effect=denied)),
            patch.object(routes, "lifeswitch_measurements_repository", repository_factory),
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes.list_measurement_entries(
                    request(OTHER),
                    OWNER,
                    90,
                    0,
                    "",
                )
        self.assertEqual(caught.exception.status_code, 403)
        repository_factory.assert_not_called()

    async def test_self_list_uses_owner_bound_repository_and_serializes(self):
        now = datetime.now(timezone.utc)
        entry_id = uuid.UUID(ENTRY)
        repository = Mock()
        repository.list_entries = AsyncMock(return_value=[{
            "measurement_entry_id": entry_id,
            "owner_user_id": uuid.UUID(OWNER),
            "local_date": date(2026, 8, 20),
            "weight_value": 205,
            "created_at": now,
        }])
        with (
            patch.object(routes, "require_actor_matches_owner", Mock(return_value=OWNER)),
            patch.object(
                routes,
                "lifeswitch_measurements_repository",
                side_effect=lambda _req: repository_context(repository),
            ),
        ):
            response = await routes.list_measurement_entries(
                request(OWNER),
                OWNER,
                25,
                0,
                "",
            )
        repository.list_entries.assert_awaited_once_with(
            owner_user_id=OWNER,
            limit=25,
            include_inactive=False,
        )
        payload = json.loads(response.body)
        self.assertEqual(payload[0]["measurement_entry_id"], ENTRY)
        self.assertEqual(payload[0]["local_date"], "2026-08-20")

    async def test_delegated_read_is_fail_closed_or_exactly_permission_bound(self):
        repository = Mock()
        repository.has_people_permission = AsyncMock(return_value=True)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as disabled:
                await routes._resolve_measurements_view_target(
                    repository,
                    OWNER,
                    OTHER,
                )
        self.assertEqual(disabled.exception.detail, "delegated_access_disabled")
        repository.has_people_permission.assert_not_awaited()

        with patch.dict(
            os.environ,
            {"LIFESWITCH_DELEGATED_READS_ENABLED": "1"},
            clear=False,
        ):
            target, delegated = await routes._resolve_measurements_view_target(
                repository,
                OWNER,
                OTHER,
            )
        self.assertEqual((target, delegated), (OTHER, True))
        repository.has_people_permission.assert_awaited_once_with(
            grantor_user_id=OTHER,
            grantee_user_id=OWNER,
            scope="measurements:view",
        )

    async def test_deactivate_maps_missing_row_to_stable_404(self):
        repository = Mock()
        repository.deactivate_entry = AsyncMock(return_value=None)
        with (
            patch.object(routes, "require_actor_matches_owner", Mock(return_value=OWNER)),
            patch.object(
                routes,
                "lifeswitch_measurements_repository",
                side_effect=lambda _req: repository_context(repository),
            ),
        ):
            with self.assertRaises(HTTPException) as missing:
                await routes.deactivate_measurement_entry(
                    ENTRY,
                    request(OWNER),
                    OWNER,
                )
        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(missing.exception.detail, "measurement entry not found")
        repository.deactivate_entry.assert_awaited_once_with(
            measurement_entry_id=ENTRY,
            owner_user_id=OWNER,
        )


if __name__ == "__main__":
    unittest.main()
