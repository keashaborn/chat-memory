from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seebx.capabilities.operations.health_routes import (
    create_operational_health_router,
)


class FakeReadinessProvider:
    def __init__(self, value: object = 1, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls = 0

    async def readiness_value(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def application(provider: FakeReadinessProvider) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_operational_health_router(
            provider,
            zep_prompt_mode="summary",
        )
    )
    return app


class OperationalHealthRouterTests(unittest.TestCase):
    def test_router_owns_exact_operational_surface(self) -> None:
        app = application(FakeReadinessProvider())
        actual = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in app.routes
            if route.path in {"/healthz", "/readyz"}
        }
        self.assertEqual(
            actual,
            {
                ("/healthz", ("GET",)),
                ("/readyz", ("GET",)),
            },
        )

    def test_health_payload_preserves_memory_disclosure(self) -> None:
        with patch(
            "seebx.capabilities.operations.health_routes.time.time",
            return_value=123.5,
        ):
            response = TestClient(
                application(FakeReadinessProvider())
            ).get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "time": 123.5,
                "memory": {
                    "provider": "zep",
                    "prompt_mode": "summary",
                    "chat_history_store": "postgres",
                    "chat_deletion_retains_memory": True,
                    "full_erasure_route": "/memory/chat-and-zep/clear",
                    "retired_governed_memory": {
                        "capture": "disabled",
                        "response_fallback": "disabled",
                        "lifecycle_commands": "disabled",
                        "erasure_proxy": "disabled",
                        "postgres_access": "disabled",
                    },
                },
            },
        )

    def test_readiness_passes_only_on_exact_select_one(self) -> None:
        provider = FakeReadinessProvider(1)
        response = TestClient(application(provider)).get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "postgres": True})
        self.assertEqual(provider.calls, 1)

    def test_readiness_fails_on_wrong_value(self) -> None:
        response = TestClient(
            application(FakeReadinessProvider(0))
        ).get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"ok": False, "postgres": "postgres select 1 failed"},
        )

    def test_readiness_maps_provider_error(self) -> None:
        response = TestClient(
            application(
                FakeReadinessProvider(error=RuntimeError("synthetic outage"))
            )
        ).get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"ok": False, "postgres": "synthetic outage"},
        )

    def test_empty_prompt_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "zep_prompt_mode_required"):
            create_operational_health_router(
                FakeReadinessProvider(),
                zep_prompt_mode=" ",
            )


if __name__ == "__main__":
    unittest.main()
