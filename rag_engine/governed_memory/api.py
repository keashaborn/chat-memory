from __future__ import annotations

"""Pure closed route contracts; this module is not proof of runtime wiring."""

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping

from .contracts import ContractViolation, canonical_sha256


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteSpecification:
    method: HttpMethod
    path: str
    operation: str
    mutation: bool
    required_body_fields: tuple[str, ...] = ()
    optional_body_fields: tuple[str, ...] = ()
    server_time_owned: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.method, HttpMethod):
            raise ContractViolation("invalid_route_method")
        if not isinstance(self.path, str) or not self.path.startswith("/memory/"):
            raise ContractViolation("invalid_route_path")
        if "user_id" in self.path or "owner" in self.path:
            raise ContractViolation("owner_path_parameter_prohibited")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.operation):
            raise ContractViolation("invalid_route_operation")
        if type(self.mutation) is not bool:
            raise ContractViolation("invalid_route_mutation_flag")
        for values in (self.required_body_fields, self.optional_body_fields):
            if values != tuple(sorted(set(values))):
                raise ContractViolation("invalid_route_body_fields")
            for value in values:
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
                    raise ContractViolation("invalid_route_body_field")
        if set(self.required_body_fields).intersection(self.optional_body_fields):
            raise ContractViolation("overlapping_route_body_fields")
        if self.server_time_owned is not self.mutation:
            raise ContractViolation("invalid_route_server_time_authority")
        prohibited_time_fields = {
            "created_at",
            "occurred_at",
            "proposal_expires_at",
            "reviewed_at",
            "transaction_time",
        }
        if prohibited_time_fields.intersection(
            set(self.required_body_fields) | set(self.optional_body_fields)
        ):
            raise ContractViolation("caller_time_field_prohibited")


CONVERSATION_ERASURE_ROUTE_SPECIFICATION = RouteSpecification(
    method=HttpMethod.POST,
    path="/memory/conversations/erasure-requests",
    operation="request_conversation_erasure",
    mutation=True,
    required_body_fields=(
        "confirmation_sha256",
        "contract_version",
        "data_domain",
        "operation_id",
        "selector_kind",
    ),
    optional_body_fields=(
        "anchor_message_id",
        "recent_window_seconds",
        "thread_id",
    ),
    server_time_owned=True,
)

CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION = RouteSpecification(
    method=HttpMethod.GET,
    path="/memory/conversations/erasure-requests/{operation_id}",
    operation="get_conversation_erasure",
    mutation=False,
)


CLAIM_OWNER_ROUTE_SPECIFICATIONS = (
    RouteSpecification(
        method=HttpMethod.GET,
        path="/memory/status",
        operation="status",
        mutation=False,
    ),
    RouteSpecification(
        method=HttpMethod.GET,
        path="/memory/claims",
        operation="list_claims",
        mutation=False,
    ),
    RouteSpecification(
        method=HttpMethod.GET,
        path="/memory/claims/{claim_id}",
        operation="get_claim",
        mutation=False,
    ),
    RouteSpecification(
        method=HttpMethod.GET,
        path="/memory/proposals",
        operation="list_proposals",
        mutation=False,
    ),
    RouteSpecification(
        method=HttpMethod.POST,
        path="/memory/proposals/{proposal_id}/review",
        operation="review_proposal",
        mutation=True,
        required_body_fields=(
            "decision",
            "expected_predicate_catalog_sha256",
            "expected_proposal_sha256",
            "expected_selected_sha256",
            "expected_selection_binding_sha256",
            "expected_source_sha256",
            "operation_id",
            "reason_codes",
        ),
        server_time_owned=True,
    ),
    RouteSpecification(
        method=HttpMethod.POST,
        path="/memory/claims/{claim_id}/correct",
        operation="correct_claim",
        mutation=True,
        required_body_fields=(
            "expected_predicate_catalog_sha256",
            "expected_revision_sha256",
            "expected_state_sha256",
            "operation_id",
            "replacement",
        ),
        server_time_owned=True,
    ),
    RouteSpecification(
        method=HttpMethod.POST,
        path="/memory/claims/{claim_id}/retract",
        operation="retract_claim",
        mutation=True,
        required_body_fields=(
            "expected_revision_sha256",
            "expected_state_sha256",
            "operation_id",
        ),
        server_time_owned=True,
    ),
    RouteSpecification(
        method=HttpMethod.DELETE,
        path="/memory/claims/{claim_id}",
        operation="delete_claim",
        mutation=True,
        required_body_fields=(
            "expected_revision_sha256",
            "expected_state_sha256",
            "operation_id",
        ),
        server_time_owned=True,
    ),
    RouteSpecification(
        method=HttpMethod.GET,
        path="/memory/operations/{operation_id}",
        operation="get_operation",
        mutation=False,
    ),
)


OWNER_ROUTE_SPECIFICATIONS = (
    *CLAIM_OWNER_ROUTE_SPECIFICATIONS,
    CONVERSATION_ERASURE_ROUTE_SPECIFICATION,
    CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION,
)


def route_manifest_sha256() -> str:
    return canonical_sha256(
        "governed_memory.owner_routes",
        tuple(
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "mutation": route.mutation,
                "required_body_fields": route.required_body_fields,
                "optional_body_fields": route.optional_body_fields,
                "server_time_owned": route.server_time_owned,
            }
            for route in OWNER_ROUTE_SPECIFICATIONS
        ),
    )


def validate_route_body(
    specification: RouteSpecification, body: Mapping[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(specification, RouteSpecification):
        raise ContractViolation("invalid_route_specification")
    if not specification.mutation:
        if body not in (None, {}):
            raise ContractViolation("body_prohibited_for_read_route")
        return {}
    if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
        raise ContractViolation("invalid_route_body")
    keys = set(body)
    required = set(specification.required_body_fields)
    allowed = required | set(specification.optional_body_fields)
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise ContractViolation("route_body_contract_mismatch")
    return dict(body)


__all__ = [
    "CLAIM_OWNER_ROUTE_SPECIFICATIONS",
    "CONVERSATION_ERASURE_ROUTE_SPECIFICATION",
    "CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION",
    "HttpMethod",
    "OWNER_ROUTE_SPECIFICATIONS",
    "RouteSpecification",
    "route_manifest_sha256",
    "validate_route_body",
]
