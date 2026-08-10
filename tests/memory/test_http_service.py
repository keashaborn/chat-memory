from __future__ import annotations

import ast
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping
import unittest
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHttpException

from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.http_service import (
    EXPECTED_DATABASE_NAME,
    EXPECTED_DATABASE_ROLE,
    GovernedMemoryHttpServiceSettings,
    HttpServiceConfigurationError,
    HttpServicePreflightError,
    SERVICE_TOKEN_HEADER,
    create_governed_memory_http_service,
)
from tests.memory.test_http_api import (
    proposal_result,
    status_result,
)


ISSUER = "https://synthetic.supabase.invalid/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
POSTGRES_DSN = "postgresql://governed_memory_api:secret@db.invalid/governed_memory"
SERVICE_TOKEN = "dedicated-synthetic-service-token"
OWNER = UUID("11111111-1111-4111-8111-111111111111")
RESOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
HASH = "a" * 64


def active_settings(**overrides: object) -> GovernedMemoryHttpServiceSettings:
    values: dict[str, object] = {
        "mode": "on",
        "postgres_dsn": POSTGRES_DSN,
        "supabase_issuer": ISSUER,
        "supabase_jwks_url": JWKS_URL,
        "service_token": SERVICE_TOKEN,
    }
    values.update(overrides)
    return GovernedMemoryHttpServiceSettings(**values)  # type: ignore[arg-type]


