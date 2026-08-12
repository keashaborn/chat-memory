from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping
import unittest
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Request

from rag_engine.governed_memory.api import (
    CLAIM_OWNER_ROUTE_SPECIFICATIONS,
    CONVERSATION_ERASURE_ROUTE_SPECIFICATION,
    CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION,
    OWNER_ROUTE_SPECIFICATIONS,
    route_manifest_sha256,
)
from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
)
from rag_engine.governed_memory.contracts import ContractViolation, canonical_sha256
from rag_engine.governed_memory.conversation_erasure_http import (
    create_conversation_erasure_router,
)
from rag_engine.governed_memory.deletion_contracts import (
    CONVERSATIONAL_ERASURE_DOMAIN,
    DELETION_REQUEST_CONTRACT_VERSION,
    BoundConversationDeletion,
    ConversationErasureState,
    ConversationErasureStatus,
    DeletionAuthority,
    DeletionSelectorKind,
    conversation_deletion_confirmation_sha256,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
SESSION = UUID("22222222-2222-4222-8222-222222222222")
OPERATION = UUID("33333333-3333-4333-8333-333333333333")
MESSAGE = UUID("44444444-4444-4444-8444-444444444444")
THREAD = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def owner_actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OWNER,
        session_id=SESSION,
        role=ActorRole.OWNER,
        scopes=(ActorScope.ERASE_CONVERSATIONS,),
        authentication_manifest_sha256=HASH_A,
        authenticated_at=NOW,
    )


def request_body(selector_kind: str, **values: object) -> dict[str, object]:
    try:
        kind = DeletionSelectorKind(selector_kind)
        confirmation = conversation_deletion_confirmation_sha256(
            operation_id=OPERATION,
            selector_kind=kind,
            thread_id=(
                UUID(str(values["thread_id"]))
                if "thread_id" in values
                else None
            ),
            anchor_message_id=(
                UUID(str(values["anchor_message_id"]))
                if "anchor_message_id" in values
                else None
            ),
            recent_window_seconds=values.get("recent_window_seconds"),
        )
    except (ContractViolation, ValueError, TypeError):
        confirmation = HASH_C
    return {
        "confirmation_sha256": confirmation,
        "contract_version": DELETION_REQUEST_CONTRACT_VERSION,
        "data_domain": CONVERSATIONAL_ERASURE_DOMAIN,
        "operation_id": str(OPERATION),
        "selector_kind": selector_kind,
        **values,
    }


