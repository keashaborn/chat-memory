from __future__ import annotations

"""Standalone, inactive-by-default HTTP service for governed Memory.

The service deliberately does not import the legacy Brains application.  Off
mode mounts the closed owner route manifest without constructing authentication
or database dependencies.  On mode remains fail-closed until a caller injects
a live Supabase account-and-session authority verifier.
"""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import json
import os
from typing import Any, Protocol

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from .auth import ActorScope, VerifiedActor
from .http_api import ActorResolver, create_owner_memory_router
from .http_auth import HttpAuthError
from .http_runtime import (
    JwksFetcher,
    SupabaseHttpRuntimeConfig,
    create_supabase_actor_resolver,
)
from .http_store import OwnerStoreError, PostgresOwnerStore


HTTP_MODE_ENV = "GOVERNED_MEMORY_HTTP_MODE"
POSTGRES_DSN_ENV = "GOVERNED_MEMORY_POSTGRES_DSN"
SUPABASE_ISSUER_ENV = "GOVERNED_MEMORY_SUPABASE_ISSUER"
SUPABASE_JWKS_URL_ENV = "GOVERNED_MEMORY_SUPABASE_JWKS_URL"
SERVICE_TOKEN_ENV = "GOVERNED_MEMORY_SERVICE_TOKEN"
SERVICE_TOKEN_HEADER = "x-governed-memory-service-token"
EXPECTED_DATABASE_NAME = "governed_memory"
EXPECTED_DATABASE_ROLE = "governed_memory_api"

_OWNER_TABLES = (
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
)
_ROLE_PREFLIGHT_SQL = """
SELECT
  pg_catalog.current_database()::text AS database_name,
  session_user::text AS session_user,
  current_user::text AS current_user,
  role.rolcanlogin AS can_login,
  role.rolinherit AS inherits,
  role.rolsuper AS is_superuser,
  role.rolbypassrls AS bypasses_rls
FROM pg_catalog.pg_roles AS role
WHERE role.rolname = session_user
"""
_RLS_PREFLIGHT_SQL = """
SELECT
  pg_catalog.count(*)::integer AS table_count,
  COALESCE(
    pg_catalog.bool_and(
      relation.relrowsecurity
      AND relation.relforcerowsecurity
      AND pg_catalog.pg_get_userbyid(relation.relowner) = 'governed_memory_owner'
    ),
    false
  ) AS exact_forced_rls,
  COALESCE(
    pg_catalog.bool_or(
      pg_catalog.has_table_privilege(
        session_user,
        relation.oid,
        'SELECT,INSERT,UPDATE,DELETE'
      )
    ),
    true
  ) AS api_has_direct_dml
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'memory'
  AND relation.relkind = 'r'
  AND relation.relname = ANY($1::text[])
"""
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

PoolFactory = Callable[..., Awaitable[Any]]
ActorResolverFactory = Callable[..., ActorResolver]


class LiveAuthorityVerifier(Protocol):
    """Verify the request's account and JWT session against live authority.

    An implementation must make bounded uncached authority requests, require
    the authoritative user UUID to equal ``actor.owner_user_id``, and require
    ``actor.session_id`` to be present in the Supabase session ledger.  The
    verifier must fail closed on ambiguity or authority unavailability.
    """

    async def __call__(self, request: Request, actor: VerifiedActor) -> None: ...