async def asgi_request(
    app: FastAPI,
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    parsed = urlsplit(target)
    request_headers = {"host": "testserver", **dict(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    received = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw_body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    return (
        int(start["status"]),
        response_headers,
        json.loads(raw_body) if raw_body else None,
    )


def concrete_path(path: str) -> str:
    return (
        path.replace("{claim_id}", RESOURCE)
        .replace("{proposal_id}", RESOURCE)
        .replace("{operation_id}", RESOURCE)
    )


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> bool:
        return False


class FakeConnection:
    def __init__(self, *, database_name: str = EXPECTED_DATABASE_NAME) -> None:
        self.database_name = database_name
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchval_calls: list[str] = []
        self.reset_calls = 0
        self.type_codec_calls: list[dict[str, object]] = []
        self.jsonb_decoder: object | None = None

    async def fetchrow(self, query: str, *arguments: object) -> Mapping[str, object]:
        self.fetchrow_calls.append((query, arguments))
        if "current_database()" in query:
            return {
                "database_name": self.database_name,
                "session_user": EXPECTED_DATABASE_ROLE,
                "current_user": EXPECTED_DATABASE_ROLE,
                "can_login": True,
                "inherits": False,
                "is_superuser": False,
                "bypasses_rls": False,
            }
        if "relrowsecurity" in query:
            return {
                "table_count": 12,
                "exact_forced_rls": True,
                "api_has_direct_dml": False,
            }
        if "memory_private.read_status()" in query:
            return status_result()
        raise AssertionError("unexpected_fetchrow")

    async def fetch(
        self, query: str, *arguments: object
    ) -> list[Mapping[str, object]]:
        if "memory_private.list_proposals" not in query:
            raise AssertionError("unexpected_fetch")
        if not callable(self.jsonb_decoder):
            raise AssertionError("jsonb_decoder_not_configured")
        object_literal = self.jsonb_decoder(
            '"amber"'
        )
        result = proposal_result()
        result["object_literal"] = object_literal
        return [result]

    async def set_type_codec(
        self,
        type_name: str,
        *,
        schema: str,
        encoder: object,
        decoder: object,
    ) -> None:
        self.type_codec_calls.append(
            {
                "type_name": type_name,
                "schema": schema,
                "encoder": encoder,
                "decoder": decoder,
            }
        )
        if type_name == "jsonb":
            self.jsonb_decoder = decoder

    async def execute(self, query: str, *arguments: object) -> str:
        self.execute_calls.append((query, arguments))
        return "SELECT 1"

    async def fetchval(self, query: str) -> UUID:
        self.fetchval_calls.append(query)
        return OWNER

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def reset(self) -> None:
        self.reset_calls += 1


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self) -> _AsyncContext:
        self.acquire_calls += 1
        return _AsyncContext(self.connection)

    async def close(self) -> None:
        self.close_calls += 1


class RecordingPoolFactory:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> FakePool:
        self.calls.append(dict(kwargs))
        initializer = kwargs.get("init")
        if callable(initializer):
            await initializer(self.pool.connection)
        return self.pool


class RecordingActorResolverFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.resolve_calls: list[tuple[str, tuple[ActorScope, ...]]] = []

    def __call__(self, config: object, **kwargs: object):
        self.calls.append((config, dict(kwargs)))

        async def resolve(
            request: Request,
            scopes: tuple[ActorScope, ...],
        ) -> VerifiedActor:
            self.resolve_calls.append((request.url.path, scopes))
            return VerifiedActor(
                owner_user_id=OWNER,
                actor_id=OWNER,
                role=ActorRole.OWNER,
                scopes=scopes,
                authentication_manifest_sha256=HASH,
                authenticated_at=NOW,
            )

        return resolve


class RecordingFreshnessVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []

    async def __call__(self, request: Request, actor: VerifiedActor) -> None:
        self.calls.append((request.url.path, actor.owner_user_id))


class ServiceSettingsTests(unittest.TestCase):
    def test_off_is_exact_default_and_does_not_load_active_environment(self) -> None:
        settings = GovernedMemoryHttpServiceSettings.from_environment(
            {
                "GOVERNED_MEMORY_POSTGRES_DSN": "invalid-but-unused",
                "GOVERNED_MEMORY_SUPABASE_ISSUER": "invalid-but-unused",
                "GOVERNED_MEMORY_SERVICE_TOKEN": "invalid-but-unused",
            }
        )
        self.assertEqual(settings, GovernedMemoryHttpServiceSettings())
        for mode in ("OFF", "true", " off", "on ", ""):
            with self.subTest(mode=mode), self.assertRaises(
                HttpServiceConfigurationError
            ) as caught:
                GovernedMemoryHttpServiceSettings(mode=mode)
            self.assertEqual(
                caught.exception.code,
                "governed_memory_http_mode_invalid",
            )

    def test_off_rejects_hidden_active_configuration(self) -> None:
        with self.assertRaises(HttpServiceConfigurationError) as caught:
            GovernedMemoryHttpServiceSettings(
                mode="off",
                postgres_dsn=POSTGRES_DSN,
            )
        self.assertEqual(
            caught.exception.code,
            "governed_memory_off_configuration_must_be_empty",
        )

    def test_on_requires_complete_https_configuration_and_redacts_secrets(self) -> None:
        settings = active_settings()
        rendered = repr(settings)
        self.assertNotIn(POSTGRES_DSN, rendered)
        self.assertNotIn(SERVICE_TOKEN, rendered)
        config = settings.authentication_config()
        self.assertEqual(config.audience, "authenticated")
        self.assertEqual(config.service_token_header, SERVICE_TOKEN_HEADER)

        for field_name in (
            "postgres_dsn",
            "supabase_issuer",
            "supabase_jwks_url",
            "service_token",
        ):
            with self.subTest(field=field_name), self.assertRaises(
                HttpServiceConfigurationError
            ):
                active_settings(**{field_name: None})
        with self.assertRaises(HttpServiceConfigurationError) as insecure:
            active_settings(
                supabase_issuer="http://synthetic.supabase.invalid/auth/v1",
                supabase_jwks_url=(
                    "http://synthetic.supabase.invalid/auth/v1/"
                    ".well-known/jwks.json"
                ),
            )
        self.assertEqual(
            insecure.exception.code,
            "governed_memory_supabase_configuration_invalid",
        )

    def test_on_refuses_without_live_session_freshness_verifier(self) -> None:
        actor_calls: list[str] = []
        pool_calls: list[str] = []

        def forbidden_actor_factory(*_args: object, **_kwargs: object):
            actor_calls.append("called")
            raise AssertionError

        async def forbidden_pool_factory(**_kwargs: object):
            pool_calls.append("called")
            raise AssertionError

        with self.assertRaises(HttpServiceConfigurationError) as caught:
            create_governed_memory_http_service(
                active_settings(),
                actor_resolver_factory=forbidden_actor_factory,
                pool_factory=forbidden_pool_factory,
            )
        self.assertEqual(
            caught.exception.code,
            "governed_memory_live_session_freshness_verifier_required",
        )
        self.assertEqual(actor_calls, [])
        self.assertEqual(pool_calls, [])


class ServiceOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_has_closed_surface_and_zero_dependency_io(self) -> None:
        actor_calls: list[str] = []
        pool_calls: list[str] = []

        def forbidden_actor_factory(*_args: object, **_kwargs: object):
            actor_calls.append("called")
            raise AssertionError

        async def forbidden_pool_factory(**_kwargs: object):
            pool_calls.append("called")
            raise AssertionError

        service = create_governed_memory_http_service(
            GovernedMemoryHttpServiceSettings(),
            actor_resolver_factory=forbidden_actor_factory,
            pool_factory=forbidden_pool_factory,
        )
        self.assertIsNone(service.openapi_url)
        self.assertIsNone(service.docs_url)
        self.assertIsNone(service.redoc_url)
        self.assertFalse(service.router.redirect_slashes)

        owner_routes = {
            (method, route.path)
            for route in service.routes
            if route.path.startswith("/memory/")
            for method in (route.methods or set())
        }
        expected_routes = {
            (specification.method.value, specification.path)
            for specification in OWNER_ROUTE_SPECIFICATIONS
        }
        self.assertEqual(owner_routes, expected_routes)

        async with service.router.lifespan_context(service):
            health = await asgi_request(service, "GET", "/healthz")
            self.assertEqual(health[0], 200)
            self.assertEqual(
                health[2],
                {"status": "ok", "service": "governed_memory_http"},
            )
            ready = await asgi_request(service, "GET", "/readyz")
            self.assertEqual(ready[0], 503)
            self.assertEqual(
                ready[2],
                {"error": {"code": "governed_memory_disabled"}},
            )
            for specification in OWNER_ROUTE_SPECIFICATIONS:
                with self.subTest(
                    method=specification.method.value,
                    path=specification.path,
                ):
                    response = await asgi_request(
                        service,
                        specification.method.value,
                        concrete_path(specification.path),
                    )
                    self.assertEqual(response[0], 503)
                    self.assertEqual(
                        response[2],
                        {"error": {"code": "memory_successor_disabled"}},
                    )
                    self.assertEqual(response[1]["cache-control"], "no-store")
                    self.assertEqual(
                        response[1]["x-content-type-options"],
                        "nosniff",
                    )
            trailing = await asgi_request(service, "GET", "/memory/status/")
            self.assertEqual(
                trailing[:1] + trailing[2:],
                (404, {"error": {"code": "memory_route_not_found"}}),
            )
            self.assertNotIn("location", trailing[1])
            wrong_method = await asgi_request(service, "POST", "/memory/status")
            self.assertEqual(
                wrong_method[:1] + wrong_method[2:],
                (405, {"error": {"code": "memory_method_not_allowed"}}),
            )

        self.assertEqual(actor_calls, [])
        self.assertEqual(pool_calls, [])

    async def test_public_exception_codes_match_owner_contract(self) -> None:
        service = create_governed_memory_http_service(
            GovernedMemoryHttpServiceSettings()
        )

        @service.get("/synthetic-validation")
        async def synthetic_validation(required_integer: int) -> dict[str, int]:
            return {"required_integer": required_integer}

        @service.get("/synthetic-http-error")
        async def synthetic_http_error() -> None:
            raise StarletteHttpException(
                status_code=418,
                detail="sensitive synthetic detail",
            )

        invalid = await asgi_request(
            service,
            "GET",
            "/synthetic-validation?required_integer=not-an-integer",
        )
        self.assertEqual(invalid[0], 400)
        self.assertEqual(
            invalid[2],
            {"error": {"code": "memory_request_invalid"}},
        )
        self.assertEqual(invalid[1]["cache-control"], "no-store")

        internal = await asgi_request(
            service,
            "GET",
            "/synthetic-http-error",
        )
        self.assertEqual(internal[0], 500)
        self.assertEqual(
            internal[2],
            {"error": {"code": "memory_internal_error"}},
        )
        self.assertEqual(internal[1]["cache-control"], "no-store")
        self.assertNotIn("sensitive", json.dumps(internal[2]))

    def test_service_module_has_no_provider_qdrant_or_legacy_import(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[2]
            / "rag_engine"
            / "governed_memory"
            / "http_service.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertTrue({"asyncpg", "fastapi"}.issubset(imported_roots))
        self.assertTrue(
            {"openai", "qdrant_client", "app"}.isdisjoint(imported_roots)
        )


class ServiceOnTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_preflights_single_pool_serves_and_closes_once(self) -> None:
        connection = FakeConnection()
        pool = FakePool(connection)
        pool_factory = RecordingPoolFactory(pool)
        actor_factory = RecordingActorResolverFactory()
        freshness = RecordingFreshnessVerifier()
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=pool_factory,
            actor_resolver_factory=actor_factory,
            freshness_verifier=freshness,
            token_clock=lambda: NOW,
        )
        self.assertEqual(len(actor_factory.calls), 1)
        auth_config = actor_factory.calls[0][0]
        self.assertEqual(auth_config.issuer, ISSUER)
        self.assertEqual(auth_config.jwks_url, JWKS_URL)
        self.assertEqual(auth_config.service_token_header, SERVICE_TOKEN_HEADER)
        self.assertEqual(pool_factory.calls, [])
        before = await asgi_request(service, "GET", "/readyz")
        self.assertEqual(before[0], 503)

        async with service.router.lifespan_context(service):
            self.assertEqual(len(pool_factory.calls), 1)
            pool_arguments = pool_factory.calls[0]
            self.assertEqual(pool_arguments["dsn"], POSTGRES_DSN)
            self.assertEqual(pool_arguments["min_size"], 1)
            self.assertEqual(pool_arguments["max_size"], 1)
            self.assertEqual(pool_arguments["command_timeout"], 8.0)
            self.assertTrue(callable(pool_arguments["reset"]))
            self.assertTrue(callable(pool_arguments["init"]))
            self.assertEqual(
                pool_arguments["server_settings"],
                {
                    "application_name": "governed_memory_http",
                    "statement_timeout": "8000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "8000",
                },
            )
            self.assertEqual(len(connection.type_codec_calls), 1)
            codec = connection.type_codec_calls[0]
            self.assertEqual(codec["type_name"], "jsonb")
            self.assertEqual(codec["schema"], "pg_catalog")
            self.assertIs(codec["encoder"], json.dumps)
            self.assertIs(codec["decoder"], json.loads)
            self.assertTrue(
                any(
                    "current_database()" in query
                    for query, _ in connection.fetchrow_calls
                )
            )
            self.assertTrue(
                any("relrowsecurity" in query for query, _ in connection.fetchrow_calls)
            )

            ready = await asgi_request(service, "GET", "/readyz")
            self.assertEqual(ready[0], 200)
            self.assertEqual(
                ready[2],
                {"status": "ready", "service": "governed_memory_http"},
            )
            status = await asgi_request(service, "GET", "/memory/status")
            expected_status = status_result()
            expected_status["last_transition_at"] = expected_status[
                "last_transition_at"
            ].isoformat()
            self.assertEqual(status[0], 200)
            self.assertEqual(status[2], expected_status)
            self.assertEqual(freshness.calls, [("/memory/status", OWNER)])
            self.assertEqual(
                actor_factory.resolve_calls,
                [("/memory/status", (ActorScope.READ_CLAIMS,))],
            )
            self.assertTrue(
                any(
                    "memory_private.read_status()" in query
                    for query, _ in connection.fetchrow_calls
                )
            )
            self.assertEqual(pool.close_calls, 0)

        self.assertEqual(pool.close_calls, 1)
        after = await asgi_request(service, "GET", "/readyz")
        self.assertEqual(after[0], 503)

    async def test_standalone_proposal_jsonb_is_decoded_once(self) -> None:
        connection = FakeConnection()
        pool = FakePool(connection)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=RecordingPoolFactory(pool),
            actor_resolver_factory=RecordingActorResolverFactory(),
            freshness_verifier=RecordingFreshnessVerifier(),
            token_clock=lambda: NOW,
        )

        async with service.router.lifespan_context(service):
            status, _, body = await asgi_request(
                service,
                "GET",
                "/memory/proposals",
            )

        self.assertEqual(status, 200)
        expected = proposal_result()
        expected["object_literal"] = "amber"
        for field in ("created_at", "expires_at"):
            expected[field] = expected[field].isoformat()
        self.assertEqual(body, [expected])
        self.assertIsInstance(body[0]["object_literal"], str)
        self.assertEqual(pool.close_calls, 1)

    async def test_preflight_failure_closes_pool_without_becoming_ready(self) -> None:
        connection = FakeConnection(database_name="wrong_database")
        pool = FakePool(connection)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=RecordingPoolFactory(pool),
            actor_resolver_factory=RecordingActorResolverFactory(),
            freshness_verifier=RecordingFreshnessVerifier(),
            token_clock=lambda: NOW,
        )
        with self.assertRaises(HttpServicePreflightError):
            async with service.router.lifespan_context(service):
                self.fail("preflight_failure_must_not_enter_lifespan")
        self.assertEqual(pool.close_calls, 1)
        ready = await asgi_request(service, "GET", "/readyz")
        self.assertEqual(ready[0], 503)


if __name__ == "__main__":
    unittest.main()
