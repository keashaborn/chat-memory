from __future__ import annotations

import ast
from pathlib import Path
import unittest

from seebx.capabilities.nutrition.routes import router


ROOT = Path(__file__).resolve().parents[1]
NUTRITION = ROOT / "seebx/capabilities/nutrition"

EXPECTED_ROUTES = (
    ("GET", "/meal_plans", "list_meal_plans"),
    ("POST", "/meal_plans/create", "create_meal_plan"),
    ("POST", "/my_foods/create_from_usda", "create_my_food_from_usda"),
    ("POST", "/meal_plans/{meal_plan_id}/items/add", "add_item"),
    ("PATCH", "/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}", "update_meal_plan_item"),
    ("DELETE", "/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}", "delete_meal_plan_item"),
    ("GET", "/meal_plans/{meal_plan_id}/items", "list_items"),
    ("GET", "/my_foods", "list_my_foods"),
    ("POST", "/my_foods/create_from_catalog", "create_my_food_from_catalog"),
    ("PATCH", "/my_foods/{my_food_id}", "update_my_food"),
    ("POST", "/my_foods/{my_food_id}/deactivate", "deactivate_my_food"),
    ("GET", "/my_foods/{my_food_id}/servings", "list_my_food_servings"),
    ("POST", "/my_foods/{my_food_id}/servings/create", "create_my_food_serving"),
    ("PATCH", "/my_foods/{my_food_id}/servings/{my_food_serving_id}", "update_my_food_serving"),
    ("GET", "/my_food_overrides", "list_my_food_overrides"),
    ("POST", "/my_food_overrides/upsert", "upsert_my_food_override"),
    ("DELETE", "/my_food_overrides", "delete_my_food_override"),
)


class NutritionRouterSplitTests(unittest.TestCase):
    def test_composition_preserves_exact_route_order(self) -> None:
        actual = tuple(
            (next(iter(route.methods)), route.path, route.endpoint.__name__)
            for route in router.routes
        )
        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_composition_owns_no_implementation(self) -> None:
        tree = ast.parse((NUTRITION / "routes.py").read_text(encoding="utf-8"))
        implementations = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        self.assertEqual(implementations, [])

    def test_aggregate_modules_own_no_raw_database_effects(self) -> None:
        forbidden_calls = {"fetch", "fetchrow", "fetchval", "execute", "close"}
        for name in ("foods.py", "meal_plans.py"):
            path = NUTRITION / name
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            calls = [
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden_calls
            ]
            with self.subTest(module=name):
                self.assertEqual(calls, [])
                self.assertNotIn("asyncpg", source)
                self.assertNotIn("connect_lifeswitch", source)
                self.assertNotIn("@router", source)


if __name__ == "__main__":
    unittest.main()