class HttpServiceConfigurationError(RuntimeError):
    """Stable content-free service configuration failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HttpServicePreflightError(RuntimeError):
    """Stable content-free database metadata preflight failure."""

    def __init__(self, code: str = "governed_memory_database_preflight_failed") -> None:
        self.code = code
        super().__init__(code)


def _required_text(value: object, *, secret: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HttpServiceConfigurationError(
            "governed_memory_secret_invalid"
            if secret
            else "governed_memory_configuration_invalid"
        )
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise HttpServiceConfigurationError(
            "governed_memory_configuration_invalid"
        ) from exc
    maximum = 4_096 if secret else 16_384
    if size > maximum:
        raise HttpServiceConfigurationError(
            "governed_memory_secret_invalid"
            if secret
            else "governed_memory_configuration_invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class GovernedMemoryHttpServiceSettings:
    mode: str = "off"
    postgres_dsn: str | None = field(default=None, repr=False)
    supabase_issuer: str | None = None
    supabase_jwks_url: str | None = None
    service_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {"off", "on"}:
            raise HttpServiceConfigurationError("governed_memory_http_mode_invalid")
        active_values = (
            self.postgres_dsn,
            self.supabase_issuer,
            self.supabase_jwks_url,
            self.service_token,
        )
        if self.mode == "off":
            if any(value is not None for value in active_values):
                raise HttpServiceConfigurationError(
                    "governed_memory_off_configuration_must_be_empty"
                )
            return
        _required_text(self.postgres_dsn)
        issuer = _required_text(self.supabase_issuer)
        jwks_url = _required_text(self.supabase_jwks_url)
        service_token = _required_text(self.service_token, secret=True)
        try:
            SupabaseHttpRuntimeConfig(
                issuer=issuer,
                audience="authenticated",
                jwks_url=jwks_url,
                expected_service_token=service_token,
                service_token_header=SERVICE_TOKEN_HEADER,
            )
        except HttpAuthError as exc:
            raise HttpServiceConfigurationError(
                "governed_memory_supabase_configuration_invalid"
            ) from exc

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "GovernedMemoryHttpServiceSettings":
        values = environment if environment is not None else os.environ
        mode = values.get(HTTP_MODE_ENV, "off")
        if mode != "on":
            return cls(mode=mode)
        return cls(
            mode="on",
            postgres_dsn=values.get(POSTGRES_DSN_ENV),
            supabase_issuer=values.get(SUPABASE_ISSUER_ENV),
            supabase_jwks_url=values.get(SUPABASE_JWKS_URL_ENV),
            service_token=values.get(SERVICE_TOKEN_ENV),
        )

    def authentication_config(self) -> SupabaseHttpRuntimeConfig:
        if self.mode != "on":
            raise HttpServiceConfigurationError("governed_memory_http_disabled")
        assert self.supabase_issuer is not None
        assert self.supabase_jwks_url is not None
        assert self.service_token is not None
        return SupabaseHttpRuntimeConfig(
            issuer=self.supabase_issuer,
            audience="authenticated",
            jwks_url=self.supabase_jwks_url,
            expected_service_token=self.service_token,
            service_token_header=SERVICE_TOKEN_HEADER,
        )


class _PoolHandle:
    __slots__ = ("_pool",)

    def __init__(self) -> None:
        self._pool: Any | None = None

    def attach(self, pool: Any) -> None:
        if pool is None or self._pool is not None:
            raise HttpServicePreflightError()
        self._pool = pool

    def detach(self, pool: Any) -> None:
        if self._pool is not pool:
            raise HttpServicePreflightError()
        self._pool = None

    def acquire(self) -> Any:
        if self._pool is None:
            raise OwnerStoreError("database_unavailable")
        return self._pool.acquire()


class _ServiceRuntime:
    __slots__ = ("mode", "pool_handle", "ready")

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.pool_handle = _PoolHandle()
        self.ready = False


def _row_values(row: object, names: tuple[str, ...]) -> dict[str, object]:
    if row is None:
        raise HttpServicePreflightError()
    try:
        return {name: row[name] for name in names}  # type: ignore[index]
    except (KeyError, TypeError, IndexError) as exc:
        raise HttpServicePreflightError() from exc


async def _preflight_connection(connection: Any) -> None:
    identity = _row_values(
        await connection.fetchrow(_ROLE_PREFLIGHT_SQL),
        (
            "database_name",
            "session_user",
            "current_user",
            "can_login",
            "inherits",
            "is_superuser",
            "bypasses_rls",
        ),
    )
    if (
        identity["database_name"] != EXPECTED_DATABASE_NAME
        or identity["session_user"] != EXPECTED_DATABASE_ROLE
        or identity["current_user"] != EXPECTED_DATABASE_ROLE
        or identity["can_login"] is not True
        or identity["inherits"] is not False
        or identity["is_superuser"] is not False
        or identity["bypasses_rls"] is not False
    ):
        raise HttpServicePreflightError()

    rls = _row_values(
        await connection.fetchrow(_RLS_PREFLIGHT_SQL, list(_OWNER_TABLES)),
        ("table_count", "exact_forced_rls", "api_has_direct_dml"),
    )
    if (
        type(rls["table_count"]) is not int
        or rls["table_count"] != len(_OWNER_TABLES)
        or rls["exact_forced_rls"] is not True
        or rls["api_has_direct_dml"] is not False
    ):
        raise HttpServicePreflightError()


async def _configure_pool_connection(connection: Any) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )


async def _reset_connection(connection: Any) -> None:
    await connection.reset()


def _compose_authoritative_actor_resolver(
    resolver: ActorResolver,
    authority_verifier: LiveAuthorityVerifier,
) -> ActorResolver:
    async def resolve(
        request: Request,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        actor = await resolver(request, scopes)
        if not isinstance(actor, VerifiedActor):
            raise HttpAuthError("auth_token_invalid")
        outcome = authority_verifier(request, actor)
        if not inspect.isawaitable(outcome):
            raise HttpAuthError("auth_configuration_invalid")
        await outcome
        return actor

    return resolve


def _service_response(
    status_code: int,
    content: Mapping[str, object],
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=dict(content),
        headers=_NO_STORE_HEADERS,
    )


def create_governed_memory_http_service(
    settings: GovernedMemoryHttpServiceSettings | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    pool_factory: PoolFactory = asyncpg.create_pool,
    actor_resolver_factory: ActorResolverFactory = create_supabase_actor_resolver,
    authority_verifier: LiveAuthorityVerifier | None = None,
    token_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    jwks_fetcher: JwksFetcher | None = None,
) -> FastAPI:
    """Build an isolated service; construction performs no external I/O."""

    if settings is not None and environment is not None:
        raise HttpServiceConfigurationError(
            "governed_memory_configuration_source_ambiguous"
        )
    active = settings or GovernedMemoryHttpServiceSettings.from_environment(
        environment
    )
    runtime = _ServiceRuntime(active.mode)
    actor_resolver: ActorResolver | None = None
    facade: PostgresOwnerStore | None = None

    if active.mode == "on":
        if not callable(pool_factory) or not callable(actor_resolver_factory):
            raise HttpServiceConfigurationError(
                "governed_memory_runtime_factory_invalid"
            )
        if authority_verifier is None or not callable(authority_verifier):
            raise HttpServiceConfigurationError(
                "governed_memory_live_authority_verifier_required"
            )
        auth_kwargs: dict[str, object] = {"token_clock": token_clock}
        if jwks_fetcher is not None:
            auth_kwargs["fetcher"] = jwks_fetcher
        base_resolver = actor_resolver_factory(
            active.authentication_config(),
            **auth_kwargs,
        )
        if not callable(base_resolver):
            raise HttpServiceConfigurationError(
                "governed_memory_actor_resolver_invalid"
            )
        actor_resolver = _compose_authoritative_actor_resolver(
            base_resolver,
            authority_verifier,
        )
        facade = PostgresOwnerStore(runtime.pool_handle)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if active.mode == "off":
            yield
            return

        assert active.postgres_dsn is not None
        pool: Any | None = None
        attached = False
        try:
            pool = await pool_factory(
                dsn=active.postgres_dsn,
                min_size=1,
                max_size=1,
                max_queries=500,
                max_inactive_connection_lifetime=60.0,
                command_timeout=8.0,
                reset=_reset_connection,
                init=_configure_pool_connection,
                server_settings={
                    "application_name": "governed_memory_http",
                    "statement_timeout": "8000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "8000",
                },
            )
            async with pool.acquire() as connection:
                await _preflight_connection(connection)
            runtime.pool_handle.attach(pool)
            attached = True
            runtime.ready = True
        except Exception:
            if pool is not None:
                await pool.close()
            raise

        try:
            yield
        finally:
            runtime.ready = False
            if attached:
                runtime.pool_handle.detach(pool)
            if pool is not None:
                await pool.close()

    service = FastAPI(
        title="Governed Memory API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    service.state.governed_memory_runtime = runtime

    @service.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return _service_response(
            200,
            {"status": "ok", "service": "governed_memory_http"},
        )

    @service.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        if active.mode == "off":
            return _service_response(
                503,
                {"error": {"code": "governed_memory_disabled"}},
            )
        if runtime.ready is not True:
            return _service_response(
                503,
                {"error": {"code": "governed_memory_not_ready"}},
            )
        return _service_response(
            200,
            {"status": "ready", "service": "governed_memory_http"},
        )

    service.include_router(
        create_owner_memory_router(
            actor_resolver=actor_resolver,
            facade=facade,
            feature_enabled=active.mode == "on",
        ),
        include_in_schema=False,
    )

    @service.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _exception: RequestValidationError,
    ) -> JSONResponse:
        return _service_response(
            400,
            {"error": {"code": "memory_request_invalid"}},
        )

    @service.exception_handler(StarletteHttpException)
    async def http_error_handler(
        _request: Request,
        exception: StarletteHttpException,
    ) -> JSONResponse:
        if exception.status_code == 404:
            return _service_response(
                404,
                {"error": {"code": "memory_route_not_found"}},
            )
        if exception.status_code == 405:
            return _service_response(
                405,
                {"error": {"code": "memory_method_not_allowed"}},
            )
        return _service_response(
            500,
            {"error": {"code": "memory_internal_error"}},
        )

    return service


__all__ = [
    "EXPECTED_DATABASE_NAME",
    "EXPECTED_DATABASE_ROLE",
    "GovernedMemoryHttpServiceSettings",
    "HttpServiceConfigurationError",
    "HttpServicePreflightError",
    "LiveAuthorityVerifier",
    "SERVICE_TOKEN_HEADER",
    "create_governed_memory_http_service",
]
