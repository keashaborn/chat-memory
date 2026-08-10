from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
import json
import unittest
from typing import Any, Mapping
from unittest.mock import patch
from uuid import UUID

from fastapi import Request

from rag_engine.governed_memory.auth import ActorScope, VerifiedActor
from rag_engine.governed_memory.http_api import create_owner_memory_app
from rag_engine.governed_memory.http_auth import HttpAuthError
from rag_engine.governed_memory.http_store import (
    OwnerStoreError,
    PostgresOwnerStore,
)
from rag_engine.governed_memory.runtime import live_supabase as live_supabase_runtime
from tests.memory.test_http_api import (
    CLAIM,
    OTHER_OWNER,
    PROPOSAL,
    FakeFacade,
    FakeResolver,
    asgi_request,
    owner_actor,
    review_body,
    transition_body,
    worker_actor,
)
from tests.memory.test_http_store import _Connection, _Pool


AUTHORIZATION = {"authorization": "Bearer a.b.c"}


def _live_config() -> live_supabase_runtime.LiveSupabaseAuthorityConfig:
    return live_supabase_runtime.LiveSupabaseAuthorityConfig(
        issuer="https://synthetic.supabase.co/auth/v1",
        api_key="synthetic-publishable-key",
    )


def _verifier(
    fetcher: live_supabase_runtime.UserFetcher,
) -> live_supabase_runtime.LiveSupabaseUserVerifier:
    return live_supabase_runtime.LiveSupabaseUserVerifier(
        _live_config(),
        fetcher=fetcher,
    )


def _authority_verifier(
    session_fetcher: live_supabase_runtime.SessionFetcher,
) -> live_supabase_runtime.LiveSupabaseAuthorityVerifier:
    config = _live_config()
    return live_supabase_runtime.LiveSupabaseAuthorityVerifier(
        live_supabase_runtime.LiveSupabaseUserVerifier(
            config,
            fetcher=_owner_body,
        ),
        live_supabase_runtime.LiveSupabaseSessionVerifier(
            config,
            fetcher=session_fetcher,
        ),
    )


