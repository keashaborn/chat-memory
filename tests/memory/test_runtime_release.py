from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID

from fastapi import FastAPI, Request

from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.http_auth import HttpAuthError
from rag_engine.governed_memory.http_service import (
    CONVERSATION_BRIDGE_CATALOG_SHA256_ENV,
    HttpServiceConfigurationError,
)
from rag_engine.governed_memory.runtime.application import (
    API_BIND_HOST,
    API_BIND_PORT,
    FRONTEND_SOURCE_IPV4,
    SUPABASE_API_KEY_ENV,
    create_runtime_application,
    main,
)
from rag_engine.governed_memory.runtime.live_supabase import (
    LiveSupabaseAuthorityConfig,
    LiveSupabaseAuthorityVerifier,
    LiveSupabaseSessionVerifier,
    LiveSupabaseUserVerifier,
)
import rag_engine.governed_memory.runtime.live_supabase as live_supabase_module


ISSUER = "https://synthetic.supabase.invalid/auth/v1"
OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
SESSION = UUID("33333333-3333-4333-8333-333333333333")
AUTHORIZATION = "Bearer aaa.bbb.ccc"
API_KEY = "synthetic-public-api-key"
HASH = "a" * 64
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)


def owner_actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OWNER,
        session_id=SESSION,
        role=ActorRole.OWNER,
        scopes=(ActorScope.READ_CLAIMS,),
        authentication_manifest_sha256=HASH,
        authenticated_at=NOW,
    )


def worker_actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OTHER,
        session_id=SESSION,
        role=ActorRole.WORKER,
        scopes=(ActorScope.LEASE_EXTRACTION,),
        authentication_manifest_sha256=HASH,
        authenticated_at=NOW,
    )


def request_with_headers(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/memory/status",
            "raw_path": b"/memory/status",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("172.31.43.160", 12345),
            "server": ("172.31.32.171", 8091),
        }
    )


def active_environment() -> dict[str, str]:
    return {
        "GOVERNED_MEMORY_HTTP_MODE": "on",
        "GOVERNED_MEMORY_POSTGRES_DSN": (
            "postgresql://governed_memory_api:secret@127.0.0.1:55432/"
            "governed_memory"
        ),
        "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN": (
            "postgresql://governed_memory_api:other-secret@127.0.0.1:5432/"
            "memory"
        ),
        CONVERSATION_BRIDGE_CATALOG_SHA256_ENV: HASH,
        "GOVERNED_MEMORY_SUPABASE_ISSUER": ISSUER,
        "GOVERNED_MEMORY_SUPABASE_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
        "GOVERNED_MEMORY_SERVICE_TOKEN": "synthetic-service-token",
        SUPABASE_API_KEY_ENV: API_KEY,
    }


