from __future__ import annotations

import ast
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping
import unittest
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
import jwt
from jwt.algorithms import RSAAlgorithm
from starlette.exceptions import HTTPException as StarletteHttpException

from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.http_service import (
    CONVERSATION_BRIDGE_CATALOG_SHA256_ENV,
    CONVERSATION_POSTGRES_DSN_ENV,
    EXPECTED_CONVERSATION_DATABASE_NAME,
    EXPECTED_CONVERSATION_DATABASE_ROLE,
    EXPECTED_CONVERSATION_REQUESTER_ROLE,
    EXPECTED_DATABASE_NAME,
    EXPECTED_DATABASE_ROLE,
    GovernedMemoryHttpServiceSettings,
    HttpServiceConfigurationError,
    HttpServicePreflightError,
    SERVICE_TOKEN_HEADER,
    _CONVERSATION_BRIDGE_CATALOG_SQL,
    _CONVERSATION_DML_PREFLIGHT_SQL,
    _CONVERSATION_FUNCTION_PREFLIGHT_SQL,
    _CONVERSATION_LOGGING_PREFLIGHT_SQL,
    _CONVERSATION_ROLE_PREFLIGHT_SQL,
    _CONVERSATION_SCHEMA_PREFLIGHT_SQL,
    _RLS_PREFLIGHT_SQL,
    _ROLE_PREFLIGHT_SQL,
    _conversation_bridge_catalog_sha256,
    _preflight_connection,
    _preflight_conversation_connection,
    create_governed_memory_http_service,
)
from rag_engine.governed_memory.deletion_contracts import (
    CONVERSATIONAL_ERASURE_DOMAIN,
    DELETION_REQUEST_CONTRACT_VERSION,
    erasure_target_manifest_sha256,
)
from tests.memory.test_http_api import (
    proposal_result,
    status_result,
)


ISSUER = "https://synthetic.supabase.invalid/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
POSTGRES_DSN = "postgresql://governed_memory_api:secret@db.invalid/governed_memory"
CONVERSATION_POSTGRES_DSN = (
    "postgresql://governed_memory_api:other-secret@db.invalid/memory"
)
SERVICE_TOKEN = "dedicated-synthetic-service-token"
OWNER = UUID("11111111-1111-4111-8111-111111111111")
RESOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
HASH = "a" * 64
OPERATION = UUID("33333333-3333-4333-8333-333333333333")
CONVERSATION_CATALOG_ROWS = [
    {
        "catalog_kind": "database",
        "catalog_identity": "memory",
        "catalog_payload": '{"encoding":"UTF8","owner":"sage"}',
    },
    {
        "catalog_kind": "routine",
        "catalog_identity": (
            "memory_ingest_private.begin_source_erasure("
            "uuid,text,uuid,uuid,integer,text)"
        ),
        "catalog_payload": '{"security_definer":true}',
    },
]
BRIDGE_CATALOG_SHA256 = _conversation_bridge_catalog_sha256(
    CONVERSATION_CATALOG_ROWS
)


class StaticJwksFetcher:
    def __init__(self, body: bytes, events: list[str] | None = None) -> None:
        self.body = body
        self.events = events
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, url: str, timeout: int, maximum_bytes: int) -> bytes:
        self.calls.append((url, timeout, maximum_bytes))
        if self.events is not None:
            self.events.append("resolver:jwks")
        return self.body


