from __future__ import annotations

import ast
from pathlib import Path
import unittest
import uuid

from fastapi import HTTPException
from starlette.requests import Request

from seebx.capabilities.measurements import routes as measurements
from seebx.capabilities.nutrition import logs as nutrition
from seebx.capabilities.plans import routes as plans
from seebx.capabilities.training import exercises as training


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_MODULES = (
    "seebx/capabilities/measurements/routes.py",
    "seebx/capabilities/nutrition/foods.py",
    "seebx/capabilities/nutrition/logs.py",
    "seebx/capabilities/nutrition/meal_plans.py",
    "seebx/capabilities/nutrition/meals.py",
    "seebx/capabilities/plans/routes.py",
    "seebx/capabilities/training/conditioning.py",
    "seebx/capabilities/training/exercises.py",
    "seebx/capabilities/training/sessions.py",
    "seebx/capabilities/training/sharing.py",
    "seebx/capabilities/training/templates.py",
)
CANONICAL_CALLS = {"require_actor", "require_request_actor"}


class LifeSwitchDomainVerifiedIdentityTests(unittest.TestCase):
    def test_domain_modules_do_not_consume_the_raw_actor_header_helper(self) -> None:
        for relative in DOMAIN_MODULES:
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                self.assertNotIn("require_actor_matches_owner", source)
                self.assertIn("seebx.core.identity", source)

    def test_every_canonical_identity_call_is_awaited(self) -> None:
        for relative in DOMAIN_MODULES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in CANONICAL_CALLS
            ]
            with self.subTest(module=relative):
                self.assertTrue(calls)
                for call in calls:
                    self.assertIsInstance(parents.get(call), ast.Await)

    def test_database_owner_callbacks_use_only_verified_actor_values(self) -> None:
        callback_modules = (
            "seebx/capabilities/nutrition/meal_plans.py",
            "seebx/capabilities/nutrition/meals.py",
            "seebx/capabilities/training/templates.py",
        )
        for relative in callback_modules:
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                self.assertIn("await require_request_actor(req)", source)
                self.assertIn("require_verified_actor_matches_owner", source)


OWNER = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))


def raw_actor_request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-vs-actor-user-id", OWNER.encode("ascii"))],
    })


class RawActorHeaderRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def assert_raw_actor_rejected(self, operation) -> None:
        with self.assertRaises(HTTPException) as rejected:
            await operation
        self.assertEqual(rejected.exception.status_code, 401)
        self.assertEqual(
            rejected.exception.detail,
            "missing_or_invalid_supabase_bearer",
        )

    async def test_plan_rejects_raw_actor_before_repository_access(self) -> None:
        await self.assert_raw_actor_rejected(
            plans.get_plan_profile(raw_actor_request(), OWNER, 0, "")
        )

    async def test_nutrition_rejects_raw_actor_before_repository_access(self) -> None:
        await self.assert_raw_actor_rejected(
            nutrition.get_log_day(raw_actor_request(), OWNER, "2026-08-21", "")
        )

    async def test_training_rejects_raw_actor_before_repository_access(self) -> None:
        await self.assert_raw_actor_rejected(
            training.list_my_exercises(raw_actor_request(), OWNER, 0)
        )

    async def test_measurements_rejects_raw_actor_before_repository_access(self) -> None:
        await self.assert_raw_actor_rejected(
            measurements.list_measurement_entries(
                raw_actor_request(), OWNER, 90, 0, ""
            )
        )


if __name__ == "__main__":
    unittest.main()