class RuntimeCompositionTests(unittest.TestCase):
    def test_off_mode_composition_constructs_no_live_adapter_or_pool(self) -> None:
        calls: list[dict[str, object]] = []

        def service_factory(**kwargs: object) -> FastAPI:
            calls.append(dict(kwargs))
            return FastAPI()

        fetch_calls = 0

        def forbidden_fetch(*_args: object) -> bytes:
            nonlocal fetch_calls
            fetch_calls += 1
            raise AssertionError("off mode performed live I/O")

        application = create_runtime_application(
            {},
            user_fetcher=forbidden_fetch,
            session_fetcher=forbidden_fetch,
            http_service_factory=service_factory,
        )
        self.assertIsInstance(application, FastAPI)
        self.assertEqual(fetch_calls, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["settings"].mode, "off")
        self.assertNotIn("authority_verifier", calls[0])

    def test_on_mode_composes_live_verifier_but_performs_no_fetch(self) -> None:
        calls: list[dict[str, object]] = []
        user_fetch_calls = 0
        session_fetch_calls = 0

        def user_fetcher(*_args: object) -> bytes:
            nonlocal user_fetch_calls
            user_fetch_calls += 1
            return json.dumps({"id": str(OWNER)}).encode("utf-8")

        def session_fetcher(*_args: object) -> bytes:
            nonlocal session_fetch_calls
            session_fetch_calls += 1
            return json.dumps(
                [
                    {
                        "owner_user_id": str(OWNER),
                        "session_id": str(SESSION),
                        "session_present": True,
                    }
                ]
            ).encode("utf-8")

        def service_factory(**kwargs: object) -> FastAPI:
            calls.append(dict(kwargs))
            return FastAPI()

        create_runtime_application(
            active_environment(),
            user_fetcher=user_fetcher,
            session_fetcher=session_fetcher,
            http_service_factory=service_factory,
        )
        self.assertEqual(user_fetch_calls, 0)
        self.assertEqual(session_fetch_calls, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["settings"].mode, "on")
        self.assertIsInstance(
            calls[0]["authority_verifier"],
            LiveSupabaseAuthorityVerifier,
        )
        authority = calls[0]["authority_verifier"]
        self.assertIsInstance(authority.user_verifier, LiveSupabaseUserVerifier)
        self.assertIsInstance(
            authority.session_verifier,
            LiveSupabaseSessionVerifier,
        )

    def test_on_mode_requires_dedicated_supabase_api_key(self) -> None:
        environment = active_environment()
        del environment[SUPABASE_API_KEY_ENV]
        with self.assertRaisesRegex(
            HttpServiceConfigurationError,
            "governed_memory_secret_invalid",
        ):
            create_runtime_application(environment)

    def test_off_mode_entrypoint_refuses_before_server_runner(self) -> None:
        calls: list[tuple[object, dict[str, object]]] = []

        def runner(app: object, **kwargs: object) -> None:
            calls.append((app, dict(kwargs)))

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = main([], environment={}, server_runner=runner)
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": {"code": "governed_memory_http_disabled"},
                "schema_version": "governed-memory-runtime-refusal-v1",
            },
        )

    def test_print_contract_bypasses_mode_and_never_runs_server(self) -> None:
        def forbidden_runner(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("print-contract invoked server runner")

        stdout = StringIO()
        with redirect_stdout(stdout):
            result = main(
                ["--print-contract"],
                environment={"GOVERNED_MEMORY_HTTP_MODE": "invalid"},
                server_runner=forbidden_runner,
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["default_mode"], "off")

    def test_on_mode_server_bind_is_exact(self) -> None:
        calls: list[tuple[object, dict[str, object]]] = []

        def runner(app: object, **kwargs: object) -> None:
            calls.append((app, dict(kwargs)))

        self.assertEqual(
            main([], environment=active_environment(), server_runner=runner),
            0,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1],
            {
                "host": API_BIND_HOST,
                "port": API_BIND_PORT,
                "access_log": False,
                "proxy_headers": False,
                "server_header": False,
                "workers": 1,
            },
        )
        self.assertEqual(API_BIND_HOST, "172.31.32.171")
        self.assertEqual(API_BIND_PORT, 8091)
        self.assertEqual(FRONTEND_SOURCE_IPV4, "172.31.43.160/32")


class LiveSupabaseConfigurationTests(unittest.TestCase):
    def test_https_user_endpoint_is_derived_exactly(self) -> None:
        config = LiveSupabaseAuthorityConfig(issuer=ISSUER, api_key=API_KEY)
        self.assertEqual(config.issuer_url, ISSUER)
        self.assertEqual(config.user_url, f"{ISSUER}/user")
        self.assertEqual(
            config.session_url,
            "https://synthetic.supabase.invalid/rest/v1/rpc/"
            "governed_memory_current_session_v1",
        )
        self.assertNotIn(API_KEY, repr(config))

    def test_non_https_redirectable_or_ambiguous_issuers_are_refused(self) -> None:
        invalid = (
            "http://synthetic.supabase.invalid/auth/v1",
            "https://synthetic.supabase.invalid/auth/v1/",
            "https://synthetic.supabase.invalid/auth/v1?x=1",
            "https://user@synthetic.supabase.invalid/auth/v1",
            "https://synthetic.supabase.invalid:8443/auth/v1",
        )
        for issuer in invalid:
            with self.subTest(issuer=issuer), self.assertRaisesRegex(
                HttpAuthError,
                "auth_live_configuration_invalid",
            ):
                LiveSupabaseAuthorityConfig(issuer=issuer, api_key=API_KEY)


class LiveSupabaseVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(
        self,
        fetcher: object,
        *,
        actor: VerifiedActor | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        verifier = LiveSupabaseUserVerifier(
            LiveSupabaseAuthorityConfig(issuer=ISSUER, api_key=API_KEY),
            fetcher=fetcher,  # type: ignore[arg-type]
        )
        await verifier(
            request_with_headers(
                headers
                if headers is not None
                else [(b"authorization", AUTHORIZATION.encode("ascii"))]
            ),
            actor if actor is not None else owner_actor(),
        )

    async def invoke_session(
        self,
        fetcher: object,
        *,
        actor: VerifiedActor | None = None,
    ) -> None:
        verifier = LiveSupabaseSessionVerifier(
            LiveSupabaseAuthorityConfig(issuer=ISSUER, api_key=API_KEY),
            fetcher=fetcher,  # type: ignore[arg-type]
        )
        await verifier(
            request_with_headers(
                [(b"authorization", AUTHORIZATION.encode("ascii"))]
            ),
            actor if actor is not None else owner_actor(),
        )

    async def test_authoritative_id_match_succeeds_and_fetch_is_bounded(self) -> None:
        observed: list[tuple[object, ...]] = []

        def fetcher(*args: object) -> bytes:
            observed.append(args)
            return json.dumps(
                {
                    "id": str(OWNER),
                    "ignored_profile": {"attempted_owner": str(OTHER)},
                }
            ).encode("utf-8")

        await self.invoke(fetcher)
        self.assertEqual(
            observed,
            [(f"{ISSUER}/user", AUTHORIZATION, API_KEY, 5, 65_536)],
        )

    async def test_each_live_boundary_failure_has_stable_code(self) -> None:
        cases: list[tuple[str, object, VerifiedActor | None, list[tuple[bytes, bytes]] | None]] = [
            (
                "auth_live_authorization_invalid",
                lambda *_args: b"{}",
                None,
                [],
            ),
            (
                "auth_live_actor_invalid",
                lambda *_args: b"{}",
                worker_actor(),
                None,
            ),
            (
                "auth_live_authority_unavailable",
                lambda *_args: (_ for _ in ()).throw(OSError()),
                None,
                None,
            ),
            (
                "auth_live_session_denied",
                lambda *_args: (_ for _ in ()).throw(
                    live_supabase_module._LiveAuthorityDenied()
                ),
                None,
                None,
            ),
            (
                "auth_live_response_invalid",
                lambda *_args: b'{"id":"x","id":"y"}',
                None,
                None,
            ),
            (
                "auth_live_user_mismatch",
                lambda *_args: json.dumps({"id": str(OTHER)}).encode("utf-8"),
                None,
                None,
            ),
        ]
        for expected, fetcher, actor, headers in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                HttpAuthError,
                expected,
            ):
                await self.invoke(
                    fetcher,
                    actor=actor,
                    headers=headers,
                )

    async def test_async_wall_clock_timeout_is_bounded(self) -> None:
        def slow_fetcher(*_args: object) -> bytes:
            import time

            time.sleep(2.0)
            return json.dumps({"id": str(OWNER)}).encode("utf-8")

        verifier = LiveSupabaseUserVerifier(
            LiveSupabaseAuthorityConfig(
                issuer=ISSUER,
                api_key=API_KEY,
                timeout_seconds=1,
            ),
            fetcher=slow_fetcher,
        )
        started = asyncio.get_running_loop().time()
        with self.assertRaisesRegex(
            HttpAuthError,
            "auth_live_authority_unavailable",
        ):
            await verifier(
                request_with_headers(
                    [(b"authorization", AUTHORIZATION.encode("ascii"))]
                ),
                owner_actor(),
            )
        self.assertLess(asyncio.get_running_loop().time() - started, 2.25)

    async def test_session_rpc_match_succeeds_and_fetch_is_bounded(self) -> None:
        observed: list[tuple[object, ...]] = []

        def fetcher(*args: object) -> bytes:
            observed.append(args)
            return json.dumps(
                [
                    {
                        "owner_user_id": str(OWNER),
                        "session_id": str(SESSION),
                        "session_present": True,
                    }
                ]
            ).encode("utf-8")

        await self.invoke_session(fetcher)
        self.assertEqual(
            observed,
            [
                (
                    "https://synthetic.supabase.invalid/rest/v1/rpc/"
                    "governed_memory_current_session_v1",
                    AUTHORIZATION,
                    API_KEY,
                    5,
                    65_536,
                )
            ],
        )

    async def test_session_rpc_failures_are_strict_and_stable(self) -> None:
        def response(
            *,
            owner: UUID = OWNER,
            session: UUID = SESSION,
            present: bool = True,
            extra: bool = False,
        ) -> bytes:
            row: dict[str, object] = {
                "owner_user_id": str(owner),
                "session_id": str(session),
                "session_present": present,
            }
            if extra:
                row["unexpected"] = "denied"
            return json.dumps([row]).encode("utf-8")

        cases: list[tuple[str, object]] = [
            (
                "auth_live_session_denied",
                lambda *_args: response(present=False),
            ),
            (
                "auth_live_session_denied",
                lambda *_args: (_ for _ in ()).throw(
                    live_supabase_module._LiveSessionDenied()
                ),
            ),
            (
                "auth_live_session_mismatch",
                lambda *_args: response(owner=OTHER),
            ),
            (
                "auth_live_session_mismatch",
                lambda *_args: response(session=OTHER),
            ),
            (
                "auth_live_session_response_invalid",
                lambda *_args: response(extra=True),
            ),
            (
                "auth_live_session_response_invalid",
                lambda *_args: b"[]",
            ),
            (
                "auth_live_authority_unavailable",
                lambda *_args: (_ for _ in ()).throw(OSError()),
            ),
        ]
        for expected, fetcher in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                HttpAuthError,
                expected,
            ):
                await self.invoke_session(fetcher)

    async def test_session_http_status_mapping_is_closed(self) -> None:
        class ErrorOpener:
            def __init__(self, status: int) -> None:
                self.status = status

            def open(self, request: object, timeout: int) -> object:
                raise HTTPError(
                    getattr(request, "full_url", "https://invalid"),
                    self.status,
                    "sensitive authority response",
                    {},
                    None,
                )

        arguments = (
            "https://synthetic.supabase.invalid/rest/v1/rpc/"
            "governed_memory_current_session_v1",
            AUTHORIZATION,
            API_KEY,
            5,
            65_536,
        )
        with patch.object(
            live_supabase_module,
            "build_opener",
            return_value=ErrorOpener(401),
        ), self.assertRaises(live_supabase_module._LiveSessionDenied):
            live_supabase_module._default_fetch_session(*arguments)
        for status in (302, 403, 404, 429, 500):
            with self.subTest(status=status), patch.object(
                live_supabase_module,
                "build_opener",
                return_value=ErrorOpener(status),
            ), self.assertRaises(live_supabase_module._LiveAuthorityUnavailable):
                live_supabase_module._default_fetch_session(*arguments)

    async def test_composite_checks_user_before_session_on_every_call(self) -> None:
        observed: list[str] = []

        def user_fetcher(*_args: object) -> bytes:
            observed.append("user")
            return json.dumps({"id": str(OWNER)}).encode("utf-8")

        def session_fetcher(*_args: object) -> bytes:
            observed.append("session")
            return json.dumps(
                [
                    {
                        "owner_user_id": str(OWNER),
                        "session_id": str(SESSION),
                        "session_present": True,
                    }
                ]
            ).encode("utf-8")

        config = LiveSupabaseAuthorityConfig(issuer=ISSUER, api_key=API_KEY)
        verifier = LiveSupabaseAuthorityVerifier(
            LiveSupabaseUserVerifier(config, fetcher=user_fetcher),
            LiveSupabaseSessionVerifier(config, fetcher=session_fetcher),
        )
        request = request_with_headers(
            [(b"authorization", AUTHORIZATION.encode("ascii"))]
        )
        await verifier(request, owner_actor())
        await verifier(request, owner_actor())
        self.assertEqual(observed, ["user", "session", "user", "session"])


if __name__ == "__main__":
    unittest.main()