def active_settings(**overrides: object) -> GovernedMemoryHttpServiceSettings:
    values: dict[str, object] = {
        "mode": "on",
        "postgres_dsn": POSTGRES_DSN,
        "conversation_postgres_dsn": CONVERSATION_POSTGRES_DSN,
        "conversation_bridge_catalog_sha256": BRIDGE_CATALOG_SHA256,
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
    json_body: Mapping[str, object] | None = None,
) -> tuple[int, dict[str, str], Any]:
    parsed = urlsplit(target)
    raw_body = (
        b""
        if json_body is None
        else json.dumps(json_body, separators=(",", ":")).encode("utf-8")
    )
    request_headers = {"host": "testserver", **dict(headers or {})}
    if json_body is not None:
        request_headers.update(
            {
                "content-type": "application/json",
                "content-length": str(len(raw_body)),
            }
        )
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
        return {
            "type": "http.request",
            "body": raw_body,
            "more_body": False,
        }

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
    def __init__(
        self,
        *,
        database_name: str = EXPECTED_DATABASE_NAME,
        events: list[str] | None = None,
        event_label: str = "successor",
    ) -> None:
        self.database_name = database_name
        self.events = events
        self.event_label = event_label
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
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
        self.fetch_calls.append((query, arguments))
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
        if self.events is not None:
            self.events.append(f"{self.event_label}:transaction")
        return _AsyncContext(self)

    async def reset(self) -> None:
        self.reset_calls += 1


