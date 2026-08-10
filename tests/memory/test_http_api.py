from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Request

from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.http_api import (
    MAX_REQUEST_BODY_BYTES,
    MemoryHttpError,
    MemoryHttpFailure,
    create_owner_memory_app,
    create_owner_memory_router,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER_OWNER = UUID("22222222-2222-4222-8222-222222222222")
CLAIM = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROPOSAL = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPERATION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def owner_actor(
    *,
    scopes: tuple[ActorScope, ...] = (
        ActorScope.MUTATE_CLAIMS,
        ActorScope.READ_CLAIMS,
        ActorScope.REVIEW_PROPOSALS,
    ),
) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OWNER,
        session_id=OWNER,
        role=ActorRole.OWNER,
        scopes=scopes,
        authentication_manifest_sha256=HASH_A,
        authenticated_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    )


def worker_actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OTHER_OWNER,
        session_id=OWNER,
        role=ActorRole.WORKER,
        scopes=(ActorScope.READ_CLAIMS,),
        authentication_manifest_sha256=HASH_A,
        authenticated_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    )


def review_body() -> dict[str, Any]:
    return {
        "decision": "admit",
        "expected_predicate_catalog_sha256": HASH_A,
        "expected_proposal_sha256": HASH_B,
        "expected_selected_sha256": HASH_C,
        "expected_selection_binding_sha256": HASH_D,
        "expected_source_sha256": HASH_E,
        "operation_id": str(OPERATION),
        "reason_codes": ["explicit_owner_review"],
    }


def correction_body() -> dict[str, Any]:
    return {
        "expected_predicate_catalog_sha256": HASH_A,
        "expected_revision_sha256": HASH_B,
        "expected_state_sha256": HASH_C,
        "operation_id": str(OPERATION),
        "replacement": {
            "epistemic_state": "supported",
            "object_display_name": None,
            "object_entity_type": None,
            "object_kind": "literal",
            "object_literal": "synthetic replacement",
            "sensitivity": "ordinary",
        },
    }


def transition_body() -> dict[str, Any]:
    return {
        "expected_revision_sha256": HASH_A,
        "expected_state_sha256": HASH_B,
        "operation_id": str(OPERATION),
    }


def status_result() -> dict[str, Any]:
    return {
        "active_claims": 1,
        "pending_proposals": 2,
        "pending_projection": 0,
        "failed_projection": 0,
        "last_transition_at": NOW,
    }


def claim_result(claim_id: UUID = CLAIM) -> dict[str, Any]:
    return {
        "claim_id": str(claim_id),
        "lifecycle_state": "active",
        "revision_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "revision_number": 1,
        "revision_sha256": HASH_A,
        "current_state_sha256": HASH_B,
        "revision_fact_policy_sha256": HASH_C,
        "predicate_catalog_sha256": HASH_D,
        "selected_sha256": HASH_E,
        "selection_binding_sha256": HASH_A,
        "object_kind": "literal",
        "predicate": "preference.personal",
        "epistemic_state": "supported",
        "sensitivity": "ordinary",
        "updated_at": NOW,
    }


def claim_detail_result(claim_id: UUID = CLAIM) -> dict[str, Any]:
    return {
        **claim_result(claim_id),
        "subject_entity_type": "self",
        "subject_entity_key": "self",
        "subject_display_name": None,
        "object_entity_type": None,
        "object_entity_key": None,
        "object_display_name": None,
        "object_literal": "synthetic literal",
    }


def proposal_result() -> dict[str, Any]:
    return {
        "proposal_id": str(PROPOSAL),
        "operation_id": str(OPERATION),
        "proposal_sha256": HASH_A,
        "source_sha256": HASH_B,
        "selected_sha256": HASH_C,
        "selection_binding_sha256": HASH_D,
        "predicate_catalog_sha256": HASH_E,
        "source_excerpt": "Synthetic source excerpt.",
        "subject_entity_type": "self",
        "subject_entity_key": "self",
        "subject_display_name": None,
        "predicate": "preference.personal",
        "object_kind": "literal",
        "object_entity_type": None,
        "object_entity_key": None,
        "object_display_name": None,
        "object_literal": "synthetic literal",
        "epistemic_state": "supported",
        "sensitivity": "ordinary",
        "projectable": True,
        "domains": ["personal"],
        "intents": ["recall"],
        "surface": "normal",
        "requires_explicit": False,
        "valid_from": None,
        "valid_to": None,
        "correction_of_claim_id": None,
        "expires_at": NOW + timedelta(days=1),
        "created_at": NOW,
    }


