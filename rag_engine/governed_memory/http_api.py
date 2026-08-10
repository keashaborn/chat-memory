from __future__ import annotations

"""Inactive-by-default HTTP boundary for the clean governed-memory successor.

The router accepts only a ``VerifiedActor`` produced by an injected outer
authentication adapter.  It never accepts an owner or actor identifier from a
path, query, request body, or identity assertion header.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import Enum
import json
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from .api import OWNER_ROUTE_SPECIFICATIONS, RouteSpecification, validate_route_body
from .auth import ActorRole, ActorScope, VerifiedActor
from .contracts import ContractViolation


MAX_REQUEST_BODY_BYTES = 65_536


class MemoryHttpFailure(str, Enum):
    REQUEST_INVALID = "memory_request_invalid"
    AUTHENTICATION_REQUIRED = "memory_authentication_required"
    AUTHORIZATION_DENIED = "memory_authorization_denied"
    RESOURCE_NOT_FOUND = "memory_resource_not_found"
    STATE_CONFLICT = "memory_state_conflict"
    SUCCESSOR_UNAVAILABLE = "memory_successor_unavailable"
    INTERNAL_ERROR = "memory_internal_error"


_FAILURE_STATUS = MappingProxyType(
    {
        MemoryHttpFailure.REQUEST_INVALID: 400,
        MemoryHttpFailure.AUTHENTICATION_REQUIRED: 401,
        MemoryHttpFailure.AUTHORIZATION_DENIED: 403,
        MemoryHttpFailure.RESOURCE_NOT_FOUND: 404,
        MemoryHttpFailure.STATE_CONFLICT: 409,
        MemoryHttpFailure.SUCCESSOR_UNAVAILABLE: 503,
        MemoryHttpFailure.INTERNAL_ERROR: 500,
    }
)


class MemoryHttpError(Exception):
    """A content-free public failure chosen by a trusted outer adapter."""

    def __init__(self, failure: MemoryHttpFailure) -> None:
        if not isinstance(failure, MemoryHttpFailure):
            failure = MemoryHttpFailure.INTERNAL_ERROR
        self.failure = failure
        super().__init__(failure.value)


class OwnerMemoryFacade(Protocol):
    async def status(self, actor: VerifiedActor) -> Mapping[str, Any]: ...

    async def list_claims(
        self, actor: VerifiedActor
    ) -> Sequence[Mapping[str, Any]]: ...

    async def get_claim(
        self, actor: VerifiedActor, claim_id: UUID
    ) -> Mapping[str, Any] | None: ...

    async def list_proposals(
        self, actor: VerifiedActor
    ) -> Sequence[Mapping[str, Any]]: ...

    async def review_proposal(
        self,
        actor: VerifiedActor,
        proposal_id: UUID,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def correct_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def retract_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def delete_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def get_operation(
        self, actor: VerifiedActor, operation_id: UUID
    ) -> Mapping[str, Any] | None: ...


ActorResolver = Callable[
    [Request, tuple[ActorScope, ...]], Awaitable[VerifiedActor]
]


_EXPECTED_OPERATIONS = (
    "status",
    "list_claims",
    "get_claim",
    "list_proposals",
    "review_proposal",
    "correct_claim",
    "retract_claim",
    "delete_claim",
    "get_operation",
)
_SPECIFICATIONS = MappingProxyType(
    {specification.operation: specification for specification in OWNER_ROUTE_SPECIFICATIONS}
)
if (
    tuple(specification.operation for specification in OWNER_ROUTE_SPECIFICATIONS)
    != _EXPECTED_OPERATIONS
    or len(_SPECIFICATIONS) != len(_EXPECTED_OPERATIONS)
):
    raise RuntimeError("owner_route_manifest_not_closed")


_OPERATION_SCOPES = MappingProxyType(
    {
        "status": (ActorScope.READ_CLAIMS,),
        "list_claims": (ActorScope.READ_CLAIMS,),
        "get_claim": (ActorScope.READ_CLAIMS,),
        "list_proposals": (ActorScope.REVIEW_PROPOSALS,),
        "review_proposal": (ActorScope.REVIEW_PROPOSALS,),
        "correct_claim": (ActorScope.MUTATE_CLAIMS,),
        "retract_claim": (ActorScope.MUTATE_CLAIMS,),
        "delete_claim": (ActorScope.MUTATE_CLAIMS,),
        "get_operation": (ActorScope.READ_CLAIMS,),
    }
)


_PROHIBITED_IDENTITY_FIELDS = frozenset(
    {"actor_id", "actor_user_id", "owner", "owner_id", "owner_user_id", "user_id"}
)
_PROHIBITED_IDENTITY_HEADERS = frozenset(
    {
        "actor-user-id",
        "owner-user-id",
        "user-id",
        "x-actor-id",
        "x-actor-user-id",
        "x-owner-id",
        "x-owner-user-id",
        "x-user-id",
        "x-vs-actor-user-id",
        "x-vs-owner-user-id",
    }
)


_AUTHENTICATION_ERROR_CODES = frozenset(
    {
        "auth_algorithm_denied",
        "auth_anonymous_claim_invalid",
        "auth_audience_mismatch",
        "auth_exp_invalid",
        "auth_header_ambiguous",
        "auth_header_invalid",
        "auth_header_missing",
        "auth_headers_invalid",
        "auth_iat_invalid",
        "auth_issuer_mismatch",
        "auth_key_id_invalid",
        "auth_key_invalid",
        "auth_key_not_found",
        "auth_nbf_invalid",
        "auth_required_claim_missing",
        "auth_service_token_invalid",
        "auth_service_token_missing",
        "auth_session_id_invalid",
        "auth_signature_invalid",
        "auth_subject_invalid",
        "auth_token_expired",
        "auth_token_header_invalid",
        "auth_token_invalid",
        "auth_token_lifetime_invalid",
        "auth_token_malformed",
        "auth_token_not_yet_valid",
    }
)
_AUTHORIZATION_ERROR_CODES = frozenset(
    {
        "auth_anonymous_forbidden",
        "auth_explicit_authority_header_forbidden",
        "auth_role_denied",
    }
)
_AUTH_UNAVAILABLE_ERROR_CODES = frozenset(
    {
        "auth_configuration_invalid",
        "auth_clock_invalid",
        "auth_key_resolver_failed",
        "auth_key_resolution_unavailable",
        "auth_service_token_configuration_invalid",
    }
)


class _DuplicateJsonKey(ValueError):
    pass


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _failure_response(
    failure: MemoryHttpFailure,
    *,
    status_code: int | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    status = _FAILURE_STATUS[failure] if status_code is None else status_code
    if failure is MemoryHttpFailure.AUTHENTICATION_REQUIRED:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        content={"error": {"code": failure.value}},
        headers=headers,
    )


def _disabled_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "memory_successor_disabled"}},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _unconfigured_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "memory_successor_unconfigured"}},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _prohibit_identity_assertions(request: Request) -> None:
    if request.query_params:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID)
    if any(name.lower() in _PROHIBITED_IDENTITY_HEADERS for name in request.headers):
        raise MemoryHttpError(MemoryHttpFailure.AUTHORIZATION_DENIED)


def _reject_identity_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _PROHIBITED_IDENTITY_FIELDS:
                raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID)
            _reject_identity_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_identity_fields(item)


async def _read_closed_body(
    request: Request, specification: RouteSpecification
) -> dict[str, Any]:
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_REQUEST_BODY_BYTES:
                raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID)
            raw.extend(chunk)
    except MemoryHttpError:
        raise
    except Exception as exc:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID) from exc
    if not raw:
        parsed: object = None
    else:
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID)
        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_closed_json_object,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateJsonKey,
            ValueError,
            RecursionError,
        ) as exc:
            raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID) from exc
    try:
        _reject_identity_fields(parsed)
    except RecursionError as exc:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID) from exc
    try:
        return validate_route_body(
            specification,
            parsed if isinstance(parsed, Mapping) else parsed,  # type: ignore[arg-type]
        )
    except ContractViolation as exc:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID) from exc


def _canonical_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID) from exc
    if str(parsed) != value:
        raise MemoryHttpError(MemoryHttpFailure.REQUEST_INVALID)
    return parsed


def _auth_failure(exception: Exception) -> MemoryHttpFailure:
    code = getattr(exception, "code", None)
    if code in _AUTHENTICATION_ERROR_CODES:
        return MemoryHttpFailure.AUTHENTICATION_REQUIRED
    if code in _AUTHORIZATION_ERROR_CODES:
        return MemoryHttpFailure.AUTHORIZATION_DENIED
    if code in _AUTH_UNAVAILABLE_ERROR_CODES:
        return MemoryHttpFailure.SUCCESSOR_UNAVAILABLE
    return MemoryHttpFailure.INTERNAL_ERROR


async def _resolve_owner_actor(
    request: Request,
    *,
    resolver: ActorResolver,
    scopes: tuple[ActorScope, ...],
) -> VerifiedActor:
    try:
        actor = await resolver(request, scopes)
    except MemoryHttpError:
        raise
    except Exception as exc:
        raise MemoryHttpError(_auth_failure(exc)) from exc
    if not isinstance(actor, VerifiedActor):
        raise MemoryHttpError(MemoryHttpFailure.AUTHENTICATION_REQUIRED)
    if actor.role is not ActorRole.OWNER:
        raise MemoryHttpError(MemoryHttpFailure.AUTHORIZATION_DENIED)
    if any(scope not in actor.scopes for scope in scopes):
        raise MemoryHttpError(MemoryHttpFailure.AUTHORIZATION_DENIED)
    return actor


def _service_failure(exception: Exception) -> MemoryHttpFailure:
    if isinstance(exception, ContractViolation):
        return MemoryHttpFailure.REQUEST_INVALID
    code = getattr(exception, "code", None)
    if code == "database_request_rejected":
        return MemoryHttpFailure.REQUEST_INVALID
    if code == "database_resource_not_found":
        return MemoryHttpFailure.RESOURCE_NOT_FOUND
    if code in {"database_conflict", "database_operation_rejected"}:
        return MemoryHttpFailure.STATE_CONFLICT
    if code == "database_authority_denied":
        return MemoryHttpFailure.AUTHORIZATION_DENIED
    if code == "database_unavailable":
        return MemoryHttpFailure.SUCCESSOR_UNAVAILABLE
    sqlstate = getattr(exception, "sqlstate", None)
    if sqlstate in {"23505", "23514", "40001"}:
        return MemoryHttpFailure.STATE_CONFLICT
    if sqlstate == "P0002":
        return MemoryHttpFailure.RESOURCE_NOT_FOUND
    if sqlstate == "22023":
        return MemoryHttpFailure.REQUEST_INVALID
    if isinstance(exception, (ConnectionError, OSError, TimeoutError)):
        return MemoryHttpFailure.SUCCESSOR_UNAVAILABLE
    return MemoryHttpFailure.INTERNAL_ERROR


def _success_response(value: object, *, missing_is_not_found: bool = False) -> JSONResponse:
    if value is None:
        if missing_is_not_found:
            return _failure_response(MemoryHttpFailure.RESOURCE_NOT_FOUND)
        return _failure_response(MemoryHttpFailure.INTERNAL_ERROR)
    if isinstance(value, Mapping):
        material: object = dict(value)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ) and all(isinstance(item, Mapping) for item in value):
        material = [dict(item) for item in value]
    else:
        return _failure_response(MemoryHttpFailure.INTERNAL_ERROR)
    try:
        content = jsonable_encoder(material)
    except Exception:
        return _failure_response(MemoryHttpFailure.INTERNAL_ERROR)
    return JSONResponse(
        status_code=200,
        content=content,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def create_owner_memory_router(
    *,
    actor_resolver: ActorResolver | None = None,
    facade: OwnerMemoryFacade | None = None,
    feature_enabled: bool = False,
) -> APIRouter:
    """Create the exact owner route manifest without activating it by default."""

    router = APIRouter()

    async def prepare(
        request: Request, operation: str
    ) -> tuple[VerifiedActor, RouteSpecification] | JSONResponse:
        if feature_enabled is not True:
            return _disabled_response()
        if actor_resolver is None or facade is None:
            return _unconfigured_response()
        try:
            _prohibit_identity_assertions(request)
            actor = await _resolve_owner_actor(
                request,
                resolver=actor_resolver,
                scopes=_OPERATION_SCOPES[operation],
            )
            return actor, _SPECIFICATIONS[operation]
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)

    async def call_service(
        awaitable: Awaitable[object], *, missing_is_not_found: bool = False
    ) -> JSONResponse:
        try:
            value = await awaitable
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        except Exception as exc:
            return _failure_response(_service_failure(exc))
        return _success_response(value, missing_is_not_found=missing_is_not_found)

    async def status_endpoint(request: Request) -> JSONResponse:
        prepared = await prepare(request, "status")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.status(actor))

    async def list_claims_endpoint(request: Request) -> JSONResponse:
        prepared = await prepare(request, "list_claims")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.list_claims(actor))

    async def get_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        prepared = await prepare(request, "get_claim")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(claim_id)
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(
            facade.get_claim(actor, resource_id), missing_is_not_found=True
        )

    async def list_proposals_endpoint(request: Request) -> JSONResponse:
        prepared = await prepare(request, "list_proposals")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.list_proposals(actor))

    async def review_proposal_endpoint(
        request: Request, proposal_id: str
    ) -> JSONResponse:
        prepared = await prepare(request, "review_proposal")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(proposal_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.review_proposal(actor, resource_id, body))

    async def correct_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        prepared = await prepare(request, "correct_claim")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.correct_claim(actor, resource_id, body))

    async def retract_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        prepared = await prepare(request, "retract_claim")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.retract_claim(actor, resource_id, body))

    async def delete_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        prepared = await prepare(request, "delete_claim")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(facade.delete_claim(actor, resource_id, body))

    async def get_operation_endpoint(
        request: Request, operation_id: str
    ) -> JSONResponse:
        prepared = await prepare(request, "get_operation")
        if isinstance(prepared, JSONResponse):
            return prepared
        actor, specification = prepared
        try:
            resource_id = _canonical_uuid(operation_id)
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        assert facade is not None
        return await call_service(
            facade.get_operation(actor, resource_id), missing_is_not_found=True
        )

    endpoints = MappingProxyType(
        {
            "status": status_endpoint,
            "list_claims": list_claims_endpoint,
            "get_claim": get_claim_endpoint,
            "list_proposals": list_proposals_endpoint,
            "review_proposal": review_proposal_endpoint,
            "correct_claim": correct_claim_endpoint,
            "retract_claim": retract_claim_endpoint,
            "delete_claim": delete_claim_endpoint,
            "get_operation": get_operation_endpoint,
        }
    )
    for operation in _EXPECTED_OPERATIONS:
        specification = _SPECIFICATIONS[operation]
        router.add_api_route(
            specification.path,
            endpoints[operation],
            methods=[specification.method.value],
            name=operation,
            response_class=JSONResponse,
        )
    return router


def create_owner_memory_app(
    *,
    actor_resolver: ActorResolver | None = None,
    facade: OwnerMemoryFacade | None = None,
    feature_enabled: bool = False,
) -> FastAPI:
    """Create the standalone, non-documenting successor application."""

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.include_router(
        create_owner_memory_router(
            actor_resolver=actor_resolver,
            facade=facade,
            feature_enabled=feature_enabled,
        )
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exception: RequestValidationError
    ) -> JSONResponse:
        return _failure_response(MemoryHttpFailure.REQUEST_INVALID)

    @app.exception_handler(StarletteHttpException)
    async def http_error_handler(
        _request: Request, exception: StarletteHttpException
    ) -> JSONResponse:
        if exception.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "memory_route_not_found"}},
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if exception.status_code == 405:
            return JSONResponse(
                status_code=405,
                content={"error": {"code": "memory_method_not_allowed"}},
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return _failure_response(MemoryHttpFailure.INTERNAL_ERROR)

    return app


__all__ = [
    "ActorResolver",
    "MAX_REQUEST_BODY_BYTES",
    "MemoryHttpError",
    "MemoryHttpFailure",
    "OwnerMemoryFacade",
    "create_owner_memory_app",
    "create_owner_memory_router",
]