async def asgi_request(
    app: FastAPI,
    *,
    json_body: Mapping[str, object] | None = None,
    method: str = "POST",
    target: str = "/memory/conversations/erasure-requests",
) -> tuple[int, dict[str, str], Any]:
    parsed = urlsplit(target)
    raw_body = (
        json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        if json_body is not None
        else b""
    )
    request_headers = {
        "host": "testserver",
        "content-type": "application/json",
        "content-length": str(len(raw_body)),
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
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
    start = next(
        item for item in messages if item["type"] == "http.response.start"
    )
    response_body = b"".join(
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
        json.loads(response_body),
    )


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[Request, tuple[ActorScope, ...]]] = []

    async def __call__(
        self,
        request: Request,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        self.calls.append((request, scopes))
        return owner_actor()


class FakeRequester:
    def __init__(
        self,
        *,
        state: ConversationErasureState = ConversationErasureState.FENCED,
        exception: Exception | None = None,
    ) -> None:
        self.state = state
        self.exception = exception
        self.calls: list[BoundConversationDeletion] = []
        self.read_calls: list[tuple[DeletionAuthority, UUID]] = []

    async def request_erasure(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus:
        self.calls.append(command)
        if self.exception is not None:
            raise self.exception
        governed_deleted = self.state in {
            ConversationErasureState.GOVERNED_DELETED,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        }
        conversation_deleted = self.state in {
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        }
        return ConversationErasureStatus(
            owner_user_id=command.authority.owner_user_id,
            operation_id=command.operation_id,
            selector_kind=command.request.selector_kind,
            state=self.state,
            target_count=2,
            selector_sha256=HASH_A,
            target_manifest_sha256=HASH_B,
            governed_receipt_sha256=HASH_C if governed_deleted else None,
            last_error_code=None,
            created_at=NOW,
            completed_at=NOW if conversation_deleted else None,
        )

    async def read_erasure_status(
        self,
        authority: DeletionAuthority,
        operation_id: UUID,
    ) -> ConversationErasureStatus | None:
        self.read_calls.append((authority, operation_id))
        if self.exception is not None:
            raise self.exception
        if operation_id != OPERATION:
            return None
        governed_deleted = self.state in {
            ConversationErasureState.GOVERNED_DELETED,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        }
        conversation_deleted = self.state in {
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        }
        return ConversationErasureStatus(
            owner_user_id=authority.owner_user_id,
            operation_id=operation_id,
            selector_kind=DeletionSelectorKind.ALL_CONVERSATIONS,
            state=self.state,
            target_count=2,
            selector_sha256=HASH_A,
            target_manifest_sha256=HASH_B,
            governed_receipt_sha256=HASH_C if governed_deleted else None,
            last_error_code=(
                "coordinator_attempts_exhausted"
                if self.state is ConversationErasureState.MANUAL_REVIEW
                else None
            ),
            created_at=NOW,
            completed_at=NOW if conversation_deleted else None,
        )


def app_for(
    resolver: FakeResolver | None,
    requester: FakeRequester | None,
    *,
    feature_enabled: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_conversation_erasure_router(
            actor_resolver=resolver,
            requester=requester,
            feature_enabled=feature_enabled,
        )
    )
    return app


class ConversationErasureHttpTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_owner_manifest_hash_binds_all_eleven_routes(self) -> None:
        self.assertEqual(len(CLAIM_OWNER_ROUTE_SPECIFICATIONS), 9)
        self.assertEqual(
            OWNER_ROUTE_SPECIFICATIONS,
            (
                *CLAIM_OWNER_ROUTE_SPECIFICATIONS,
                CONVERSATION_ERASURE_ROUTE_SPECIFICATION,
                CONVERSATION_ERASURE_STATUS_ROUTE_SPECIFICATION,
            ),
        )

        def manifest_hash(specifications: tuple[object, ...]) -> str:
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
                    for route in specifications
                ),
            )

        self.assertEqual(len(OWNER_ROUTE_SPECIFICATIONS), 11)
        self.assertEqual(
            route_manifest_sha256(),
            manifest_hash(OWNER_ROUTE_SPECIFICATIONS),
        )
        self.assertNotEqual(
            route_manifest_sha256(),
            manifest_hash(CLAIM_OWNER_ROUTE_SPECIFICATIONS),
        )

    async def test_all_four_selectors_bind_verified_owner_and_exact_scope(
        self,
    ) -> None:
        cases = (
            request_body("thread", thread_id=str(THREAD)),
            request_body(
                "message_tail",
                thread_id=str(THREAD),
                anchor_message_id=str(MESSAGE),
            ),
            request_body("recent", recent_window_seconds=3600),
            request_body("all_conversations"),
        )
        for body in cases:
            with self.subTest(selector_kind=body["selector_kind"]):
                resolver = FakeResolver()
                requester = FakeRequester()
                status, headers, response = await asgi_request(
                    app_for(resolver, requester),
                    json_body=body,
                )
                self.assertEqual(status, 202)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["retry-after"], "2")
                self.assertEqual(
                    headers["location"],
                    "/memory/conversations/erasure-requests/" + str(OPERATION),
                )
                self.assertEqual(len(resolver.calls), 1)
                self.assertEqual(
                    resolver.calls[0][1],
                    (ActorScope.ERASE_CONVERSATIONS,),
                )
                self.assertEqual(len(requester.calls), 1)
                command = requester.calls[0]
                self.assertEqual(command.authority.owner_user_id, OWNER)
                self.assertEqual(
                    command.request.selector_kind.value,
                    body["selector_kind"],
                )
                self.assertNotIn("owner_user_id", response)
                self.assertEqual(
                    set(response),
                    {
                        "operation_id",
                        "selector_kind",
                        "state",
                        "target_count",
                        "selector_sha256",
                        "target_manifest_sha256",
                        "governed_receipt_sha256",
                        "last_error_code",
                        "created_at",
                        "completed_at",
                    },
                )

    async def test_completed_idempotent_replay_returns_200(self) -> None:
        requester = FakeRequester(state=ConversationErasureState.COMPLETED)
        status, _, response = await asgi_request(
            app_for(FakeResolver(), requester),
            json_body=request_body("all_conversations"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["state"], "completed")
        self.assertEqual(response["governed_receipt_sha256"], HASH_C)
        self.assertEqual(response["completed_at"], NOW.isoformat())

    async def test_active_states_return_202_and_manual_review_returns_409(self) -> None:
        nonterminal_states = tuple(
            state
            for state in ConversationErasureState
            if state not in {
                ConversationErasureState.COMPLETED,
                ConversationErasureState.MANUAL_REVIEW,
            }
        )
        self.assertEqual(len(nonterminal_states), 5)
        for state in nonterminal_states:
            with self.subTest(state=state.value):
                status, _, response = await asgi_request(
                    app_for(FakeResolver(), FakeRequester(state=state)),
                    json_body=request_body("all_conversations"),
                )
                self.assertEqual(status, 202)
                self.assertEqual(response["state"], state.value)
        status, headers, response = await asgi_request(
            app_for(
                FakeResolver(),
                FakeRequester(state=ConversationErasureState.MANUAL_REVIEW),
            ),
            json_body=request_body("all_conversations"),
        )
        self.assertEqual(status, 409)
        self.assertNotIn("retry-after", headers)
        self.assertEqual(response["state"], "manual_review")

    async def test_get_status_is_owner_scoped_retryable_and_content_free(self) -> None:
        resolver = FakeResolver()
        requester = FakeRequester(state=ConversationErasureState.RETRYABLE)
        status, headers, response = await asgi_request(
            app_for(resolver, requester),
            method="GET",
            target=(
                "/memory/conversations/erasure-requests/" + str(OPERATION)
            ),
        )
        self.assertEqual(status, 202)
        self.assertEqual(headers["retry-after"], "2")
        self.assertEqual(response["operation_id"], str(OPERATION))
        self.assertEqual(len(requester.read_calls), 1)
        authority, operation_id = requester.read_calls[0]
        self.assertEqual(authority.owner_user_id, OWNER)
        self.assertEqual(operation_id, OPERATION)
        self.assertEqual(
            resolver.calls[0][1], (ActorScope.ERASE_CONVERSATIONS,)
        )

    async def test_get_status_unknown_is_404_and_invalid_uuid_is_400(self) -> None:
        requester = FakeRequester()
        unknown = UUID("99999999-9999-4999-8999-999999999999")
        status, _, response = await asgi_request(
            app_for(FakeResolver(), requester),
            method="GET",
            target=f"/memory/conversations/erasure-requests/{unknown}",
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            response, {"error": {"code": "memory_resource_not_found"}}
        )
        resolver = FakeResolver()
        status, _, response = await asgi_request(
            app_for(resolver, requester),
            method="GET",
            target="/memory/conversations/erasure-requests/not-a-uuid",
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            response, {"error": {"code": "memory_request_invalid"}}
        )
        self.assertEqual(resolver.calls, [])

    async def test_exact_repository_failures_are_typed(self) -> None:
        cases = (
            (
                DeletionRepositoryFailure.REQUEST_INVALID,
                400,
                "memory_request_invalid",
            ),
            (
                DeletionRepositoryFailure.LEGACY_PROJECT_THREAD_DEPENDENCY,
                409,
                "governed_project_thread_erasure_required",
            ),
            (
                DeletionRepositoryFailure.REPLAY_CONFLICT,
                409,
                "conversation_erasure_replay_conflict",
            ),
            (
                DeletionRepositoryFailure.ALREADY_ACTIVE,
                409,
                "conversation_erasure_already_active",
            ),
            (
                DeletionRepositoryFailure.SELECTOR_NOT_FOUND,
                409,
                "conversation_erasure_selector_not_found",
            ),
            (
                DeletionRepositoryFailure.SOURCE_PRECONDITION_FAILED,
                409,
                "conversation_erasure_source_precondition_failed",
            ),
            (
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE,
                503,
                "conversation_unavailable",
            ),
        )
        for failure, expected_status, expected_code in cases:
            with self.subTest(failure=failure.value):
                requester = FakeRequester(
                    exception=DeletionRepositoryError(failure)
                )
                status, _, response = await asgi_request(
                    app_for(FakeResolver(), requester),
                    json_body=request_body("all_conversations"),
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(response, {"error": {"code": expected_code}})

    async def test_unexpected_requester_failure_is_content_free(self) -> None:
        failures = (
            RuntimeError("sensitive source database detail"),
            DeletionRepositoryError(
                DeletionRepositoryFailure.OPERATION_OUTCOME_UNKNOWN
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                requester = FakeRequester(exception=failure)
                status, _, response = await asgi_request(
                    app_for(FakeResolver(), requester),
                    json_body=request_body("all_conversations"),
                )
                self.assertEqual(status, 503)
                self.assertEqual(
                    response,
                    {"error": {"code": "memory_operation_outcome_unknown"}},
                )
                self.assertNotIn("sensitive", json.dumps(response))

    async def test_owner_time_and_structured_fields_fail_before_requester(
        self,
    ) -> None:
        cases = (
            {**request_body("all_conversations"), "owner_user_id": str(OWNER)},
            {**request_body("all_conversations"), "user_id": str(OWNER)},
            {**request_body("all_conversations"), "requested_at": NOW.isoformat()},
            {**request_body("all_conversations"), "cutoff_at": NOW.isoformat()},
            {**request_body("all_conversations"), "library_id": str(THREAD)},
            {**request_body("all_conversations"), "account_id": str(OWNER)},
            {
                **request_body("all_conversations"),
                "data_domain": "weightlifting_sessions",
            },
            {
                **request_body("all_conversations"),
                "data_domain": "daily_food_logs",
            },
        )
        for body in cases:
            with self.subTest(body=body):
                resolver = FakeResolver()
                requester = FakeRequester()
                status, _, response = await asgi_request(
                    app_for(resolver, requester),
                    json_body=body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    response,
                    {"error": {"code": "memory_request_invalid"}},
                )
                self.assertEqual(resolver.calls, [])
                self.assertEqual(requester.calls, [])

    async def test_inactive_and_unconfigured_modes_fail_closed(self) -> None:
        body = request_body("all_conversations")
        disabled_status, _, disabled = await asgi_request(
            app_for(None, None, feature_enabled=False),
            json_body=body,
        )
        self.assertEqual(disabled_status, 503)
        self.assertEqual(
            disabled,
            {"error": {"code": "memory_successor_disabled"}},
        )
        unconfigured_status, _, unconfigured = await asgi_request(
            app_for(None, None),
            json_body=body,
        )
        self.assertEqual(unconfigured_status, 503)
        self.assertEqual(
            unconfigured,
            {"error": {"code": "memory_successor_unconfigured"}},
        )


if __name__ == "__main__":
    unittest.main()