def review_result(decision: str) -> dict[str, Any]:
    if decision == "admit":
        return {
            "outcome": "admitted",
            "claim_id": str(CLAIM),
            "revision_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "outbox_id": str(OPERATION),
        }
    return {
        "outcome": "rejected",
        "claim_id": None,
        "revision_id": None,
        "outbox_id": None,
    }


def operation_result(operation_id: UUID = OPERATION) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "events": [
            {
                "operation_id": str(operation_id),
                "transition_code": "claim_retracted",
                "object_type": "claim",
                "object_id": str(CLAIM),
                "reason_code": "explicit_owner_retraction",
                "new_state_sha256": HASH_A,
                "created_at": NOW,
            }
        ],
    }


_UNSET = object()


async def asgi_request(
    app: FastAPI,
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = _UNSET,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], Any]:
    parsed = urlsplit(target)
    if raw_body is not None and json_body is not _UNSET:
        raise AssertionError("choose one request body form")
    if raw_body is not None:
        body = raw_body
    elif json_body is not _UNSET:
        body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
    else:
        body = b""
    request_headers = {"host": "testserver", **dict(headers or {})}
    if json_body is not _UNSET:
        request_headers.setdefault("content-type", "application/json")
    if body:
        request_headers["content-length"] = str(len(body))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
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
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    decoded = json.loads(response_body) if response_body else None
    return int(start["status"]), response_headers, decoded


class FakeResolver:
    def __init__(
        self,
        *,
        actor: object = _UNSET,
        exception: Exception | None = None,
    ) -> None:
        self.actor = owner_actor() if actor is _UNSET else actor
        self.exception = exception
        self.calls: list[tuple[Request, tuple[ActorScope, ...]]] = []

    async def __call__(
        self, request: Request, scopes: tuple[ActorScope, ...]
    ) -> VerifiedActor:
        self.calls.append((request, scopes))
        if self.exception is not None:
            raise self.exception
        return self.actor  # type: ignore[return-value]


class FakeFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failures: dict[str, Exception] = {}
        self.missing: set[str] = set()
        self.results: dict[str, object] = {}

    def _result(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        if name in self.failures:
            raise self.failures[name]
        if name in self.missing:
            return None
        if name in self.results:
            return self.results[name]
        if name == "status":
            return status_result()
        if name == "list_claims":
            return [claim_result()]
        if name == "get_claim":
            return claim_detail_result(args[1])
        if name == "list_proposals":
            return [proposal_result()]
        if name == "review_proposal":
            return review_result(args[2]["decision"])
        if name == "correct_claim":
            return {
                "outcome": "correction_pending",
                "proposal_id": str(PROPOSAL),
                "proposal_sha256": HASH_A,
                "review_operation_id": str(OPERATION),
            }
        if name == "retract_claim":
            return {"outcome": "retracted", "outbox_id": str(PROPOSAL)}
        if name == "delete_claim":
            return {
                "outcome": "deletion_pending",
                "outbox_id": str(PROPOSAL),
            }
        if name == "get_operation":
            return operation_result(args[1])
        raise AssertionError("unexpected_fake_facade_operation")

    async def status(self, actor: VerifiedActor) -> Mapping[str, Any]:
        return self._result("status", actor)

    async def list_claims(self, actor: VerifiedActor) -> list[Mapping[str, Any]]:
        return self._result("list_claims", actor)

    async def get_claim(
        self, actor: VerifiedActor, claim_id: UUID
    ) -> Mapping[str, Any] | None:
        return self._result("get_claim", actor, claim_id)

    async def list_proposals(
        self, actor: VerifiedActor
    ) -> list[Mapping[str, Any]]:
        return self._result("list_proposals", actor)

    async def review_proposal(
        self, actor: VerifiedActor, proposal_id: UUID, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._result("review_proposal", actor, proposal_id, body)

    async def correct_claim(
        self, actor: VerifiedActor, claim_id: UUID, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._result("correct_claim", actor, claim_id, body)

    async def retract_claim(
        self, actor: VerifiedActor, claim_id: UUID, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._result("retract_claim", actor, claim_id, body)

    async def delete_claim(
        self, actor: VerifiedActor, claim_id: UUID, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._result("delete_claim", actor, claim_id, body)

    async def get_operation(
        self, actor: VerifiedActor, operation_id: UUID
    ) -> Mapping[str, Any] | None:
        return self._result("get_operation", actor, operation_id)


class AuthCodeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("sensitive authentication detail")


class DatabaseError(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        super().__init__("sensitive database content")


class OwnerStoreCodeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("sensitive owner-store content")


class OwnerMemoryHttpApiTests(unittest.IsolatedAsyncioTestCase):
    def test_router_is_exactly_the_pure_route_manifest(self) -> None:
        router = create_owner_memory_router()
        actual = tuple(
            (next(iter(route.methods)), route.path, route.name)
            for route in router.routes
        )
        expected = tuple(
            (spec.method.value, spec.path, spec.operation)
            for spec in OWNER_ROUTE_SPECIFICATIONS
        )
        self.assertEqual(actual, expected)
        self.assertFalse(any("owner" in route.path for route in router.routes))

    async def test_feature_gate_defaults_disabled_and_non_bool_true_stays_disabled(
        self,
    ) -> None:
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                app = create_owner_memory_app(feature_enabled=value)  # type: ignore[arg-type]
                status, headers, body = await asgi_request(app, "GET", "/memory/status")
                self.assertEqual(status, 503)
                self.assertEqual(
                    body, {"error": {"code": "memory_successor_disabled"}}
                )
                self.assertEqual(headers["cache-control"], "no-store")

    async def test_enabled_route_without_both_dependencies_fails_closed(self) -> None:
        status, _, body = await asgi_request(
            create_owner_memory_app(feature_enabled=True),
            "GET",
            "/memory/status",
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            body, {"error": {"code": "memory_successor_unconfigured"}}
        )

    async def test_all_routes_call_exact_facade_method_and_scope(self) -> None:
        resolver = FakeResolver()
        facade = FakeFacade()
        app = create_owner_memory_app(
            actor_resolver=resolver, facade=facade, feature_enabled=True
        )
        cases = (
            ("GET", "/memory/status", "status", ActorScope.READ_CLAIMS, _UNSET),
            ("GET", "/memory/claims", "list_claims", ActorScope.READ_CLAIMS, _UNSET),
            (
                "GET",
                f"/memory/claims/{CLAIM}",
                "get_claim",
                ActorScope.READ_CLAIMS,
                _UNSET,
            ),
            (
                "GET",
                "/memory/proposals",
                "list_proposals",
                ActorScope.REVIEW_PROPOSALS,
                _UNSET,
            ),
            (
                "POST",
                f"/memory/proposals/{PROPOSAL}/review",
                "review_proposal",
                ActorScope.REVIEW_PROPOSALS,
                review_body(),
            ),
            (
                "POST",
                f"/memory/claims/{CLAIM}/correct",
                "correct_claim",
                ActorScope.MUTATE_CLAIMS,
                correction_body(),
            ),
            (
                "POST",
                f"/memory/claims/{CLAIM}/retract",
                "retract_claim",
                ActorScope.MUTATE_CLAIMS,
                transition_body(),
            ),
            (
                "DELETE",
                f"/memory/claims/{CLAIM}",
                "delete_claim",
                ActorScope.MUTATE_CLAIMS,
                transition_body(),
            ),
            (
                "GET",
                f"/memory/operations/{OPERATION}",
                "get_operation",
                ActorScope.READ_CLAIMS,
                _UNSET,
            ),
        )
        for method, target, operation, expected_scope, body_value in cases:
            with self.subTest(operation=operation):
                kwargs = {} if body_value is _UNSET else {"json_body": body_value}
                status, headers, body = await asgi_request(
                    app,
                    method,
                    target,
                    headers={"authorization": "Bearer synthetic"},
                    **kwargs,
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertIsInstance(body, (dict, list))
                self.assertEqual(facade.calls[-1][0], operation)
                self.assertIs(facade.calls[-1][1][0], resolver.actor)
                self.assertEqual(resolver.calls[-1][1], (expected_scope,))

        self.assertEqual(facade.calls[2][1][1], CLAIM)
        self.assertEqual(facade.calls[4][1][1], PROPOSAL)
        self.assertEqual(facade.calls[5][1][1], CLAIM)
        self.assertEqual(facade.calls[8][1][1], OPERATION)
        self.assertEqual(dict(facade.calls[4][1][2]), review_body())

    async def test_owner_actor_authority_inputs_are_rejected_not_forwarded(self) -> None:
        cases = (
            (
                "GET",
                "/memory/status",
                {"headers": {"x-vs-actor-user-id": str(OTHER_OWNER)}},
                403,
                "memory_authorization_denied",
            ),
            (
                "GET",
                f"/memory/claims/{CLAIM}?owner_user_id={OTHER_OWNER}",
                {},
                400,
                "memory_request_invalid",
            ),
            (
                "POST",
                f"/memory/proposals/{PROPOSAL}/review",
                {"json_body": {**review_body(), "owner_user_id": str(OTHER_OWNER)}},
                400,
                "memory_request_invalid",
            ),
            (
                "POST",
                f"/memory/claims/{CLAIM}/correct",
                {
                    "json_body": {
                        **correction_body(),
                        "replacement": {
                            **correction_body()["replacement"],
                            "actor_user_id": str(OTHER_OWNER),
                        },
                    }
                },
                400,
                "memory_request_invalid",
            ),
        )
        for method, target, kwargs, expected_status, expected_code in cases:
            with self.subTest(target=target):
                resolver = FakeResolver()
                facade = FakeFacade()
                app = create_owner_memory_app(
                    actor_resolver=resolver,
                    facade=facade,
                    feature_enabled=True,
                )
                status, _, body = await asgi_request(app, method, target, **kwargs)
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"error": {"code": expected_code}})
                self.assertEqual(facade.calls, [])

        resolver = FakeResolver()
        facade = FakeFacade()
        app = create_owner_memory_app(
            actor_resolver=resolver, facade=facade, feature_enabled=True
        )
        status, _, body = await asgi_request(
            app, "GET", f"/memory/{OTHER_OWNER}/claims"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": {"code": "memory_route_not_found"}})
        self.assertEqual(resolver.calls, [])

    async def test_claim_list_is_content_free_and_detail_shape_is_exact(self) -> None:
        app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=FakeFacade(),
            feature_enabled=True,
        )
        list_status, _, claims = await asgi_request(
            app,
            "GET",
            "/memory/claims",
        )
        detail_status, _, detail = await asgi_request(
            app,
            "GET",
            f"/memory/claims/{CLAIM}",
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(detail_status, 200)
        fact_fields = {
            "subject_entity_type",
            "subject_entity_key",
            "subject_display_name",
            "object_entity_type",
            "object_entity_key",
            "object_display_name",
            "object_literal",
        }
        self.assertTrue(fact_fields.isdisjoint(claims[0]))
        self.assertEqual(set(detail), set(claim_result()) | fact_fields)
        self.assertEqual(detail["subject_entity_key"], "self")
        self.assertEqual(detail["object_literal"], "synthetic literal")

        entity_detail = claim_detail_result()
        entity_detail.update(
            {
                "object_kind": "entity",
                "object_entity_type": "person",
                "object_entity_key": "person:synthetic",
                "object_display_name": "Synthetic Person",
                "object_literal": None,
            }
        )
        entity_facade = FakeFacade()
        entity_facade.results["get_claim"] = entity_detail
        entity_app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=entity_facade,
            feature_enabled=True,
        )
        entity_status, _, entity_body = await asgi_request(
            entity_app,
            "GET",
            f"/memory/claims/{CLAIM}",
        )
        self.assertEqual(entity_status, 200)
        self.assertEqual(entity_body["object_entity_key"], "person:synthetic")

        leaking_facade = FakeFacade()
        leaking_facade.results["list_claims"] = [claim_detail_result()]
        leaking_app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=leaking_facade,
            feature_enabled=True,
        )
        leaking_status, _, leaking_body = await asgi_request(
            leaking_app,
            "GET",
            "/memory/claims",
        )
        self.assertEqual(leaking_status, 500)
        self.assertEqual(
            leaking_body,
            {"error": {"code": "memory_internal_error"}},
        )

    async def test_claim_detail_conditional_shape_fails_closed(self) -> None:
        invalid_values: list[dict[str, Any]] = []
        literal_with_entity = claim_detail_result()
        literal_with_entity["object_entity_type"] = "person"
        invalid_values.append(literal_with_entity)
        self_with_display = claim_detail_result()
        self_with_display["subject_display_name"] = "Synthetic owner"
        invalid_values.append(self_with_display)
        missing_literal = claim_detail_result()
        missing_literal["object_literal"] = None
        invalid_values.append(missing_literal)
        extra_field = claim_detail_result()
        extra_field["source_excerpt"] = "must not escape detail contract"
        invalid_values.append(extra_field)

        for value in invalid_values:
            facade = FakeFacade()
            facade.results["get_claim"] = value
            app = create_owner_memory_app(
                actor_resolver=FakeResolver(),
                facade=facade,
                feature_enabled=True,
            )
            status, _, body = await asgi_request(
                app,
                "GET",
                f"/memory/claims/{CLAIM}",
            )
            self.assertEqual(status, 500)
            self.assertEqual(body, {"error": {"code": "memory_internal_error"}})

    async def test_mutation_body_parser_is_closed_and_duplicate_safe(self) -> None:
        valid = review_body()
        duplicate = (
            b'{"decision":"admit","decision":"reject",'
            + json.dumps({key: value for key, value in valid.items() if key != "decision"})[
                1:
            ].encode("utf-8")
        )
        cases = (
            ({}, "missing_body"),
            ({"json_body": []}, "non_object"),
            (
                {"raw_body": b"{", "headers": {"content-type": "application/json"}},
                "malformed",
            ),
            (
                {"raw_body": duplicate, "headers": {"content-type": "application/json"}},
                "duplicate",
            ),
            (
                {
                    "raw_body": b'{"value":' + b"1" * 5000 + b"}",
                    "headers": {"content-type": "application/json"},
                },
                "integer_conversion_limit",
            ),
            (
                {"json_body": {**valid, "created_at": "2030-01-01T00:00:00Z"}},
                "caller_time",
            ),
            (
                {
                    "raw_body": json.dumps(valid).encode("utf-8"),
                    "headers": {"content-type": "text/plain"},
                },
                "wrong_media_type",
            ),
            (
                {
                    "raw_body": b"{" + b" " * MAX_REQUEST_BODY_BYTES + b"}",
                    "headers": {"content-type": "application/json"},
                },
                "oversized",
            ),
            (
                {
                    "raw_body": (b"[" * 1100) + (b"]" * 1100),
                    "headers": {"content-type": "application/json"},
                },
                "excessive_nesting",
            ),
        )
        for kwargs, label in cases:
            with self.subTest(label=label):
                resolver = FakeResolver()
                facade = FakeFacade()
                app = create_owner_memory_app(
                    actor_resolver=resolver,
                    facade=facade,
                    feature_enabled=True,
                )
                status, _, body = await asgi_request(
                    app,
                    "POST",
                    f"/memory/proposals/{PROPOSAL}/review",
                    **kwargs,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    body, {"error": {"code": "memory_request_invalid"}}
                )
                self.assertEqual(facade.calls, [])
                self.assertEqual(resolver.calls, [])

    async def test_read_route_accepts_only_absent_or_empty_object_body(self) -> None:
        resolver = FakeResolver()
        facade = FakeFacade()
        app = create_owner_memory_app(
            actor_resolver=resolver, facade=facade, feature_enabled=True
        )
        status, _, _ = await asgi_request(
            app, "GET", "/memory/status", json_body={}
        )
        self.assertEqual(status, 200)
        status, _, body = await asgi_request(
            app, "GET", "/memory/status", json_body={"value": 1}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": {"code": "memory_request_invalid"}})

    async def test_resource_identifiers_require_canonical_uuid_text(self) -> None:
        resolver = FakeResolver()
        facade = FakeFacade()
        app = create_owner_memory_app(
            actor_resolver=resolver, facade=facade, feature_enabled=True
        )
        for value in ("not-a-uuid", str(CLAIM).upper(), CLAIM.hex):
            with self.subTest(value=value):
                status, _, body = await asgi_request(
                    app, "GET", f"/memory/claims/{value}"
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    body, {"error": {"code": "memory_request_invalid"}}
                )
        self.assertEqual(facade.calls, [])
        self.assertEqual(resolver.calls, [])

    async def test_authority_resolution_is_the_last_step_before_store(self) -> None:
        events: list[str] = []

        class OrderedResolver(FakeResolver):
            async def __call__(
                self,
                request: Request,
                scopes: tuple[ActorScope, ...],
            ) -> VerifiedActor:
                events.append("authority")
                return await super().__call__(request, scopes)

        class OrderedFacade(FakeFacade):
            async def get_claim(
                self,
                actor: VerifiedActor,
                claim_id: UUID,
            ) -> Mapping[str, Any] | None:
                events.append("store")
                return await super().get_claim(actor, claim_id)

        app = create_owner_memory_app(
            actor_resolver=OrderedResolver(),
            facade=OrderedFacade(),
            feature_enabled=True,
        )
        status, _, _ = await asgi_request(
            app,
            "GET",
            f"/memory/claims/{CLAIM}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(events, ["authority", "store"])

    async def test_resolver_error_mapping_and_actor_defense_in_depth(self) -> None:
        cases = (
            (AuthCodeError("auth_header_missing"), None, 401, "memory_authentication_required"),
            (AuthCodeError("auth_role_denied"), None, 403, "memory_authorization_denied"),
            (
                AuthCodeError("auth_key_resolution_unavailable"),
                None,
                503,
                "memory_successor_unavailable",
            ),
            (
                AuthCodeError("auth_key_resolver_failed"),
                None,
                503,
                "memory_successor_unavailable",
            ),
            (AuthCodeError("unexpected_auth_bug"), None, 500, "memory_internal_error"),
            (None, object(), 401, "memory_authentication_required"),
            (None, worker_actor(), 403, "memory_authorization_denied"),
            (
                None,
                owner_actor(scopes=(ActorScope.MUTATE_CLAIMS,)),
                403,
                "memory_authorization_denied",
            ),
        )
        for exception, actor, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code, actor=type(actor).__name__):
                resolver = FakeResolver(
                    actor=actor if actor is not None else _UNSET,
                    exception=exception,
                )
                facade = FakeFacade()
                app = create_owner_memory_app(
                    actor_resolver=resolver,
                    facade=facade,
                    feature_enabled=True,
                )
                status, headers, body = await asgi_request(
                    app, "GET", "/memory/status"
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"error": {"code": expected_code}})
                self.assertNotIn("sensitive", json.dumps(body))
                if status == 401:
                    self.assertEqual(headers["www-authenticate"], "Bearer")
                self.assertEqual(facade.calls, [])

    async def test_service_failures_map_without_database_or_content_leaks(self) -> None:
        cases = (
            (DatabaseError("40001"), 409, "memory_state_conflict"),
            (DatabaseError("23505"), 409, "memory_state_conflict"),
            (DatabaseError("23514"), 409, "memory_state_conflict"),
            (DatabaseError("P0002"), 404, "memory_resource_not_found"),
            (DatabaseError("22023"), 400, "memory_request_invalid"),
            (
                OwnerStoreCodeError("database_request_rejected"),
                400,
                "memory_request_invalid",
            ),
            (
                OwnerStoreCodeError("database_resource_not_found"),
                404,
                "memory_resource_not_found",
            ),
            (
                OwnerStoreCodeError("database_conflict"),
                409,
                "memory_state_conflict",
            ),
            (
                OwnerStoreCodeError("database_operation_rejected"),
                409,
                "memory_state_conflict",
            ),
            (
                OwnerStoreCodeError("database_authority_denied"),
                403,
                "memory_authorization_denied",
            ),
            (
                OwnerStoreCodeError("database_unavailable"),
                503,
                "memory_successor_unavailable",
            ),
            (ContractViolation("invalid_synthetic_request"), 400, "memory_request_invalid"),
            (TimeoutError("sensitive timeout content"), 503, "memory_successor_unavailable"),
            (RuntimeError("sensitive internal content"), 500, "memory_internal_error"),
            (
                MemoryHttpError(MemoryHttpFailure.RESOURCE_NOT_FOUND),
                404,
                "memory_resource_not_found",
            ),
        )
        for exception, expected_status, expected_code in cases:
            with self.subTest(exception=type(exception).__name__):
                resolver = FakeResolver()
                facade = FakeFacade()
                facade.failures["status"] = exception
                app = create_owner_memory_app(
                    actor_resolver=resolver,
                    facade=facade,
                    feature_enabled=True,
                )
                status, _, body = await asgi_request(app, "GET", "/memory/status")
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"error": {"code": expected_code}})
                self.assertNotIn("sensitive", json.dumps(body))

    async def test_all_success_contracts_fail_closed_on_backend_drift(self) -> None:
        invalid_claim = claim_detail_result(OTHER_OWNER)
        invalid_proposal = proposal_result()
        invalid_proposal["object_literal"] = {"unexpected": "json-object"}
        invalid_operation = operation_result()
        invalid_operation["events"][0]["operation_id"] = str(OTHER_OWNER)
        cases = (
            (
                "GET",
                "/memory/status",
                "status",
                {**status_result(), "unexpected": 1},
                _UNSET,
                500,
                "memory_internal_error",
            ),
            (
                "GET",
                "/memory/claims",
                "list_claims",
                (claim_result(),),
                _UNSET,
                500,
                "memory_internal_error",
            ),
            (
                "GET",
                f"/memory/claims/{CLAIM}",
                "get_claim",
                invalid_claim,
                _UNSET,
                500,
                "memory_internal_error",
            ),
            (
                "GET",
                "/memory/proposals",
                "list_proposals",
                [invalid_proposal],
                _UNSET,
                500,
                "memory_internal_error",
            ),
            (
                "POST",
                f"/memory/proposals/{PROPOSAL}/review",
                "review_proposal",
                {
                    "outcome": "admitted",
                    "claim_id": str(CLAIM),
                    "revision_id": str(PROPOSAL),
                },
                review_body(),
                503,
                "memory_operation_outcome_unknown",
            ),
            (
                "POST",
                f"/memory/claims/{CLAIM}/correct",
                "correct_claim",
                {
                    "outcome": "pending_review",
                    "proposal_id": str(PROPOSAL),
                    "proposal_sha256": HASH_A,
                    "review_operation_id": str(OPERATION),
                },
                correction_body(),
                503,
                "memory_operation_outcome_unknown",
            ),
            (
                "POST",
                f"/memory/claims/{CLAIM}/retract",
                "retract_claim",
                {"outcome": "requested", "outbox_id": str(PROPOSAL)},
                transition_body(),
                503,
                "memory_operation_outcome_unknown",
            ),
            (
                "DELETE",
                f"/memory/claims/{CLAIM}",
                "delete_claim",
                {
                    "outcome": "deletion_pending",
                    "outbox_id": str(PROPOSAL),
                    "unexpected": True,
                },
                transition_body(),
                503,
                "memory_operation_outcome_unknown",
            ),
            (
                "GET",
                f"/memory/operations/{OPERATION}",
                "get_operation",
                invalid_operation,
                _UNSET,
                500,
                "memory_internal_error",
            ),
        )
        for method, target, operation, result, body_value, expected_status, code in cases:
            with self.subTest(operation=operation):
                facade = FakeFacade()
                facade.results[operation] = result
                app = create_owner_memory_app(
                    actor_resolver=FakeResolver(),
                    facade=facade,
                    feature_enabled=True,
                )
                kwargs = {} if body_value is _UNSET else {"json_body": body_value}
                status, _, body = await asgi_request(
                    app,
                    method,
                    target,
                    **kwargs,
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"error": {"code": code}})
                self.assertNotIn("unexpected", json.dumps(body))

    async def test_review_receipt_nullability_fails_closed(self) -> None:
        reject_body = review_body()
        reject_body["decision"] = "reject"
        cases = (
            (
                review_body(),
                {
                    "outcome": "admitted",
                    "claim_id": str(CLAIM),
                    "revision_id": str(PROPOSAL),
                    "outbox_id": None,
                },
            ),
            (
                reject_body,
                {
                    "outcome": "rejected",
                    "claim_id": str(CLAIM),
                    "revision_id": None,
                    "outbox_id": None,
                },
            ),
        )
        for request_body, result in cases:
            with self.subTest(
                decision=request_body["decision"],
                outcome=result["outcome"],
            ):
                facade = FakeFacade()
                facade.results["review_proposal"] = result
                app = create_owner_memory_app(
                    actor_resolver=FakeResolver(),
                    facade=facade,
                    feature_enabled=True,
                )
                status, _, body = await asgi_request(
                    app,
                    "POST",
                    f"/memory/proposals/{PROPOSAL}/review",
                    json_body=request_body,
                )
                self.assertEqual(status, 503)
                self.assertEqual(
                    body,
                    {"error": {"code": "memory_operation_outcome_unknown"}},
                )

    async def test_mutation_nondeterministic_failures_are_outcome_unknown(self) -> None:
        failures = (
            TimeoutError("sensitive inner timeout"),
            ConnectionError("sensitive connection loss"),
            OSError("sensitive operating system failure"),
            OwnerStoreCodeError("database_unavailable"),
            RuntimeError("sensitive unexpected mutation failure"),
            DatabaseError("08006"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                facade = FakeFacade()
                facade.failures["delete_claim"] = failure
                app = create_owner_memory_app(
                    actor_resolver=FakeResolver(),
                    facade=facade,
                    feature_enabled=True,
                )
                status, _, body = await asgi_request(
                    app,
                    "DELETE",
                    f"/memory/claims/{CLAIM}",
                    json_body=transition_body(),
                )
                self.assertEqual(status, 503)
                self.assertEqual(
                    body,
                    {"error": {"code": "memory_operation_outcome_unknown"}},
                )
                self.assertNotIn("sensitive", json.dumps(body))

    async def test_success_encoding_failure_is_operation_aware(self) -> None:
        cases = (
            ("GET", "/memory/status", _UNSET, 500, "memory_internal_error"),
            (
                "DELETE",
                f"/memory/claims/{CLAIM}",
                transition_body(),
                503,
                "memory_operation_outcome_unknown",
            ),
        )
        for method, target, body_value, expected_status, code in cases:
            with self.subTest(method=method):
                app = create_owner_memory_app(
                    actor_resolver=FakeResolver(),
                    facade=FakeFacade(),
                    feature_enabled=True,
                )
                kwargs = {} if body_value is _UNSET else {"json_body": body_value}
                with patch(
                    "rag_engine.governed_memory.http_api.jsonable_encoder",
                    side_effect=RuntimeError("sensitive encoding failure"),
                ):
                    status, _, body = await asgi_request(
                        app,
                        method,
                        target,
                        **kwargs,
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"error": {"code": code}})

    async def test_missing_owner_resources_are_masked_as_not_found(self) -> None:
        resolver = FakeResolver()
        facade = FakeFacade()
        facade.missing.update({"get_claim", "get_operation"})
        app = create_owner_memory_app(
            actor_resolver=resolver, facade=facade, feature_enabled=True
        )
        for target in (
            f"/memory/claims/{CLAIM}",
            f"/memory/operations/{OPERATION}",
        ):
            with self.subTest(target=target):
                status, _, body = await asgi_request(app, "GET", target)
                self.assertEqual(status, 404)
                self.assertEqual(
                    body, {"error": {"code": "memory_resource_not_found"}}
                )

    async def test_wrong_method_and_unknown_route_have_closed_bodies(self) -> None:
        app = create_owner_memory_app(feature_enabled=True)
        status, _, body = await asgi_request(app, "POST", "/memory/status")
        self.assertEqual(status, 405)
        self.assertEqual(body, {"error": {"code": "memory_method_not_allowed"}})
        status, _, body = await asgi_request(app, "GET", "/memory/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": {"code": "memory_route_not_found"}})
        status, _, body = await asgi_request(app, "GET", "/memory/status/")
        self.assertEqual(status, 404)
        self.assertEqual(
            body, {"error": {"code": "memory_route_not_found"}}
        )


if __name__ == "__main__":
    unittest.main()
