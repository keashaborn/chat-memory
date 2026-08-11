from __future__ import annotations

"""Inactive-by-default HTTP boundary for the clean governed-memory successor.

The router accepts only a ``VerifiedActor`` produced by an injected outer
authentication adapter.  It never accepts an owner or actor identifier from a
path, query, request body, or identity assertion header.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import Enum
import json
import re
from types import MappingProxyType
from typing import Any, Protocol
import unicodedata
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from .api import (
    CLAIM_OWNER_ROUTE_SPECIFICATIONS,
    RouteSpecification,
    validate_route_body,
)
from .auth import ActorRole, ActorScope, VerifiedActor
from .contracts import ContractViolation


MAX_REQUEST_BODY_BYTES = 65_536
OWNER_REQUEST_DEADLINE_SECONDS = 20.0


class MemoryHttpFailure(str, Enum):
    REQUEST_INVALID = "memory_request_invalid"
    AUTHENTICATION_REQUIRED = "memory_authentication_required"
    AUTHORIZATION_DENIED = "memory_authorization_denied"
    RESOURCE_NOT_FOUND = "memory_resource_not_found"
    STATE_CONFLICT = "memory_state_conflict"
    SUCCESSOR_UNAVAILABLE = "memory_successor_unavailable"
    OPERATION_OUTCOME_UNKNOWN = "memory_operation_outcome_unknown"
    INTERNAL_ERROR = "memory_internal_error"


_FAILURE_STATUS = MappingProxyType(
    {
        MemoryHttpFailure.REQUEST_INVALID: 400,
        MemoryHttpFailure.AUTHENTICATION_REQUIRED: 401,
        MemoryHttpFailure.AUTHORIZATION_DENIED: 403,
        MemoryHttpFailure.RESOURCE_NOT_FOUND: 404,
        MemoryHttpFailure.STATE_CONFLICT: 409,
        MemoryHttpFailure.SUCCESSOR_UNAVAILABLE: 503,
        MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN: 503,
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
    {
        specification.operation: specification
        for specification in CLAIM_OWNER_ROUTE_SPECIFICATIONS
    }
)
if (
    tuple(
        specification.operation
        for specification in CLAIM_OWNER_ROUTE_SPECIFICATIONS
    )
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
        "auth_live_authorization_invalid",
        "auth_live_request_invalid",
        "auth_live_session_denied",
        "auth_live_session_mismatch",
        "auth_live_user_mismatch",
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
        "auth_live_actor_invalid",
        "auth_role_denied",
    }
)
_AUTH_UNAVAILABLE_ERROR_CODES = frozenset(
    {
        "auth_configuration_invalid",
        "auth_clock_invalid",
        "auth_key_resolver_failed",
        "auth_live_authority_unavailable",
        "auth_live_configuration_invalid",
        "auth_live_response_invalid",
        "auth_live_session_response_invalid",
        "auth_key_resolution_unavailable",
        "auth_service_token_configuration_invalid",
    }
)


_MUTATION_OPERATIONS = frozenset(
    {"review_proposal", "correct_claim", "retract_claim", "delete_claim"}
)
_DETERMINISTIC_MUTATION_FAILURES = frozenset(
    {
        MemoryHttpFailure.REQUEST_INVALID,
        MemoryHttpFailure.AUTHORIZATION_DENIED,
        MemoryHttpFailure.RESOURCE_NOT_FOUND,
        MemoryHttpFailure.STATE_CONFLICT,
    }
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_TYPES = frozenset(
    {"self", "person", "pet", "organization", "place", "other"}
)
_PREDICATES = frozenset(
    {
        "commitment.active",
        "entity.attribute",
        "event.occurred",
        "identity.alias",
        "identity.preferred_name",
        "location.association",
        "preference.personal",
        "relationship.kind",
    }
)
_CLAIM_STATES = frozenset(
    {"active", "correction_pending", "retracted", "deletion_pending"}
)
_EPISTEMIC_STATES = frozenset({"supported", "uncertain", "disputed"})
_SENSITIVITIES = frozenset(
    {"ordinary", "sensitive_self", "sensitive_third_party"}
)
_SURFACES = frozenset({"normal", "explicit_only", "never"})
_STATUS_KEYS = frozenset(
    {
        "active_claims",
        "pending_proposals",
        "pending_projection",
        "failed_projection",
        "last_transition_at",
    }
)
_CLAIM_KEYS = frozenset(
    {
        "claim_id",
        "lifecycle_state",
        "revision_id",
        "revision_number",
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "object_kind",
        "predicate",
        "epistemic_state",
        "sensitivity",
        "updated_at",
    }
)
_CLAIM_DETAIL_KEYS = _CLAIM_KEYS | frozenset(
    {
        "subject_entity_type",
        "subject_entity_key",
        "subject_display_name",
        "object_entity_type",
        "object_entity_key",
        "object_display_name",
        "object_literal",
    }
)
_PROPOSAL_KEYS = frozenset(
    {
        "proposal_id",
        "operation_id",
        "proposal_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "predicate_catalog_sha256",
        "source_excerpt",
        "subject_entity_type",
        "subject_entity_key",
        "subject_display_name",
        "predicate",
        "object_kind",
        "object_entity_type",
        "object_entity_key",
        "object_display_name",
        "object_literal",
        "epistemic_state",
        "sensitivity",
        "projectable",
        "domains",
        "intents",
        "surface",
        "requires_explicit",
        "valid_from",
        "valid_to",
        "correction_of_claim_id",
        "expires_at",
        "created_at",
    }
)
_REVIEW_RECEIPT_KEYS = frozenset(
    {"outcome", "claim_id", "revision_id", "outbox_id"}
)
_CORRECTION_RECEIPT_KEYS = frozenset(
    {"outcome", "proposal_id", "proposal_sha256", "review_operation_id"}
)
_TRANSITION_RECEIPT_KEYS = frozenset({"outcome", "outbox_id"})
_OPERATION_KEYS = frozenset({"operation_id", "events"})
_OPERATION_EVENT_KEYS = frozenset(
    {
        "operation_id",
        "transition_code",
        "object_type",
        "object_id",
        "reason_code",
        "new_state_sha256",
        "created_at",
    }
)


class _SuccessContractError(ValueError):
    pass


def _closed_success_mapping(
    value: object,
    expected_keys: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _SuccessContractError
    result = dict(value)
    if frozenset(result) != expected_keys:
        raise _SuccessContractError
    return result


def _success_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise _SuccessContractError
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise _SuccessContractError from exc
    if str(parsed) != value:
        raise _SuccessContractError
    return value


def _success_nullable_uuid(value: object) -> str | None:
    return None if value is None else _success_uuid(value)


def _success_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _SuccessContractError
    return value


def _success_nfc_text(value: object, maximum_bytes: int) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise _SuccessContractError
    size = len(value.encode("utf-8"))
    if not 1 <= size <= maximum_bytes:
        raise _SuccessContractError
    return value


def _success_nullable_nfc_text(
    value: object,
    maximum_bytes: int,
) -> str | None:
    return None if value is None else _success_nfc_text(value, maximum_bytes)


def _success_key(value: object, maximum_bytes: int = 128) -> str:
    if (
        not isinstance(value, str)
        or _KEY_RE.fullmatch(value) is None
        or len(value.encode("ascii")) > maximum_bytes
    ):
        raise _SuccessContractError
    return value


def _success_timestamp(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _SuccessContractError
    return value


def _success_nullable_timestamp(value: object) -> datetime | None:
    return None if value is None else _success_timestamp(value)


def _success_int(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        raise _SuccessContractError
    return value


def _success_enum(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _SuccessContractError
    return value


def _success_text_array(value: object) -> list[str]:
    if type(value) is not list or len(value) > 16:
        raise _SuccessContractError
    for item in value:
        _success_nfc_text(item, 256)
    return value


def _validate_status_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _closed_success_mapping(value, _STATUS_KEYS)
    for field in (
        "active_claims",
        "pending_proposals",
        "pending_projection",
        "failed_projection",
    ):
        _success_int(result[field])
    _success_nullable_timestamp(result["last_transition_at"])
    return result


def _validate_claim_common(
    result: dict[str, Any],
    expected_claim_id: UUID | None,
) -> dict[str, Any]:
    claim_id = _success_uuid(result["claim_id"])
    if expected_claim_id is not None and claim_id != str(expected_claim_id):
        raise _SuccessContractError
    _success_enum(result["lifecycle_state"], _CLAIM_STATES)
    _success_uuid(result["revision_id"])
    _success_int(result["revision_number"], 1)
    for field in (
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
    ):
        _success_sha256(result[field])
    _success_enum(result["object_kind"], frozenset({"literal", "entity"}))
    if result["predicate"] not in _PREDICATES:
        raise _SuccessContractError
    _success_enum(result["epistemic_state"], _EPISTEMIC_STATES)
    _success_enum(result["sensitivity"], _SENSITIVITIES)
    _success_timestamp(result["updated_at"])
    return result


def _validate_claim_row(value: object, expected_claim_id: UUID | None) -> dict[str, Any]:
    return _validate_claim_common(
        _closed_success_mapping(value, _CLAIM_KEYS),
        expected_claim_id,
    )


def _validate_claim_detail_row(
    value: object,
    expected_claim_id: UUID,
) -> dict[str, Any]:
    result = _validate_claim_common(
        _closed_success_mapping(value, _CLAIM_DETAIL_KEYS),
        expected_claim_id,
    )
    subject_type = _success_enum(result["subject_entity_type"], _ENTITY_TYPES)
    subject_key = _success_nfc_text(result["subject_entity_key"], 200)
    if subject_type == "self":
        if subject_key != "self" or result["subject_display_name"] is not None:
            raise _SuccessContractError
    else:
        if subject_key == "self":
            raise _SuccessContractError
        _success_nfc_text(result["subject_display_name"], 256)
    if result["object_kind"] == "literal":
        _success_nfc_text(result["object_literal"], 2_000)
        if any(
            result[field] is not None
            for field in (
                "object_entity_type",
                "object_entity_key",
                "object_display_name",
            )
        ):
            raise _SuccessContractError
    else:
        _success_enum(result["object_entity_type"], _ENTITY_TYPES)
        _success_nfc_text(result["object_entity_key"], 200)
        _success_nfc_text(result["object_display_name"], 256)
        if result["object_literal"] is not None:
            raise _SuccessContractError
    return result


def _validate_claim_list_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > 100:
        raise _SuccessContractError
    return [_validate_claim_row(item, None) for item in value]


def _validate_claim_success(
    value: object,
    resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if resource_id is None:
        raise _SuccessContractError
    return _validate_claim_detail_row(value, resource_id)


def _validate_proposal_row(value: object) -> dict[str, Any]:
    result = _closed_success_mapping(value, _PROPOSAL_KEYS)
    _success_uuid(result["proposal_id"])
    _success_uuid(result["operation_id"])
    for field in (
        "proposal_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "predicate_catalog_sha256",
    ):
        _success_sha256(result[field])
    _success_nullable_nfc_text(result["source_excerpt"], 16_000)
    subject_type = _success_enum(result["subject_entity_type"], _ENTITY_TYPES)
    _success_nfc_text(result["subject_entity_key"], 200)
    subject_display = result["subject_display_name"]
    if subject_type == "self":
        if subject_display is not None:
            raise _SuccessContractError
    else:
        _success_nfc_text(subject_display, 256)
    if result["predicate"] not in _PREDICATES:
        raise _SuccessContractError
    object_kind = _success_enum(
        result["object_kind"], frozenset({"literal", "entity"})
    )
    if object_kind == "literal":
        _success_nfc_text(result["object_literal"], 2_000)
        if any(
            result[field] is not None
            for field in (
                "object_entity_type",
                "object_entity_key",
                "object_display_name",
            )
        ):
            raise _SuccessContractError
    else:
        _success_enum(result["object_entity_type"], _ENTITY_TYPES)
        _success_nfc_text(result["object_entity_key"], 200)
        _success_nfc_text(result["object_display_name"], 256)
        if result["object_literal"] is not None:
            raise _SuccessContractError
    _success_enum(result["epistemic_state"], _EPISTEMIC_STATES)
    _success_enum(result["sensitivity"], _SENSITIVITIES)
    if type(result["projectable"]) is not bool:
        raise _SuccessContractError
    _success_text_array(result["domains"])
    _success_text_array(result["intents"])
    _success_enum(result["surface"], _SURFACES)
    if type(result["requires_explicit"]) is not bool:
        raise _SuccessContractError
    valid_from = _success_nullable_timestamp(result["valid_from"])
    valid_to = _success_nullable_timestamp(result["valid_to"])
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        raise _SuccessContractError
    _success_nullable_uuid(result["correction_of_claim_id"])
    created_at = _success_timestamp(result["created_at"])
    expires_at = _success_timestamp(result["expires_at"])
    if not 0 < (expires_at - created_at).total_seconds() <= 7 * 24 * 60 * 60:
        raise _SuccessContractError
    return result


def _validate_proposal_list_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > 100:
        raise _SuccessContractError
    return [_validate_proposal_row(item) for item in value]


def _validate_review_success(
    value: object,
    _resource_id: UUID | None,
    body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _closed_success_mapping(value, _REVIEW_RECEIPT_KEYS)
    decision = None if body is None else body.get("decision")
    outcome = result["outcome"]
    claim_id = _success_nullable_uuid(result["claim_id"])
    revision_id = _success_nullable_uuid(result["revision_id"])
    outbox_id = _success_nullable_uuid(result["outbox_id"])
    if decision == "admit":
        if (
            outcome not in {"admitted", "replayed"}
            or claim_id is None
            or revision_id is None
            or outbox_id is None
        ):
            raise _SuccessContractError
    elif decision == "reject":
        if outcome not in {"rejected", "replayed"} or revision_id is not None:
            raise _SuccessContractError
        if (claim_id is None) != (outbox_id is None):
            raise _SuccessContractError
    else:
        raise _SuccessContractError
    return result


def _validate_correction_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _closed_success_mapping(value, _CORRECTION_RECEIPT_KEYS)
    _success_enum(
        result["outcome"], frozenset({"correction_pending", "replayed"})
    )
    _success_uuid(result["proposal_id"])
    _success_sha256(result["proposal_sha256"])
    _success_uuid(result["review_operation_id"])
    return result


def _validate_retract_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _closed_success_mapping(value, _TRANSITION_RECEIPT_KEYS)
    _success_enum(result["outcome"], frozenset({"retracted", "replayed"}))
    _success_uuid(result["outbox_id"])
    return result


def _validate_delete_success(
    value: object,
    _resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = _closed_success_mapping(value, _TRANSITION_RECEIPT_KEYS)
    _success_enum(
        result["outcome"], frozenset({"deletion_pending", "replayed"})
    )
    _success_uuid(result["outbox_id"])
    return result


def _validate_operation_success(
    value: object,
    resource_id: UUID | None,
    _body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if resource_id is None:
        raise _SuccessContractError
    result = _closed_success_mapping(value, _OPERATION_KEYS)
    operation_id = _success_uuid(result["operation_id"])
    if operation_id != str(resource_id):
        raise _SuccessContractError
    events = result["events"]
    if type(events) is not list or not 1 <= len(events) <= 1_000:
        raise _SuccessContractError
    for event_value in events:
        event = _closed_success_mapping(event_value, _OPERATION_EVENT_KEYS)
        if _success_uuid(event["operation_id"]) != operation_id:
            raise _SuccessContractError
        _success_key(event["transition_code"], 96)
        _success_key(event["object_type"], 64)
        _success_uuid(event["object_id"])
        _success_key(event["reason_code"], 128)
        _success_sha256(event["new_state_sha256"])
        _success_timestamp(event["created_at"])
    return result


SuccessValidator = Callable[
    [object, UUID | None, Mapping[str, Any] | None],
    object,
]
_SUCCESS_VALIDATORS: Mapping[str, SuccessValidator] = MappingProxyType(
    {
        "status": _validate_status_success,
        "list_claims": _validate_claim_list_success,
        "get_claim": _validate_claim_success,
        "list_proposals": _validate_proposal_list_success,
        "review_proposal": _validate_review_success,
        "correct_claim": _validate_correction_success,
        "retract_claim": _validate_retract_success,
        "delete_claim": _validate_delete_success,
        "get_operation": _validate_operation_success,
    }
)
if frozenset(_SUCCESS_VALIDATORS) != frozenset(_EXPECTED_OPERATIONS):
    raise RuntimeError("owner_success_contract_not_closed")


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


class _OwnerRequestDeadlineRoute(APIRoute):
    """Cancel one complete owner request before returning a closed timeout."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        original_handler = super().get_route_handler()

        async def deadline_handler(request: Request) -> Any:
            try:
                async with asyncio.timeout(OWNER_REQUEST_DEADLINE_SECONDS):
                    return await original_handler(request)
            except TimeoutError:
                failure = (
                    MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN
                    if request.method in {"POST", "DELETE"}
                    else MemoryHttpFailure.SUCCESSOR_UNAVAILABLE
                )
                return _failure_response(failure)

        return deadline_handler


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
    if sqlstate in {"23503", "23505", "23514", "40001", "P0001"}:
        return MemoryHttpFailure.STATE_CONFLICT
    if sqlstate == "P0002":
        return MemoryHttpFailure.RESOURCE_NOT_FOUND
    if sqlstate == "22023":
        return MemoryHttpFailure.REQUEST_INVALID
    if sqlstate == "42501":
        return MemoryHttpFailure.AUTHORIZATION_DENIED
    if isinstance(exception, (ConnectionError, OSError, TimeoutError)):
        return MemoryHttpFailure.SUCCESSOR_UNAVAILABLE
    return MemoryHttpFailure.INTERNAL_ERROR


