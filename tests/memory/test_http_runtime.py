from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import unittest
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Request
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from rag_engine.governed_memory.auth import ActorScope
from rag_engine.governed_memory.http_api import create_owner_memory_app
from rag_engine.governed_memory.http_auth import (
    HttpAuthError,
    KeyNotFound,
    KeyResolverUnavailable,
)
from rag_engine.governed_memory.http_runtime import (
    BoundedCachedJwksKeyResolver,
    SupabaseHttpRuntimeConfig,
    create_supabase_actor_resolver,
)


ISSUER = "https://synthetic.supabase.invalid/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
AUDIENCE = "authenticated"
OWNER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OWNER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SESSION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())
SERVICE_TOKEN = "synthetic-service-token"


def config(**overrides: object) -> SupabaseHttpRuntimeConfig:
    values: dict[str, object] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": JWKS_URL,
        "expected_service_token": SERVICE_TOKEN,
    }
    values.update(overrides)
    return SupabaseHttpRuntimeConfig(**values)  # type: ignore[arg-type]


def rsa_jwk(public_key: object, *, key_id: str = "synthetic-rsa") -> dict[str, object]:
    material = json.loads(RSAAlgorithm.to_jwk(public_key))
    material.update(
        {
            "alg": "RS256",
            "kid": key_id,
            "key_ops": ["verify"],
            "use": "sig",
        }
    )
    return material


def ec_jwk(public_key: object, *, key_id: str = "synthetic-ec") -> dict[str, object]:
    material = json.loads(ECAlgorithm.to_jwk(public_key))
    material.update(
        {
            "alg": "ES256",
            "kid": key_id,
            "key_ops": ["verify"],
            "use": "sig",
        }
    )
    return material


