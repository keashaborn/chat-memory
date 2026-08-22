from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "seebx/capabilities/catalog/routes.py"
NUTRITION = ROOT / "seebx/capabilities/nutrition/foods.py"
ADAPTER = ROOT / "seebx/adapters/usda_fdc.py"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found")


class UsdaProviderBoundaryTests(unittest.TestCase):
    def test_active_catalog_routes_use_only_canonical_provider(self):
        for name in ("usda_food_barcode", "usda_food_guide"):
            route = function_source(CATALOG, name)
            with self.subTest(name=name):
                self.assertIn("usda_fdc_client", route)
                self.assertNotIn("requests", route)
                self.assertNotIn("USDA_API_KEY", route)
                self.assertNotIn("api.nal.usda.gov", route)
        adapter = ADAPTER.read_text(encoding="utf-8")
        self.assertEqual(adapter.count("https://api.nal.usda.gov"), 2)
        guide = function_source(CATALOG, "usda_food_guide")
        self.assertIn("asyncio.Semaphore(6)", guide)
        self.assertIn("asyncio.Semaphore(8)", guide)

    def test_owner_scoped_import_authenticates_before_provider_and_database(self):
        route = function_source(NUTRITION, "create_my_food_from_usda")
        actor = route.index("await require_actor")
        provider = route.index("usda_fdc_client")
        database = route.index("lifeswitch_foods_repository")
        self.assertLess(actor, provider)
        self.assertLess(provider, database)
        self.assertNotIn("requests", route)
        self.assertNotIn("USDA_API_KEY", route)
        self.assertNotIn("api.nal.usda.gov", route)
        self.assertNotIn("def _nutr_amount", route)
        self.assertIn("usda_nutrient_summary", route)

    def test_provider_adapter_has_no_database_or_fastapi_authority(self):
        source = ADAPTER.read_text(encoding="utf-8")
        for forbidden in (
            "asyncpg",
            "POSTGRES_DSN",
            "LIFESWITCH_POSTGRES_DSN",
            "fastapi",
            "HTTPException",
            "owner_user_id",
            "insert into",
            "update ",
            "delete from",
        ):
            self.assertNotIn(forbidden, source)


class FakeUsdaClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.detail_calls: list[int] = []

    async def search(self, query, *, page_size, data_types=()):
        self.search_calls.append((query, page_size, tuple(data_types)))
        return [
            {
                "fdcId": 91,
                "description": "Test Food",
                "brandOwner": "Test Brand",
                "gtinUpc": "001234567890",
                "dataType": "Branded",
                "score": 99.0,
            }
        ]

    async def detail(self, fdc_id):
        self.detail_calls.append(int(fdc_id))
        return {
            "fdcId": int(fdc_id),
            "description": "Test Food",
            "brandOwner": "Test Brand",
            "gtinUpc": "001234567890",
            "dataType": "Branded",
            "servingSize": 100,
            "servingSizeUnit": "g",
            "foodNutrients": [
                {"nutrient": {"number": "208"}, "amount": 170},
                {"nutrient": {"number": "203"}, "amount": 20},
                {"nutrient": {"number": "205"}, "amount": 0},
                {"nutrient": {"number": "204"}, "amount": 10},
            ],
        }


class UsdaActiveRouteBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_barcode_preserves_public_response_contract(self):
        from seebx.capabilities.catalog import routes

        client = FakeUsdaClient()
        with patch.object(routes, "usda_fdc_client", return_value=client):
            response = await routes.usda_food_barcode("001234567890", 5)

        payload = json.loads(response.body)
        self.assertEqual(payload["upc"], "001234567890")
        self.assertEqual(payload["match_type"], "exact")
        self.assertEqual(len(payload["candidates"]), 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["fdc_id"], 91)
        self.assertEqual(candidate["basis"], "per_100g")
        self.assertEqual(candidate["nutrients"]["kcal"], 170.0)
        self.assertEqual(candidate["import"], {"fdc_id": 91})
        self.assertEqual(client.search_calls, [("001234567890", 15, ("Branded",))])
        self.assertEqual(client.detail_calls, [91])

    async def test_guide_preserves_public_response_contract(self):
        from seebx.capabilities.catalog import routes

        client = FakeUsdaClient()
        with patch.object(routes, "usda_fdc_client", return_value=client):
            response = await routes.usda_food_guide("  Test   Food  ", 1)

        payload = json.loads(response.body)
        self.assertEqual(payload["query"], "Test Food")
        self.assertEqual(payload["search_errors"], [])
        self.assertEqual(len(payload["candidates"]), 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["fdc_id"], 91)
        self.assertEqual(candidate["basis"], "per_100g")
        self.assertEqual(candidate["nutrients"]["protein_g"], 20.0)
        self.assertEqual(candidate["import"], {"fdc_id": 91})
        self.assertGreaterEqual(len(client.search_calls), 1)
        self.assertEqual(client.detail_calls, [91])


if __name__ == "__main__":
    unittest.main()