def _operation_failure(
    operation: str,
    exception: Exception,
) -> MemoryHttpFailure:
    failure = (
        exception.failure
        if isinstance(exception, MemoryHttpError)
        else _service_failure(exception)
    )
    if (
        operation in _MUTATION_OPERATIONS
        and failure not in _DETERMINISTIC_MUTATION_FAILURES
    ):
        return MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN
    return failure


def _success_response(
    operation: str,
    value: object,
    *,
    resource_id: UUID | None = None,
    body: Mapping[str, Any] | None = None,
    missing_is_not_found: bool = False,
) -> JSONResponse:
    if value is None:
        if missing_is_not_found:
            return _failure_response(MemoryHttpFailure.RESOURCE_NOT_FOUND)
    contract_failure = (
        MemoryHttpFailure.OPERATION_OUTCOME_UNKNOWN
        if operation in _MUTATION_OPERATIONS
        else MemoryHttpFailure.INTERNAL_ERROR
    )
    try:
        validator = _SUCCESS_VALIDATORS[operation]
        material = validator(value, resource_id, body)
        content = jsonable_encoder(material)
        return JSONResponse(
            status_code=200,
            content=content,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception:
        return _failure_response(contract_failure)


def create_owner_memory_router(
    *,
    actor_resolver: ActorResolver | None = None,
    facade: OwnerMemoryFacade | None = None,
    feature_enabled: bool = False,
) -> APIRouter:
    """Create the exact owner route manifest without activating it by default."""

    router = APIRouter(route_class=_OwnerRequestDeadlineRoute)

    def prepare_input(
        request: Request,
        operation: str,
    ) -> RouteSpecification | JSONResponse:
        if feature_enabled is not True:
            return _disabled_response()
        if actor_resolver is None or facade is None:
            return _unconfigured_response()
        try:
            _prohibit_identity_assertions(request)
            return _SPECIFICATIONS[operation]
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)

    async def authenticate_for_store(
        request: Request,
        operation: str,
    ) -> VerifiedActor | JSONResponse:
        assert actor_resolver is not None
        try:
            return await _resolve_owner_actor(
                request,
                resolver=actor_resolver,
                scopes=_OPERATION_SCOPES[operation],
            )
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)

    async def call_service(
        operation: str,
        awaitable: Awaitable[object],
        *,
        resource_id: UUID | None = None,
        body: Mapping[str, Any] | None = None,
        missing_is_not_found: bool = False,
    ) -> JSONResponse:
        try:
            value = await awaitable
        except Exception as exc:
            return _failure_response(_operation_failure(operation, exc))
        return _success_response(
            operation,
            value,
            resource_id=resource_id,
            body=body,
            missing_is_not_found=missing_is_not_found,
        )

    async def status_endpoint(request: Request) -> JSONResponse:
        specification = prepare_input(request, "status")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "status")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service("status", facade.status(actor))

    async def list_claims_endpoint(request: Request) -> JSONResponse:
        specification = prepare_input(request, "list_claims")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "list_claims")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service("list_claims", facade.list_claims(actor))

    async def get_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        specification = prepare_input(request, "get_claim")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(claim_id)
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "get_claim")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "get_claim",
            facade.get_claim(actor, resource_id),
            resource_id=resource_id,
            missing_is_not_found=True,
        )

    async def list_proposals_endpoint(request: Request) -> JSONResponse:
        specification = prepare_input(request, "list_proposals")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "list_proposals")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service("list_proposals", facade.list_proposals(actor))

    async def review_proposal_endpoint(
        request: Request, proposal_id: str
    ) -> JSONResponse:
        specification = prepare_input(request, "review_proposal")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(proposal_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "review_proposal")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "review_proposal",
            facade.review_proposal(actor, resource_id, body),
            resource_id=resource_id,
            body=body,
        )

    async def correct_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        specification = prepare_input(request, "correct_claim")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "correct_claim")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "correct_claim",
            facade.correct_claim(actor, resource_id, body),
            resource_id=resource_id,
            body=body,
        )

    async def retract_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        specification = prepare_input(request, "retract_claim")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "retract_claim")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "retract_claim",
            facade.retract_claim(actor, resource_id, body),
            resource_id=resource_id,
            body=body,
        )

    async def delete_claim_endpoint(request: Request, claim_id: str) -> JSONResponse:
        specification = prepare_input(request, "delete_claim")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(claim_id)
            body = await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "delete_claim")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "delete_claim",
            facade.delete_claim(actor, resource_id, body),
            resource_id=resource_id,
            body=body,
        )

    async def get_operation_endpoint(
        request: Request, operation_id: str
    ) -> JSONResponse:
        specification = prepare_input(request, "get_operation")
        if isinstance(specification, JSONResponse):
            return specification
        try:
            resource_id = _canonical_uuid(operation_id)
            await _read_closed_body(request, specification)
        except MemoryHttpError as exc:
            return _failure_response(exc.failure)
        actor = await authenticate_for_store(request, "get_operation")
        if isinstance(actor, JSONResponse):
            return actor
        assert facade is not None
        return await call_service(
            "get_operation",
            facade.get_operation(actor, resource_id),
            resource_id=resource_id,
            missing_is_not_found=True,
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
    "OWNER_REQUEST_DEADLINE_SECONDS",
    "MemoryHttpError",
    "MemoryHttpFailure",
    "OwnerMemoryFacade",
    "create_owner_memory_app",
    "create_owner_memory_router",
]