def _owner_body(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    return json.dumps({"id": str(owner_actor().owner_user_id)}).encode("utf-8")


def _other_owner_body(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    return json.dumps({"id": str(OTHER_OWNER)}).encode("utf-8")


def _malformed_body(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    return b"{}"


def _unavailable(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    raise OSError("sensitive authority failure")


def _denied(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    raise live_supabase_runtime._LiveAuthorityDenied  # type: ignore[attr-defined]


def _session_body(
    _url: str,
    _authorization: str,
    _api_key: str,
    _timeout_seconds: int,
    _maximum_bytes: int,
) -> bytes:
    actor = owner_actor()
    return json.dumps(
        [
            {
                "owner_user_id": str(actor.owner_user_id),
                "session_id": str(actor.session_id),
                "session_present": True,
            }
        ]
    ).encode("utf-8")


def _session_absent(*args: object) -> bytes:
    row = json.loads(_session_body(*args).decode("utf-8"))[0]
    row["session_present"] = False
    return json.dumps([row]).encode("utf-8")


def _session_mismatch(*args: object) -> bytes:
    row = json.loads(_session_body(*args).decode("utf-8"))[0]
    row["session_id"] = str(OTHER_OWNER)
    return json.dumps([row]).encode("utf-8")


def _session_malformed(*_args: object) -> bytes:
    return b"[]"


class _FetchUserResponse(AbstractContextManager["_FetchUserResponse"]):
    status = 200
    headers = {
        "Content-Length": "2",
        "Content-Type": "application/json",
    }

    def __init__(self, url: str) -> None:
        self._url = url

    def __enter__(self) -> "_FetchUserResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, _maximum: int) -> bytes:
        return b"{}"


class _CapturingOpener:
    def __init__(self) -> None:
        self.request: object | None = None

    def open(self, request: object, *, timeout: int) -> _FetchUserResponse:
        self.request = request
        self.timeout = timeout
        return _FetchUserResponse(request.full_url)  # type: ignore[attr-defined]


def _live_resolver(
    verifier: Any,
    *,
    actor: VerifiedActor | None = None,
) -> Any:
    resolved_actor = actor or owner_actor()

    async def resolve(
        request: Request,
        _scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        await verifier(request, resolved_actor)
        return resolved_actor

    return resolve


class LiveAuthPublicMappingTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_public_mapping(
        self,
        resolver: Any,
        *,
        headers: Mapping[str, str] | None,
        expected_status: int,
        expected_code: str,
    ) -> None:
        facade = FakeFacade()
        app = create_owner_memory_app(
            actor_resolver=resolver,
            facade=facade,
            feature_enabled=True,
        )
        status, response_headers, body = await asgi_request(
            app,
            "GET",
            "/memory/status",
            headers=headers,
        )
        self.assertEqual(status, expected_status)
        self.assertEqual(body, {"error": {"code": expected_code}})
        self.assertNotIn("sensitive", json.dumps(body))
        if status == 401:
            self.assertEqual(response_headers["www-authenticate"], "Bearer")
        self.assertEqual(facade.calls, [])

    async def test_real_runtime_live_auth_codes_have_closed_public_mappings(
        self,
    ) -> None:
        runtime_cases = (
            (
                _live_resolver(_verifier(_owner_body)),
                None,
                401,
                "memory_authentication_required",
            ),
            (
                _live_resolver(_verifier(_denied)),
                AUTHORIZATION,
                401,
                "memory_authentication_required",
            ),
            (
                _live_resolver(_verifier(_other_owner_body)),
                AUTHORIZATION,
                401,
                "memory_authentication_required",
            ),
            (
                _live_resolver(_verifier(_owner_body), actor=worker_actor()),
                AUTHORIZATION,
                403,
                "memory_authorization_denied",
            ),
            (
                _live_resolver(_verifier(_unavailable)),
                AUTHORIZATION,
                503,
                "memory_successor_unavailable",
            ),
            (
                _live_resolver(_verifier(_malformed_body)),
                AUTHORIZATION,
                503,
                "memory_successor_unavailable",
            ),
            (
                _live_resolver(_authority_verifier(_session_absent)),
                AUTHORIZATION,
                401,
                "memory_authentication_required",
            ),
            (
                _live_resolver(_authority_verifier(_session_mismatch)),
                AUTHORIZATION,
                401,
                "memory_authentication_required",
            ),
            (
                _live_resolver(_authority_verifier(_session_malformed)),
                AUTHORIZATION,
                503,
                "memory_successor_unavailable",
            ),
            (
                _live_resolver(_authority_verifier(_unavailable)),
                AUTHORIZATION,
                503,
                "memory_successor_unavailable",
            ),
        )
        for resolver, headers, expected_status, expected_code in runtime_cases:
            with self.subTest(expected_status=expected_status, expected_code=expected_code):
                await self._assert_public_mapping(
                    resolver,
                    headers=headers,
                    expected_status=expected_status,
                    expected_code=expected_code,
                )

    async def test_real_request_and_configuration_errors_are_mapped(self) -> None:
        verifier = _verifier(_owner_body)
        with self.assertRaises(HttpAuthError) as request_error:
            await verifier(object(), owner_actor())  # type: ignore[arg-type]
        self.assertEqual(request_error.exception.code, "auth_live_request_invalid")
        await self._assert_public_mapping(
            FakeResolver(exception=request_error.exception),
            headers=AUTHORIZATION,
            expected_status=401,
            expected_code="memory_authentication_required",
        )

        with self.assertRaises(HttpAuthError) as configuration_error:
            live_supabase_runtime.LiveSupabaseAuthorityConfig(
                issuer="http://synthetic.invalid/auth/v1",
                api_key="synthetic-publishable-key",
            )
        self.assertEqual(
            configuration_error.exception.code,
            "auth_live_configuration_invalid",
        )
        await self._assert_public_mapping(
            FakeResolver(exception=configuration_error.exception),
            headers=AUTHORIZATION,
            expected_status=503,
            expected_code="memory_successor_unavailable",
        )

    def test_default_live_user_request_explicitly_disables_caching(self) -> None:
        opener = _CapturingOpener()
        url = "https://synthetic.supabase.co/auth/v1/user"
        with patch.object(live_supabase_runtime, "build_opener", return_value=opener):
            self.assertEqual(
                live_supabase_runtime._default_fetch_user(  # type: ignore[attr-defined]
                    url,
                    "Bearer a.b.c",
                    "synthetic-publishable-key",
                    2,
                    1024,
                ),
                b"{}",
            )
        request = opener.request
        self.assertIsNotNone(request)
        headers = {
            key.lower(): value
            for key, value in request.header_items()  # type: ignore[attr-defined]
        }
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["pragma"], "no-cache")
        self.assertEqual(opener.timeout, 2)


class OwnerRequestDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_cancels_authentication_before_returning_503(self) -> None:
        class BlockingResolver(FakeResolver):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled = False

            async def __call__(
                self,
                request: Request,
                scopes: tuple[ActorScope, ...],
            ) -> VerifiedActor:
                self.calls.append((request, scopes))
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                raise AssertionError("deadline failed to cancel authentication")

        resolver = BlockingResolver()
        app = create_owner_memory_app(
            actor_resolver=resolver,
            facade=FakeFacade(),
            feature_enabled=True,
        )
        with patch(
            "rag_engine.governed_memory.http_api.OWNER_REQUEST_DEADLINE_SECONDS",
            0.01,
        ):
            status, headers, body = await asgi_request(
                app,
                "GET",
                "/memory/status",
            )
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"code": "memory_successor_unavailable"}})
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(resolver.cancelled)

    async def test_deadline_cancels_mutation_and_marks_outcome_unknown(
        self,
    ) -> None:
        class BlockingMutationFacade(FakeFacade):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled = False

            async def delete_claim(
                self,
                actor: VerifiedActor,
                claim_id: UUID,
                body: Mapping[str, Any],
            ) -> Mapping[str, Any]:
                self.calls.append(("delete_claim_started", (actor, claim_id, body)))
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                raise AssertionError("deadline failed to cancel mutation")

        facade = BlockingMutationFacade()
        app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=facade,
            feature_enabled=True,
        )
        with patch(
            "rag_engine.governed_memory.http_api.OWNER_REQUEST_DEADLINE_SECONDS",
            0.01,
        ):
            status, headers, body = await asgi_request(
                app,
                "DELETE",
                f"/memory/claims/{CLAIM}",
                json_body=transition_body(),
            )
        self.assertEqual(status, 503)
        self.assertEqual(
            body, {"error": {"code": "memory_operation_outcome_unknown"}}
        )
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(facade.cancelled)
        self.assertEqual(facade.calls[0][0], "delete_claim_started")

    async def test_deadline_marks_post_mutation_outcome_unknown(self) -> None:
        class BlockingPostFacade(FakeFacade):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled = False

            async def review_proposal(
                self,
                actor: VerifiedActor,
                proposal_id: UUID,
                body: Mapping[str, Any],
            ) -> Mapping[str, Any]:
                self.calls.append(
                    ("review_proposal_started", (actor, proposal_id, body))
                )
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                raise AssertionError("deadline failed to cancel mutation")

        facade = BlockingPostFacade()
        app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=facade,
            feature_enabled=True,
        )
        with patch(
            "rag_engine.governed_memory.http_api.OWNER_REQUEST_DEADLINE_SECONDS",
            0.01,
        ):
            status, headers, body = await asgi_request(
                app,
                "POST",
                f"/memory/proposals/{PROPOSAL}/review",
                json_body=review_body(),
            )
        self.assertEqual(status, 503)
        self.assertEqual(
            body, {"error": {"code": "memory_operation_outcome_unknown"}}
        )
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(facade.cancelled)
        self.assertEqual(facade.calls[0][0], "review_proposal_started")


class OwnerStoreDeadlineRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_timeout_unwinds_active_transaction_with_cancellation(
        self,
    ) -> None:
        class BlockingConnection(_Connection):
            def __init__(self) -> None:
                super().__init__()
                self.query_cancelled = False

            async def fetchrow(self, query: str, *args: Any) -> Any:
                self.fetchrow_calls.append((query, args))
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.query_cancelled = True
                    raise
                raise AssertionError("store deadline failed to cancel query")

        connection = BlockingConnection()
        store = PostgresOwnerStore(
            _Pool(connection),
            operation_timeout_seconds=0.01,
        )

        with self.assertRaisesRegex(OwnerStoreError, "database_unavailable"):
            await store.delete_claim(owner_actor(), CLAIM, transition_body())

        self.assertTrue(connection.query_cancelled)
        self.assertEqual(connection.transaction_exits, [asyncio.CancelledError])
        self.assertEqual(connection.acquire_exits, [asyncio.CancelledError])

    async def test_store_timeout_is_unknown_outcome_through_http(self) -> None:
        class BlockingConnection(_Connection):
            def __init__(self) -> None:
                super().__init__()
                self.query_cancelled = False

            async def fetchrow(self, query: str, *args: Any) -> Any:
                self.fetchrow_calls.append((query, args))
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.query_cancelled = True
                    raise
                raise AssertionError("store deadline failed to cancel query")

        connection = BlockingConnection()
        store = PostgresOwnerStore(
            _Pool(connection),
            operation_timeout_seconds=0.01,
        )
        app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=store,
            feature_enabled=True,
        )

        status, headers, body = await asgi_request(
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
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(connection.query_cancelled)
        self.assertEqual(connection.transaction_exits, [asyncio.CancelledError])
        self.assertEqual(connection.acquire_exits, [asyncio.CancelledError])

    async def test_commit_failure_is_unknown_outcome_through_http(self) -> None:
        class CommitFailureTransaction:
            def __init__(self, connection: "CommitFailureConnection") -> None:
                self.connection = connection

            async def __aenter__(self) -> None:
                self.connection.transaction_entries += 1

            async def __aexit__(
                self,
                exception_type: Any,
                _exception: Any,
                _traceback: Any,
            ) -> None:
                self.connection.transaction_exits.append(exception_type)
                if exception_type is None:
                    raise ConnectionError("sensitive commit failure")

        class CommitFailureConnection(_Connection):
            def transaction(self) -> CommitFailureTransaction:
                return CommitFailureTransaction(self)

        connection = CommitFailureConnection()
        store = PostgresOwnerStore(_Pool(connection))
        app = create_owner_memory_app(
            actor_resolver=FakeResolver(),
            facade=store,
            feature_enabled=True,
        )

        status, headers, body = await asgi_request(
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
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("sensitive", json.dumps(body))
        self.assertEqual(len(connection.fetchrow_calls), 1)
        self.assertEqual(connection.transaction_exits, [None])
        self.assertEqual(connection.acquire_exits, [ConnectionError])


if __name__ == "__main__":
    unittest.main()
