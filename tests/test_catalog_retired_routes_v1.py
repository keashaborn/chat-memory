from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "seebx/capabilities/catalog/routes.py"


class CatalogRetiredRoutesV1Tests(unittest.TestCase):
    def test_only_named_product_catalog_routes_remain(self) -> None:
        source = CATALOG.read_text(encoding="utf-8")
        retained = (
            "/exercises/search",
            "/exercises/browse",
            "/foods/usda/barcode",
            "/foods/usda/guide",
        )
        retired = (
            "/foods/search",
            "/foods/by_barcode",
            "/foods/usda/search",
            "/foods/usda/import",
            "/foods/approve",
        )
        for route in retained:
            self.assertEqual(source.count(route), 1, route)
        for route in retired:
            self.assertNotIn(route, source, route)
        self.assertEqual(source.count("@router."), len(retained))

    def test_catalog_router_has_no_platform_database_or_hidden_write_path(self) -> None:
        source = CATALOG.read_text(encoding="utf-8")
        for residue in (
            "POSTGRES_DSN",
            "CATALOG_SCHEMA",
            "DEFAULT_PUBLIC_IMPORT",
            "asyncpg",
            "Open Food Facts",
            "insert into",
            "update ",
        ):
            self.assertNotIn(residue, source, residue)


if __name__ == "__main__":
    unittest.main()
