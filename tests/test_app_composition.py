from __future__ import annotations

import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class AppCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                )
            },
            clear=False,
        ):
            cls.backend = importlib.import_module("app")

    def test_composition_root_has_no_direct_route_decorators(self) -> None:
        source = (ROOT / "app.py").read_text()
        self.assertNotIn("@app.get(", source)
        self.assertNotIn("@app.post(", source)
        self.assertNotIn("@app.put(", source)
        self.assertNotIn("@app.patch(", source)
        self.assertNotIn("@app.delete(", source)
        self.assertNotIn("@app.middleware(", source)

    def test_only_fastapi_owns_openapi_route(self) -> None:
        routes = [
            route
            for route in self.backend.app.routes
            if getattr(route, "path", None) == "/openapi.json"
        ]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].name, "openapi")
        self.assertEqual(routes[0].endpoint.__module__, "fastapi.applications")

    def test_platform_boundary_and_health_have_canonical_owners(self) -> None:
        source = (ROOT / "app.py").read_text()
        self.assertIn("install_http_boundary(app)", source)
        self.assertIn("create_operational_health_router(", source)
        self.assertNotIn("service_token_middleware", source)
        self.assertNotIn("async def health(", source)
        self.assertNotIn("async def readyz(", source)


if __name__ == "__main__":
    unittest.main()