class FakeConversationConnection(FakeConnection):
    def __init__(
        self,
        *,
        overrides: Mapping[str, object] | None = None,
        catalog_rows: list[Mapping[str, object]] | None = None,
        request_role_context: Mapping[str, object] | None = None,
        events: list[str] | None = None,
    ) -> None:
        super().__init__(
            database_name=EXPECTED_CONVERSATION_DATABASE_NAME,
            events=events,
            event_label="conversation",
        )
        self.overrides = dict(overrides or {})
        self.request_role_context = {
            "session_user": EXPECTED_CONVERSATION_DATABASE_ROLE,
            "current_user": EXPECTED_CONVERSATION_REQUESTER_ROLE,
            "requester_member": True,
            **dict(request_role_context or {}),
        }
        self.catalog_rows = list(catalog_rows or CONVERSATION_CATALOG_ROWS)
        self.local_owner: str | None = None
        self.local_auth_context: str | None = None
        self.local_role: str | None = None
        self.deletion_queries = 0
        self.target_manifest_sha256 = erasure_target_manifest_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            targets=(),
        )

    def _row(self, values: dict[str, object]) -> dict[str, object]:
        values.update(
            {
                key: value
                for key, value in self.overrides.items()
                if key in values
            }
        )
        return values

    async def fetchrow(
        self,
        query: str,
        *arguments: object,
    ) -> Mapping[str, object]:
        self.fetchrow_calls.append((query, arguments))
        if query == _CONVERSATION_ROLE_PREFLIGHT_SQL:
            return self._row(
                {
                    "database_name": EXPECTED_CONVERSATION_DATABASE_NAME,
                    "session_user": EXPECTED_CONVERSATION_DATABASE_ROLE,
                    "current_user": EXPECTED_CONVERSATION_DATABASE_ROLE,
                    "api_can_login": True,
                    "api_inherits": False,
                    "api_is_superuser": False,
                    "api_can_create_database": False,
                    "api_can_create_role": False,
                    "api_can_replicate": False,
                    "api_bypasses_rls": False,
                    "requester_can_login": False,
                    "requester_inherits": False,
                    "requester_is_superuser": False,
                    "requester_can_create_database": False,
                    "requester_can_create_role": False,
                    "requester_can_replicate": False,
                    "requester_bypasses_rls": False,
                    "api_direct_membership_count": 1,
                    "requester_direct_member_count": 1,
                    "api_effective_membership_count": 1,
                    "requester_effective_membership_count": 0,
                    "requester_member": True,
                    "requester_admin_option": False,
                    "requester_inherit_option": False,
                    "requester_set_option": True,
                    "brains_app_requester_member": False,
                }
            )
        if query == _CONVERSATION_LOGGING_PREFLIGHT_SQL:
            return self._row(
                {
                    "log_statement_disabled": True,
                    "error_parameter_logging_disabled": True,
                    "duration_logging_disabled": True,
                    "duration_statement_logging_disabled": True,
                    "duration_sample_logging_disabled": True,
                    "transaction_sampling_disabled": True,
                    "ordinary_parameter_logging_disabled": True,
                    "pgaudit_not_preloaded": True,
                    "auto_explain_parameter_logging_disabled": True,
                }
            )
        if query == _CONVERSATION_SCHEMA_PREFLIGHT_SQL:
            return self._row(
                {
                    "schema_owner_exact": True,
                    "requester_has_usage": True,
                    "requester_has_create": False,
                    "schema_acl_entry_count": 5,
                    "schema_owner_grantable_entry_count": 2,
                    "schema_runtime_grantable_entry_count": 0,
                    "schema_acl_exact": True,
                }
            )
        if query == _CONVERSATION_FUNCTION_PREFLIGHT_SQL:
            return self._row(
                {
                    "expected_function_count": 2,
                    "expected_function_identity_exact": True,
                    "requester_execute_count": 2,
                    "requester_expected_execute_count": 2,
                    "public_or_api_execute_count": 0,
                    "expected_function_acl_entry_count": 4,
                    "expected_function_owner_grantable_entry_count": 2,
                    "expected_function_requester_grantable_entry_count": 0,
                    "expected_function_acl_exact": True,
                    "unexpected_public_api_or_requester_security_definer_count": 0,
                }
            )
        if query == _CONVERSATION_DML_PREFLIGHT_SQL:
            return self._row(
                {
                    "application_relation_count": 42,
                    "application_sequence_count": 5,
                    "public_table_count": 3,
                    "chat_root_identity_exact": True,
                    "private_table_count": 7,
                    "private_table_identity_exact": True,
                    "api_or_requester_has_relation_privilege": False,
                    "public_api_or_requester_direct_relation_grant": False,
                    "api_or_requester_has_column_privilege": False,
                    "public_api_or_requester_direct_column_grant": False,
                    "api_or_requester_has_sequence_privilege": False,
                    "public_api_or_requester_direct_sequence_grant": False,
                }
            )
        if "session_user::text AS session_user" in query:
            if self.events is not None:
                self.events.append("conversation:verify_role_context")
            return dict(self.request_role_context)
        if "begin_source_erasure" in query:
            if self.events is not None:
                self.events.append("conversation:begin_source_erasure")
            self.deletion_queries += 1
            return {
                "outcome": "fenced",
                "operation_id": OPERATION,
                "state": "fenced",
                "target_count": 0,
                "selector_sha256": "7" * 64,
                "target_manifest_sha256": self.target_manifest_sha256,
            }
        if "read_source_erasure" in query:
            if self.events is not None:
                self.events.append("conversation:read_source_erasure")
            self.deletion_queries += 1
            return {
                "operation_id": OPERATION,
                "selector_kind": "all_conversations",
                "state": "fenced",
                "target_count": 0,
                "selector_sha256": "7" * 64,
                "target_manifest_sha256": self.target_manifest_sha256,
                "governed_receipt_sha256": None,
                "last_error_code": None,
                "created_at": NOW,
                "completed_at": None,
            }
        raise AssertionError("unexpected_conversation_fetchrow")

    async def fetch(
        self,
        query: str,
        *arguments: object,
    ) -> list[Mapping[str, object]]:
        self.fetch_calls.append((query, arguments))
        if query == _CONVERSATION_BRIDGE_CATALOG_SQL:
            return list(self.catalog_rows)
        return await super().fetch(query, *arguments)

    async def execute(self, query: str, *arguments: object) -> str:
        self.execute_calls.append((query, arguments))
        if query == "SET LOCAL ROLE memory_erasure_requester":
            self.local_role = EXPECTED_CONVERSATION_REQUESTER_ROLE
            if self.events is not None:
                self.events.append("conversation:set_role")
        elif "'app.user_id'" in query:
            self.local_owner = str(arguments[0])
            if self.events is not None:
                self.events.append("conversation:set_owner")
        elif "'app.auth_context_sha256'" in query:
            self.local_auth_context = str(arguments[0])
            if self.events is not None:
                self.events.append("conversation:set_auth_context")
        return "SELECT 1"

    async def fetchval(self, query: str) -> object:
        self.fetchval_calls.append(query)
        if "'app.user_id'" in query:
            if self.events is not None:
                self.events.append("conversation:verify_owner")
            return self.local_owner
        if "'app.auth_context_sha256'" in query:
            if self.events is not None:
                self.events.append("conversation:verify_auth_context")
            return self.local_auth_context
        raise AssertionError("unexpected_conversation_fetchval")


