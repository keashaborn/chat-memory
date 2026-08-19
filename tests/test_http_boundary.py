from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seebx.core.http_boundary import (
    SENSITIVE_NO_STORE_HEADERS,
    install_http_boundary,
    service_token_required,
)


def application() -> FastAPI:
    app = FastAPI()
    install_http_boundary(app)

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    @app.get("/admin/probe")
    async def admin_probe():
        return {"ok": True}

    return app


class HttpBoundaryTests(unittest.TestCase):
    def test_service_token_decision_preserves_public_surface(self) -> None:
        self.assertFalse(service_token_required("/openapi.json", "GET"))
        self.assertFalse(service_token_required("/docs", "GET"))
        self.assertFalse(service_token_required("/openapi-extra", "POST"))
        self.assertFalse(service_token_required("/catalog/item", "GET"))
        self.assertTrue(service_token_required("/catalog/item", "POST"))
        self.assertTrue(service_token_required("/protected", "GET"))

    def test_missing_configuration_fails_closed(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = TestClient(application()).get(
                "/protected",
                headers={"x-request-id": "request-1"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "status": "unavailable",
                "detail": "service_token_not_configured",
            },
        )
        self.assertEqual(response.headers["x-request-id"], "request-1")

    def test_invalid_token_fails_and_valid_token_passes(self) -> None:
        with patch.dict(
            "os.environ",
            {"VS_SERVICE_TOKEN": "synthetic-secret"},
            clear=False,
        ):
            client = TestClient(application())
            denied = client.get(
                "/protected",
                headers={
                    "x-vs-service-token": "wrong",
                    "x-request-id": "request-2",
                },
            )
            allowed = client.get(
                "/protected",
                headers={
                    "x-vs-service-token": "synthetic-secret",
                    "x-request-id": "request-3",
                },
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            denied.json()["detail"],
            "missing_or_invalid_service_token",
        )
        self.assertEqual(denied.headers["x-request-id"], "request-2")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["x-request-id"], "request-3")

    def test_sensitive_response_receives_complete_no_store_boundary(self) -> None:
        with patch.dict(
            "os.environ",
            {"VS_SERVICE_TOKEN": "synthetic-secret"},
            clear=False,
        ):
            response = TestClient(application()).get(
                "/admin/probe",
                headers={"x-vs-service-token": "synthetic-secret"},
            )
        self.assertEqual(response.status_code, 200)
        for name, value in SENSITIVE_NO_STORE_HEADERS.items():
            self.assertEqual(response.headers[name], value)

    def test_boundary_cannot_be_installed_twice(self) -> None:
        app = FastAPI()
        install_http_boundary(app)
        with self.assertRaisesRegex(
            RuntimeError,
            "seebx_http_boundary_already_installed",
        ):
            install_http_boundary(app)


if __name__ == "__main__":
    unittest.main()
