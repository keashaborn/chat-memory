from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping
import unittest
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
        role=ActorRole.OWNER,
        scopes=scopes,
        authentication_manifest_sha256=HASH_A,
        authenticated_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    )


def worker_actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OTHER_OWNER,
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

    def _result(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        if name in self.failures:
            raise self.failures[name]
        if name in self.missing:
            return None
        if name in {"list_claims", "list_proposals"}:
            return [{"operation": name, "owner_user_id": str(args[0].owner_user_id)}]
        return {"operation": name, "owner_user_id": str(args[0].owner_user_id)}

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
                material = body[0] if isinstance(body, list) else body
                self.assertEqual(material["operation"], operation)
                self.assertEqual(material["owner_user_id"], str(OWNER))
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