class FakePool:
    def __init__(
        self,
        connection: FakeConnection,
        *,
        events: list[str] | None = None,
        event_label: str = "pool",
    ) -> None:
        self.connection = connection
        self.events = events
        self.event_label = event_label
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self) -> _AsyncContext:
        self.acquire_calls += 1
        if self.events is not None:
            self.events.append(f"{self.event_label}:acquire")
        return _AsyncContext(self.connection)

    async def close(self) -> None:
        self.close_calls += 1


class RecordingPoolFactory:
    def __init__(self, *pools: FakePool) -> None:
        self.pools = pools
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> FakePool:
        self.calls.append(dict(kwargs))
        pool = self.pools[len(self.calls) - 1]
        initializer = kwargs.get("init")
        if callable(initializer):
            await initializer(pool.connection)
        return pool


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
                session_id=OWNER,
                role=ActorRole.OWNER,
                scopes=scopes,
                authentication_manifest_sha256=HASH,
                authenticated_at=NOW,
            )

        return resolve


class RecordingAuthorityVerifier:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[str, UUID]] = []
        self.events = events

    async def __call__(self, request: Request, actor: VerifiedActor) -> None:
        self.calls.append((request.url.path, actor.owner_user_id))
        if self.events is not None:
            self.events.append("authority:verified")