def jwks_bytes(*keys: dict[str, object]) -> bytes:
    return json.dumps(
        {"keys": list(keys)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_from_raw_headers(
    headers: list[tuple[bytes, bytes]],
    *,
    path: str = "/memory/status",
) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "server": ("synthetic.invalid", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


class StaticFetcher:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, url: str, timeout: int, maximum_bytes: int) -> bytes:
        self.calls.append((url, timeout, maximum_bytes))
        return self.body


class RuntimeConfigurationTests(unittest.TestCase):
    def test_https_configuration_is_explicit_and_redacts_service_token(self) -> None:
        active = config()
        self.assertEqual(active.issuer, ISSUER)
        self.assertEqual(active.jwks_url, JWKS_URL)
        self.assertNotIn(SERVICE_TOKEN, repr(active))

    def test_http_requires_explicit_literal_loopback_disposable_opt_in(self) -> None:
        loopback_issuer = "http://127.0.0.1:18091/auth/v1"
        loopback_jwks = f"{loopback_issuer}/.well-known/jwks.json"
        with self.assertRaises(HttpAuthError) as disabled:
            config(issuer=loopback_issuer, jwks_url=loopback_jwks)
        self.assertEqual(disabled.exception.code, "auth_configuration_invalid")

        active = config(
            issuer=loopback_issuer,
            jwks_url=loopback_jwks,
            allow_disposable_loopback_http=True,
        )
        self.assertTrue(active.allow_disposable_loopback_http)

        for host in ("localhost", "192.0.2.10", "example.invalid"):
            issuer = f"http://{host}:18091/auth/v1"
            with self.subTest(host=host), self.assertRaises(HttpAuthError):
                config(
                    issuer=issuer,
                    jwks_url=f"{issuer}/.well-known/jwks.json",
                    allow_disposable_loopback_http=True,
                )

    def test_jwks_url_must_be_the_exact_issuer_endpoint(self) -> None:
        for jwks_url in (
            JWKS_URL + "?key=value",
            "https://other.invalid/auth/v1/.well-known/jwks.json",
            f"{ISSUER}/../.well-known/jwks.json",
        ):
            with self.subTest(jwks_url=jwks_url), self.assertRaises(
                HttpAuthError
            ) as caught:
                config(jwks_url=jwks_url)
            self.assertEqual(caught.exception.code, "auth_configuration_invalid")

    def test_factory_construction_is_lazy_and_has_no_fetch_or_network(self) -> None:
        calls: list[str] = []

        def forbidden_fetcher(_url: str, _timeout: int, _maximum: int) -> bytes:
            calls.append("called")
            raise AssertionError("construction_must_not_fetch")

        resolver = create_supabase_actor_resolver(
            config(),
            fetcher=forbidden_fetcher,
            token_clock=lambda: NOW,
            cache_clock=lambda: 1.0,
        )
        self.assertTrue(callable(resolver))
        self.assertEqual(calls, [])


class BoundedJwksCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.weak_rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=1024,
        )
        cls.ec_private = ec.generate_private_key(ec.SECP256R1())

    def test_cache_is_lazy_bounded_and_uses_exact_kid_algorithm_pairs(self) -> None:
        body = jwks_bytes(
            rsa_jwk(self.rsa_private.public_key()),
            ec_jwk(self.ec_private.public_key()),
        )
        fetcher = StaticFetcher(body)
        now = [100.0]
        resolver = BoundedCachedJwksKeyResolver(
            config(cache_ttl_seconds=30),
            fetcher=fetcher,
            cache_clock=lambda: now[0],
        )
        self.assertEqual(fetcher.calls, [])

        rsa_key = resolver("synthetic-rsa", "RS256")
        self.assertEqual(
            rsa_key.public_numbers(), self.rsa_private.public_key().public_numbers()
        )
        resolver("synthetic-ec", "ES256")
        self.assertEqual(len(fetcher.calls), 1)

        for key_id, algorithm in (
            ("synthetic-rsa", "ES256"),
            ("synthetic-ec", "RS256"),
            ("missing", "RS256"),
            ("synthetic-rsa", "HS256"),
        ):
            with self.subTest(key_id=key_id, algorithm=algorithm), self.assertRaises(
                KeyNotFound
            ):
                resolver(key_id, algorithm)
        self.assertEqual(len(fetcher.calls), 1)

        now[0] = 131.0
        resolver("synthetic-rsa", "RS256")
        self.assertEqual(len(fetcher.calls), 2)

    def test_invalid_or_ambiguous_jwks_fail_closed(self) -> None:
        valid = rsa_jwk(self.rsa_private.public_key())
        duplicate = jwks_bytes(valid, dict(valid))
        private = dict(valid)
        private["d"] = "forbidden"
        unsupported = jwks_bytes(
            {
                "alg": "HS256",
                "kid": "symmetric",
                "kty": "oct",
                "k": "Zm9yYmlkZGVu",
            }
        )
        duplicate_json = b'{"keys":[],"keys":[]}'
        cases = (
            b"{}",
            b'{"keys":[]}',
            duplicate,
            jwks_bytes(private),
            jwks_bytes(rsa_jwk(self.weak_rsa_private.public_key())),
            unsupported,
            duplicate_json,
        )
        for body in cases:
            resolver = BoundedCachedJwksKeyResolver(
                config(),
                fetcher=StaticFetcher(body),
                cache_clock=lambda: 1.0,
            )
            with self.subTest(body=body[:80]), self.assertRaises(
                KeyResolverUnavailable
            ):
                resolver("synthetic-rsa", "RS256")

    def test_unknown_kid_refresh_is_rotation_friendly_and_rate_bounded(self) -> None:
        rotated_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        bodies = [
            jwks_bytes(rsa_jwk(self.rsa_private.public_key())),
            jwks_bytes(
                rsa_jwk(self.rsa_private.public_key()),
                rsa_jwk(rotated_private.public_key(), key_id="rotated-rsa"),
            ),
        ]
        calls: list[int] = []

        def rotating_fetcher(_url: str, _timeout: int, _maximum: int) -> bytes:
            calls.append(len(calls))
            return bodies[min(len(calls) - 1, len(bodies) - 1)]

        now = [100.0]
        resolver = BoundedCachedJwksKeyResolver(
            config(cache_ttl_seconds=30, minimum_refresh_interval_seconds=10),
            fetcher=rotating_fetcher,
            cache_clock=lambda: now[0],
        )
        resolver("synthetic-rsa", "RS256")
        now[0] = 105.0
        with self.assertRaises(KeyNotFound):
            resolver("rotated-rsa", "RS256")
        self.assertEqual(len(calls), 1)

        now[0] = 110.0
        rotated = resolver("rotated-rsa", "RS256")
        self.assertEqual(
            rotated.public_numbers(), rotated_private.public_key().public_numbers()
        )
        self.assertEqual(len(calls), 2)

    def test_failed_refresh_is_rate_bounded_across_concurrent_requests(self) -> None:
        calls: list[int] = []
        now = [100.0]

        def failing_fetcher(_url: str, _timeout: int, _maximum: int) -> bytes:
            calls.append(len(calls))
            raise OSError("synthetic outage")

        resolver = BoundedCachedJwksKeyResolver(
            config(minimum_refresh_interval_seconds=10),
            fetcher=failing_fetcher,
            cache_clock=lambda: now[0],
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(resolver, "synthetic-rsa", "RS256")
                for _ in range(8)
            ]
            for future in futures:
                with self.assertRaises(KeyResolverUnavailable):
                    future.result()
        self.assertEqual(len(calls), 1)

        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")
        self.assertEqual(len(calls), 1)

        now[0] = 110.0
        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")
        self.assertEqual(len(calls), 2)

    def test_expired_cache_refresh_failure_is_rate_bounded(self) -> None:
        body = jwks_bytes(rsa_jwk(self.rsa_private.public_key()))
        calls: list[int] = []
        now = [100.0]

        def failing_after_initial(
            _url: str, _timeout: int, _maximum: int
        ) -> bytes:
            calls.append(len(calls))
            if len(calls) == 1:
                return body
            raise OSError("synthetic outage")

        resolver = BoundedCachedJwksKeyResolver(
            config(cache_ttl_seconds=30, minimum_refresh_interval_seconds=10),
            fetcher=failing_after_initial,
            cache_clock=lambda: now[0],
        )
        resolver("synthetic-rsa", "RS256")
        now[0] = 131.0
        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")
        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")
        self.assertEqual(len(calls), 2)

        now[0] = 141.0
        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")
        self.assertEqual(len(calls), 3)
    def test_fetch_and_size_failures_are_unavailable_not_key_absence(self) -> None:
        def failing_fetcher(_url: str, _timeout: int, _maximum: int) -> bytes:
            raise OSError("synthetic failure")

        resolver = BoundedCachedJwksKeyResolver(
            config(),
            fetcher=failing_fetcher,
            cache_clock=lambda: 1.0,
        )
        with self.assertRaises(KeyResolverUnavailable):
            resolver("synthetic-rsa", "RS256")

        oversized = BoundedCachedJwksKeyResolver(
            config(maximum_jwks_bytes=1_024),
            fetcher=StaticFetcher(b"x" * 1_025),
            cache_clock=lambda: 1.0,
        )
        with self.assertRaises(KeyResolverUnavailable):
            oversized("synthetic-rsa", "RS256")


class AsyncActorResolverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.jwks = jwks_bytes(rsa_jwk(cls.rsa_private.public_key()))

    def claims(self) -> dict[str, object]:
        return {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(OWNER_A),
            "session_id": str(SESSION_ID),
            "role": "authenticated",
            "is_anonymous": False,
            "iat": NOW_SECONDS - 60,
            "exp": NOW_SECONDS + 300,
            "user_metadata": {"owner_user_id": str(OWNER_B)},
        }

    def token(self) -> str:
        return jwt.encode(
            self.claims(),
            self.rsa_private,
            algorithm="RS256",
            headers={"alg": "RS256", "kid": "synthetic-rsa", "typ": "JWT"},
        )

    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        return [
            (b"authorization", f"Bearer {self.token()}".encode("ascii")),
            (b"x-governed-memory-service-token", SERVICE_TOKEN.encode("ascii")),
        ]

    def resolver(self, fetcher: StaticFetcher | None = None):
        active_fetcher = fetcher or StaticFetcher(self.jwks)
        return (
            create_supabase_actor_resolver(
                config(),
                fetcher=active_fetcher,
                token_clock=lambda: NOW,
                cache_clock=lambda: 1.0,
            ),
            active_fetcher,
        )

    async def test_async_resolver_uses_raw_headers_and_derives_signed_subject(self) -> None:
        resolver, fetcher = self.resolver()
        actor = await resolver(
            request_from_raw_headers(self.raw_headers()),
            (ActorScope.READ_CLAIMS,),
        )
        self.assertEqual(actor.owner_user_id, OWNER_A)
        self.assertEqual(actor.actor_id, OWNER_A)
        self.assertEqual(actor.scopes, (ActorScope.READ_CLAIMS,))
        self.assertEqual(len(fetcher.calls), 1)

    async def test_sensitive_duplicate_headers_fail_before_mapping_or_fetch(self) -> None:
        for name in (
            b"authorization",
            b"x-governed-memory-service-token",
            b"x-owner-user-id",
            b"x-custom-actor-reference",
        ):
            resolver, fetcher = self.resolver()
            headers = self.raw_headers()
            value = headers[0][1] if name == b"authorization" else b"duplicate"
            headers.extend([(name, value), (name.upper(), value)])
            with self.subTest(name=name), self.assertRaises(HttpAuthError) as caught:
                await resolver(
                    request_from_raw_headers(headers),
                    (ActorScope.READ_CLAIMS,),
                )
            self.assertEqual(caught.exception.code, "auth_header_ambiguous")
            self.assertEqual(fetcher.calls, [])

    async def test_single_authority_header_is_rejected_before_jwks_fetch(self) -> None:
        resolver, fetcher = self.resolver()
        headers = self.raw_headers()
        headers.append((b"x-custom-owner-reference", str(OWNER_B).encode("ascii")))
        with self.assertRaises(HttpAuthError) as caught:
            await resolver(
                request_from_raw_headers(headers),
                (ActorScope.READ_CLAIMS,),
            )
        self.assertEqual(
            caught.exception.code, "auth_explicit_authority_header_forbidden"
        )
        self.assertEqual(fetcher.calls, [])

    async def test_resolver_is_directly_usable_by_owner_memory_app(self) -> None:
        resolver, fetcher = self.resolver()

        class StatusFacade:
            async def status(self, _actor: object) -> dict[str, object]:
                return {
                    "active_claims": 1,
                    "pending_proposals": 2,
                    "pending_projection": 0,
                    "failed_projection": 0,
                    "last_transition_at": NOW,
                }

        app = create_owner_memory_app(
            actor_resolver=resolver,
            facade=StatusFacade(),  # type: ignore[arg-type]
            feature_enabled=True,
        )
        request_messages = [{"type": "http.request", "body": b"", "more_body": False}]
        response_messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return request_messages.pop(0)

        async def send(message: dict[str, object]) -> None:
            response_messages.append(message)

        scope = request_from_raw_headers(self.raw_headers()).scope
        await app(scope, receive, send)
        start = next(item for item in response_messages if item["type"] == "http.response.start")
        body = b"".join(
            item.get("body", b"")  # type: ignore[arg-type]
            for item in response_messages
            if item["type"] == "http.response.body"
        )
        self.assertEqual(start["status"], 200)
        self.assertEqual(
            json.loads(body),
            {
                "active_claims": 1,
                "pending_proposals": 2,
                "pending_projection": 0,
                "failed_projection": 0,
                "last_transition_at": NOW.isoformat(),
            },
        )
        self.assertEqual(len(fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