class ServiceSettingsTests(unittest.TestCase):
    def test_off_is_exact_default_and_does_not_load_active_environment(self) -> None:
        settings = GovernedMemoryHttpServiceSettings.from_environment(
            {
                "GOVERNED_MEMORY_POSTGRES_DSN": "invalid-but-unused",
                CONVERSATION_POSTGRES_DSN_ENV: "invalid-but-unused",
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
        self.assertNotIn(CONVERSATION_POSTGRES_DSN, rendered)
        self.assertNotIn(SERVICE_TOKEN, rendered)
        config = settings.authentication_config()
        self.assertEqual(config.audience, "authenticated")
        self.assertEqual(config.service_token_header, SERVICE_TOKEN_HEADER)

        for field_name in (
            "postgres_dsn",
            "conversation_postgres_dsn",
            "conversation_bridge_catalog_sha256",
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
        for invalid_hash in ("", "A" * 64, "a" * 63, "g" * 64):
            with self.subTest(invalid_hash=invalid_hash), self.assertRaises(
                HttpServiceConfigurationError
            ):
                active_settings(
                    conversation_bridge_catalog_sha256=invalid_hash
                )

    def test_on_environment_requires_full_bridge_catalog_hash(self) -> None:
        environment = {
            "GOVERNED_MEMORY_HTTP_MODE": "on",
            "GOVERNED_MEMORY_POSTGRES_DSN": POSTGRES_DSN,
            CONVERSATION_POSTGRES_DSN_ENV: CONVERSATION_POSTGRES_DSN,
            CONVERSATION_BRIDGE_CATALOG_SHA256_ENV: BRIDGE_CATALOG_SHA256,
            "GOVERNED_MEMORY_SUPABASE_ISSUER": ISSUER,
            "GOVERNED_MEMORY_SUPABASE_JWKS_URL": JWKS_URL,
            "GOVERNED_MEMORY_SERVICE_TOKEN": SERVICE_TOKEN,
        }
        self.assertEqual(
            GovernedMemoryHttpServiceSettings.from_environment(environment),
            active_settings(),
        )

    def test_on_refuses_without_live_authority_verifier(self) -> None:
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
            "governed_memory_live_authority_verifier_required",
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
    @classmethod
    def setUpClass(cls) -> None:
        cls.rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        jwk = json.loads(RSAAlgorithm.to_jwk(cls.rsa_private.public_key()))
        jwk.update(
            {
                "alg": "RS256",
                "kid": "service-integration-rsa",
                "key_ops": ["verify"],
                "use": "sig",
            }
        )
        cls.jwks = json.dumps(
            {"keys": [jwk]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _token(self) -> str:
        now_seconds = int(NOW.timestamp())
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": "authenticated",
                "sub": str(OWNER),
                "session_id": str(OWNER),
                "role": "authenticated",
                "is_anonymous": False,
                "iat": now_seconds - 60,
                "exp": now_seconds + 300,
            },
            self.rsa_private,
            algorithm="RS256",
            headers={
                "alg": "RS256",
                "kid": "service-integration-rsa",
                "typ": "JWT",
            },
        )

    async def test_preflight_query_sequences_are_disjoint_and_exact(self) -> None:
        successor = FakeConnection()
        conversation = FakeConversationConnection()
        await _preflight_connection(successor)
        await _preflight_conversation_connection(
            conversation,
            BRIDGE_CATALOG_SHA256,
        )
        self.assertEqual(
            successor.fetchrow_calls,
            [
                (_ROLE_PREFLIGHT_SQL, ()),
                (_RLS_PREFLIGHT_SQL, ([
                    "answer_binding",
                    "audit_event",
                    "claim",
                    "claim_deletion_receipt",
                    "claim_evidence",
                    "claim_revision",
                    "entity",
                    "evidence",
                    "extraction_job",
                    "projection_outbox",
                    "proposal",
                    "provider_call",
                ],)),
            ],
        )
        self.assertEqual(
            conversation.fetchrow_calls,
            [
                (_CONVERSATION_ROLE_PREFLIGHT_SQL, ()),
                (_CONVERSATION_LOGGING_PREFLIGHT_SQL, ()),
                (_CONVERSATION_SCHEMA_PREFLIGHT_SQL, ()),
                (_CONVERSATION_FUNCTION_PREFLIGHT_SQL, ()),
                (_CONVERSATION_DML_PREFLIGHT_SQL, ()),
            ],
        )
        self.assertEqual(
            conversation.fetch_calls,
            [(_CONVERSATION_BRIDGE_CATALOG_SQL, ())],
        )
        self.assertIn(
            "'memory_erasure_requester', routine.oid, 'EXECUTE'",
            _CONVERSATION_FUNCTION_PREFLIGHT_SQL,
        )
        self.assertIn("pg_catalog.pg_attribute", _CONVERSATION_DML_PREFLIGHT_SQL)
        self.assertIn(
            "pg_catalog.has_column_privilege",
            _CONVERSATION_DML_PREFLIGHT_SQL,
        )
        self.assertIn("target_policies AS (", _CONVERSATION_BRIDGE_CATALOG_SQL)
        self.assertIn(
            "policy.polrelid IN (SELECT oid FROM target_relations)",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "target_trigger_routines AS (",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "target_policy_routines AS (",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "authority_routine_oids AS (",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "dependency.classid = 'pg_catalog.pg_policy'::pg_catalog.regclass",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "dependency.refclassid = 'pg_catalog.pg_proc'::pg_catalog.regclass",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "WHERE namespace.nspname <> 'pg_catalog'",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "SELECT routine_oid FROM target_trigger_routines",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "SELECT routine_oid FROM target_policy_routines",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "routine.oid IN (SELECT routine_oid FROM authority_routine_oids)",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        policy_facts = _CONVERSATION_BRIDGE_CATALOG_SQL.split(
            "), policy_facts AS (", 1
        )[1].split("), trigger_facts AS (", 1)[0]
        self.assertIn("FROM target_policies AS policy", policy_facts)
        self.assertNotIn("nspname = 'memory_ingest_private'", policy_facts)
        self.assertIn(
            "index_value.indrelid IN (SELECT oid FROM target_relations)",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )
        self.assertIn(
            "namespace.nspname IN (SELECT nspname FROM target_relations)",
            _CONVERSATION_BRIDGE_CATALOG_SQL,
        )

    async def test_on_preflights_two_pools_serves_and_closes_once(self) -> None:
        connection = FakeConnection()
        conversation_connection = FakeConversationConnection()
        pool = FakePool(connection)
        conversation_pool = FakePool(conversation_connection)
        pool_factory = RecordingPoolFactory(pool, conversation_pool)
        actor_factory = RecordingActorResolverFactory()
        authority = RecordingAuthorityVerifier()
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=pool_factory,
            actor_resolver_factory=actor_factory,
            authority_verifier=authority,
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
            self.assertEqual(len(pool_factory.calls), 2)
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
            conversation_arguments = pool_factory.calls[1]
            self.assertEqual(
                conversation_arguments["dsn"],
                CONVERSATION_POSTGRES_DSN,
            )
            self.assertEqual(
                conversation_arguments["server_settings"],
                {
                    "application_name": "governed_memory_conversation_http",
                    "statement_timeout": "8000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "8000",
                },
            )
            self.assertEqual(len(connection.type_codec_calls), 1)
            self.assertEqual(len(conversation_connection.type_codec_calls), 1)
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
            self.assertEqual(authority.calls, [("/memory/status", OWNER)])
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
            self.assertEqual(conversation_pool.close_calls, 0)

        self.assertEqual(pool.close_calls, 1)
        self.assertEqual(conversation_pool.close_calls, 1)
        after = await asgi_request(service, "GET", "/readyz")
        self.assertEqual(after[0], 503)

    async def test_service_post_uses_real_resolver_synthetic_jwt_and_conversation_pool_only(
        self,
    ) -> None:
        events: list[str] = []
        successor_connection = FakeConnection(events=events)
        conversation_connection = FakeConversationConnection(events=events)
        successor_pool = FakePool(
            successor_connection,
            events=events,
            event_label="successor",
        )
        conversation_pool = FakePool(
            conversation_connection,
            events=events,
            event_label="conversation",
        )
        fetcher = StaticJwksFetcher(self.jwks, events)
        authority = RecordingAuthorityVerifier(events)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=RecordingPoolFactory(
                successor_pool,
                conversation_pool,
            ),
            authority_verifier=authority,
            token_clock=lambda: NOW,
            jwks_fetcher=fetcher,
        )
        body = {
            "confirmation_sha256": "c" * 64,
            "contract_version": DELETION_REQUEST_CONTRACT_VERSION,
            "data_domain": CONVERSATIONAL_ERASURE_DOMAIN,
            "operation_id": str(OPERATION),
            "selector_kind": "all_conversations",
        }
        headers = {
            "authorization": f"Bearer {self._token()}",
            SERVICE_TOKEN_HEADER: SERVICE_TOKEN,
        }

        async with service.router.lifespan_context(service):
            events.clear()
            status, _, response = await asgi_request(
                service,
                "POST",
                "/memory/conversations/erasure-requests",
                headers=headers,
                json_body=body,
            )

        self.assertEqual(status, 202)
        self.assertEqual(response["operation_id"], str(OPERATION))
        self.assertEqual(response["state"], "fenced")
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(
            authority.calls,
            [("/memory/conversations/erasure-requests", OWNER)],
        )
        self.assertEqual(successor_pool.acquire_calls, 1)
        self.assertEqual(conversation_pool.acquire_calls, 2)
        self.assertEqual(conversation_connection.deletion_queries, 2)
        self.assertEqual(
            events,
            [
                "resolver:jwks",
                "authority:verified",
                "conversation:acquire",
                "conversation:transaction",
                "conversation:set_role",
                "conversation:verify_role_context",
                "conversation:set_owner",
                "conversation:set_auth_context",
                "conversation:verify_owner",
                "conversation:verify_auth_context",
                "conversation:begin_source_erasure",
                "conversation:read_source_erasure",
            ],
        )
        executed_sql = [
            query for query, _arguments in conversation_connection.execute_calls
        ]
        self.assertIn(
            "SET LOCAL ROLE memory_erasure_requester",
            executed_sql,
        )
        self.assertTrue(
            any("'app.user_id'" in query for query in executed_sql)
        )
        self.assertTrue(
            any("'app.auth_context_sha256'" in query for query in executed_sql)
        )
        conversation_fetch_sql = [
            query for query, _arguments in conversation_connection.fetchrow_calls
        ]
        self.assertTrue(
            any("begin_source_erasure" in query for query in conversation_fetch_sql)
        )
        self.assertTrue(
            any("read_source_erasure" in query for query in conversation_fetch_sql)
        )
        successor_sql = [
            query
            for query, _arguments in (
                successor_connection.fetchrow_calls
                + successor_connection.execute_calls
            )
        ]
        self.assertFalse(
            any("source_erasure" in query for query in successor_sql)
        )

    async def test_service_post_role_context_drift_runs_zero_deletion_sql(
        self,
    ) -> None:
        body = {
            "confirmation_sha256": "c" * 64,
            "contract_version": DELETION_REQUEST_CONTRACT_VERSION,
            "data_domain": CONVERSATIONAL_ERASURE_DOMAIN,
            "operation_id": str(OPERATION),
            "selector_kind": "all_conversations",
        }
        headers = {
            "authorization": f"Bearer {self._token()}",
            SERVICE_TOKEN_HEADER: SERVICE_TOKEN,
        }
        drifts = (
            {"session_user": "brains_app"},
            {"current_user": EXPECTED_CONVERSATION_DATABASE_ROLE},
            {"requester_member": False},
        )
        for drift in drifts:
            with self.subTest(drift=drift):
                events: list[str] = []
                successor_connection = FakeConnection(events=events)
                conversation_connection = FakeConversationConnection(
                    request_role_context=drift,
                    events=events,
                )
                successor_pool = FakePool(
                    successor_connection,
                    events=events,
                    event_label="successor",
                )
                conversation_pool = FakePool(
                    conversation_connection,
                    events=events,
                    event_label="conversation",
                )
                service = create_governed_memory_http_service(
                    active_settings(),
                    pool_factory=RecordingPoolFactory(
                        successor_pool,
                        conversation_pool,
                    ),
                    authority_verifier=RecordingAuthorityVerifier(events),
                    token_clock=lambda: NOW,
                    jwks_fetcher=StaticJwksFetcher(self.jwks, events),
                )
                async with service.router.lifespan_context(service):
                    events.clear()
                    successor_connection.fetchrow_calls.clear()
                    successor_connection.execute_calls.clear()
                    conversation_connection.fetchrow_calls.clear()
                    conversation_connection.execute_calls.clear()
                    status, _, response = await asgi_request(
                        service,
                        "POST",
                        "/memory/conversations/erasure-requests",
                        headers=headers,
                        json_body=body,
                    )

                self.assertEqual(status, 503)
                self.assertEqual(
                    response,
                    {"error": {"code": "conversation_unavailable"}},
                )
                self.assertEqual(conversation_connection.deletion_queries, 0)
                self.assertEqual(
                    events,
                    [
                        "resolver:jwks",
                        "authority:verified",
                        "conversation:acquire",
                        "conversation:transaction",
                        "conversation:set_role",
                        "conversation:verify_role_context",
                    ],
                )
                self.assertFalse(
                    any(
                        "begin_source_erasure" in query
                        or "read_source_erasure" in query
                        for query, _arguments in (
                            conversation_connection.fetchrow_calls
                        )
                    )
                )
                self.assertFalse(
                    any(
                        "source_erasure" in query
                        for query, _arguments in (
                            successor_connection.fetchrow_calls
                            + successor_connection.execute_calls
                        )
                    )
                )

    async def test_source_preflight_rejects_every_authority_surface_drift(
        self,
    ) -> None:
        self.assertIn(
            "pg_catalog.string_to_array(",
            _CONVERSATION_LOGGING_PREFLIGHT_SQL,
        )
        self.assertNotIn(
            r"\\s*pgaudit",
            _CONVERSATION_LOGGING_PREFLIGHT_SQL,
        )
        drifts = (
            ("api_can_create_database", True),
            ("requester_can_login", True),
            ("requester_direct_member_count", 2),
            ("api_effective_membership_count", 2),
            ("requester_effective_membership_count", 1),
            ("requester_admin_option", True),
            ("requester_inherit_option", True),
            ("requester_set_option", False),
            ("brains_app_requester_member", True),
            ("log_statement_disabled", False),
            ("error_parameter_logging_disabled", False),
            ("duration_logging_disabled", False),
            ("duration_statement_logging_disabled", False),
            ("duration_sample_logging_disabled", False),
            ("transaction_sampling_disabled", False),
            ("ordinary_parameter_logging_disabled", False),
            ("pgaudit_not_preloaded", False),
            ("auto_explain_parameter_logging_disabled", False),
            ("schema_owner_exact", False),
            ("schema_acl_entry_count", 6),
            ("schema_owner_grantable_entry_count", 0),
            ("schema_runtime_grantable_entry_count", 1),
            ("schema_acl_exact", False),
            ("expected_function_identity_exact", False),
            ("requester_execute_count", 3),
            ("expected_function_acl_entry_count", 5),
            ("expected_function_owner_grantable_entry_count", 0),
            ("expected_function_requester_grantable_entry_count", 1),
            ("expected_function_acl_exact", False),
            (
                "unexpected_public_api_or_requester_security_definer_count",
                1,
            ),
            ("chat_root_identity_exact", False),
            ("private_table_identity_exact", False),
            ("api_or_requester_has_relation_privilege", True),
            ("public_api_or_requester_direct_relation_grant", True),
            ("api_or_requester_has_column_privilege", True),
            ("public_api_or_requester_direct_column_grant", True),
            ("api_or_requester_has_sequence_privilege", True),
            ("public_api_or_requester_direct_sequence_grant", True),
        )
        for field_name, drifted_value in drifts:
            with self.subTest(field_name=field_name):
                with self.assertRaises(HttpServicePreflightError):
                    await _preflight_conversation_connection(
                        FakeConversationConnection(
                            overrides={field_name: drifted_value}
                        ),
                        BRIDGE_CATALOG_SHA256,
                    )

        with self.assertRaises(HttpServicePreflightError):
            await _preflight_conversation_connection(
                FakeConversationConnection(),
                "f" * 64,
            )
        with self.assertRaises(HttpServicePreflightError):
            await _preflight_conversation_connection(
                FakeConversationConnection(
                    catalog_rows=[
                        *CONVERSATION_CATALOG_ROWS,
                        dict(CONVERSATION_CATALOG_ROWS[0]),
                    ]
                ),
                BRIDGE_CATALOG_SHA256,
            )

    async def test_source_preflight_failure_closes_both_pools(self) -> None:
        successor_pool = FakePool(FakeConnection())
        conversation_pool = FakePool(
            FakeConversationConnection(
                overrides={"requester_set_option": False}
            )
        )
        factory = RecordingPoolFactory(successor_pool, conversation_pool)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=factory,
            actor_resolver_factory=RecordingActorResolverFactory(),
            authority_verifier=RecordingAuthorityVerifier(),
            token_clock=lambda: NOW,
        )
        with self.assertRaises(HttpServicePreflightError):
            async with service.router.lifespan_context(service):
                self.fail("source_preflight_failure_must_not_enter_lifespan")
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(successor_pool.close_calls, 1)
        self.assertEqual(conversation_pool.close_calls, 1)
        self.assertEqual((await asgi_request(service, "GET", "/readyz"))[0], 503)

    async def test_standalone_proposal_jsonb_is_decoded_once(self) -> None:
        connection = FakeConnection()
        conversation_connection = FakeConversationConnection()
        pool = FakePool(connection)
        conversation_pool = FakePool(conversation_connection)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=RecordingPoolFactory(pool, conversation_pool),
            actor_resolver_factory=RecordingActorResolverFactory(),
            authority_verifier=RecordingAuthorityVerifier(),
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
        self.assertEqual(conversation_pool.close_calls, 1)

    async def test_preflight_failure_closes_pool_without_becoming_ready(self) -> None:
        connection = FakeConnection(database_name="wrong_database")
        pool = FakePool(connection)
        service = create_governed_memory_http_service(
            active_settings(),
            pool_factory=RecordingPoolFactory(pool),
            actor_resolver_factory=RecordingActorResolverFactory(),
            authority_verifier=RecordingAuthorityVerifier(),
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
    _CONVERSATION_LOGGING_PREFLIGHT_SQL,
    _conversation_bridge_catalog_sha256,
