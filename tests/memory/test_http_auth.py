from __future__ import annotations

from datetime import UTC, datetime
import unittest
from unittest.mock import patch
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ec, rsa
import jwt

from rag_engine.governed_memory.auth import ActorRole, ActorScope
from rag_engine.governed_memory.http_auth import (
    GovernedMemoryRequestAuthenticator,
    HttpAuthError,
    KeyNotFound,
    KeyResolverUnavailable,
    SupabaseJwtVerifier,
)


ISSUER = "https://synthetic-successor.supabase.co/auth/v1"
AUDIENCE = "authenticated"
OWNER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OWNER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SESSION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())
SERVICE_TOKEN = "synthetic-service-token"


class RecordingResolver:
    def __init__(self, keys: dict[tuple[str, str], object]) -> None:
        self.keys = keys
        self.calls: list[tuple[str, str]] = []

    def __call__(self, key_id: str, algorithm: str) -> object:
        self.calls.append((key_id, algorithm))
        try:
            return self.keys[(key_id, algorithm)]
        except KeyError as exc:
            raise KeyNotFound from exc


class SupabaseHttpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.rsa_public = cls.rsa_private.public_key()
        cls.other_rsa_private = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.ec_private = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public = cls.ec_private.public_key()

    def setUp(self) -> None:
        self.resolver = RecordingResolver(
            {
                ("rsa-key", "RS256"): self.rsa_public,
                ("ec-key", "ES256"): self.ec_public,
            }
        )
        self.verifier = SupabaseJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            key_resolver=self.resolver,
            clock=lambda: NOW,
        )

    def claims(self, **overrides: object) -> dict[str, object]:
        claims: dict[str, object] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(OWNER_A),
            "session_id": str(SESSION_ID),
            "role": "authenticated",
            "is_anonymous": False,
            "iat": NOW_SECONDS - 60,
            "exp": NOW_SECONDS + 300,
        }
        claims.update(overrides)
        return claims

    def token(
        self,
        *,
        algorithm: str = "RS256",
        key_id: str = "rsa-key",
        claims: dict[str, object] | None = None,
        private_key: object | None = None,
        headers: dict[str, object] | None = None,
    ) -> str:
        key = private_key or (
            self.rsa_private if algorithm == "RS256" else self.ec_private
        )
        active_headers: dict[str, object] = {"kid": key_id, "typ": "JWT"}
        active_headers.update(headers or {})
        return jwt.encode(
            claims or self.claims(),
            key,
            algorithm=algorithm,
            headers=active_headers,
        )

    def authorization(self, token: str | None = None) -> str:
        return f"Bearer {token or self.token()}"

    def assert_error(self, code: str, callback: object) -> None:
        with self.assertRaises(HttpAuthError) as caught:
            callback()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def verify(
        self,
        token: str | None = None,
        *,
        scopes: tuple[ActorScope, ...] = (ActorScope.READ_CLAIMS,),
    ):
        return self.verifier.verify_bearer(
            self.authorization(token),
            scopes=scopes,
        )

    def test_rs256_derives_owner_actor_only_from_subject(self) -> None:
        actor = self.verify()

        self.assertEqual(actor.owner_user_id, OWNER_A)
        self.assertEqual(actor.actor_id, OWNER_A)
        self.assertIs(actor.role, ActorRole.OWNER)
        self.assertEqual(actor.scopes, (ActorScope.READ_CLAIMS,))
        self.assertEqual(actor.authenticated_at, NOW)
        self.assertRegex(actor.authentication_manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(self.resolver.calls, [("rsa-key", "RS256")])

    def test_es256_is_the_only_other_accepted_algorithm(self) -> None:
        actor = self.verify(
            self.token(algorithm="ES256", key_id="ec-key")
        )
        self.assertEqual(actor.owner_user_id, OWNER_A)
        self.assertEqual(self.resolver.calls, [("ec-key", "ES256")])

    def test_route_scopes_are_used_exactly_without_augmentation(self) -> None:
        scopes = (ActorScope.MUTATE_CLAIMS, ActorScope.READ_CLAIMS)
        actor = self.verify(scopes=scopes)
        self.assertEqual(actor.scopes, scopes)

        for invalid in (
            (),
            (ActorScope.READ_CLAIMS, ActorScope.MUTATE_CLAIMS),
            (ActorScope.READ_CLAIMS, ActorScope.READ_CLAIMS),
        ):
            with self.subTest(scopes=invalid):
                self.assert_error(
                    "auth_scopes_invalid",
                    lambda invalid=invalid: self.verify(scopes=invalid),
                )

    def test_user_and_app_metadata_are_completely_non_authoritative(self) -> None:
        baseline = self.verify()
        forged_metadata = {
            "owner_user_id": str(OWNER_B),
            "actor_id": str(OWNER_B),
            "role": "admin",
            "scopes": [scope.value for scope in ActorScope],
        }
        actor = self.verify(
            self.token(
                claims=self.claims(
                    user_metadata=forged_metadata,
                    app_metadata=forged_metadata,
                )
            )
        )

        self.assertEqual(actor.owner_user_id, OWNER_A)
        self.assertEqual(actor.actor_id, OWNER_A)
        self.assertEqual(actor.scopes, (ActorScope.READ_CLAIMS,))
        self.assertEqual(
            actor.authentication_manifest_sha256,
            baseline.authentication_manifest_sha256,
        )

    def test_anonymous_and_non_user_roles_are_denied(self) -> None:
        self.assert_error(
            "auth_anonymous_forbidden",
            lambda: self.verify(
                self.token(claims=self.claims(is_anonymous=True))
            ),
        )
        self.assert_error(
            "auth_role_denied",
            lambda: self.verify(
                self.token(claims=self.claims(role="service_role"))
            ),
        )

    def test_hs256_and_none_are_denied_before_key_resolution(self) -> None:
        hs_token = jwt.encode(
            self.claims(),
            "synthetic-shared-secret-that-is-long-enough",
            algorithm="HS256",
            headers={"kid": "shared-key", "typ": "JWT"},
        )
        none_token = jwt.encode(
            self.claims(),
            key=None,
            algorithm="none",
            headers={"kid": "none-key", "typ": "JWT"},
        ) + "synthetic-signature"
        for token in (hs_token, none_token):
            with self.subTest(algorithm=jwt.get_unverified_header(token)["alg"]):
                self.assert_error(
                    "auth_algorithm_denied", lambda token=token: self.verify(token)
                )
        self.assertEqual(self.resolver.calls, [])

    def test_issuer_and_audience_require_exact_scalar_matches(self) -> None:
        cases = (
            ("auth_issuer_mismatch", {"iss": ISSUER + "/"}),
            ("auth_audience_mismatch", {"aud": "anon"}),
            ("auth_audience_mismatch", {"aud": [AUDIENCE]}),
        )
        for code, override in cases:
            with self.subTest(code=code, override=override):
                self.assert_error(
                    code,
                    lambda override=override: self.verify(
                        self.token(claims=self.claims(**override))
                    ),
                )

    def test_every_required_identity_claim_is_required(self) -> None:
        for claim in (
            "iss",
            "aud",
            "sub",
            "session_id",
            "role",
            "is_anonymous",
            "iat",
            "exp",
        ):
            claims = self.claims()
            del claims[claim]
            with self.subTest(claim=claim):
                self.assert_error(
                    "auth_required_claim_missing",
                    lambda claims=claims: self.verify(self.token(claims=claims)),
                )

    def test_claim_types_uuid_forms_and_token_times_are_strict(self) -> None:
        cases = (
            ("auth_subject_invalid", {"sub": str(OWNER_A).upper()}),
            ("auth_subject_invalid", {"sub": 123}),
            ("auth_session_id_invalid", {"session_id": "not-a-uuid"}),
            ("auth_anonymous_claim_invalid", {"is_anonymous": 0}),
            ("auth_iat_invalid", {"iat": float(NOW_SECONDS - 1)}),
            ("auth_exp_invalid", {"exp": str(NOW_SECONDS + 300)}),
            ("auth_token_not_yet_valid", {"iat": NOW_SECONDS + 1}),
            (
                "auth_token_lifetime_invalid",
                {"iat": NOW_SECONDS - 1, "exp": NOW_SECONDS - 1},
            ),
            ("auth_token_expired", {"exp": NOW_SECONDS}),
            ("auth_nbf_invalid", {"nbf": "later"}),
            ("auth_token_not_yet_valid", {"nbf": NOW_SECONDS + 1}),
        )
        for code, override in cases:
            with self.subTest(code=code, override=override):
                self.assert_error(
                    code,
                    lambda override=override: self.verify(
                        self.token(claims=self.claims(**override))
                    ),
                )

    def test_signature_header_and_key_failures_are_stable(self) -> None:
        bad_signature = self.token(private_key=self.other_rsa_private)
        self.assert_error(
            "auth_signature_invalid", lambda: self.verify(bad_signature)
        )
        self.assert_error(
            "auth_key_id_invalid",
            lambda: self.verify(self.token(headers={"kid": "bad kid"})),
        )
        self.assert_error(
            "auth_token_header_invalid",
            lambda: self.verify(self.token(headers={"typ": "JOSE"})),
        )
        self.assert_error(
            "auth_token_header_invalid",
            lambda: self.verify(self.token(headers={"crit": ["unsupported"]})),
        )
        self.assert_error(
            "auth_token_malformed", lambda: self.verify("aaa.bbb.ccc")
        )

    def test_resolver_absence_unavailability_and_unknown_failure_are_distinct(self) -> None:
        absent = SupabaseJwtVerifier(
            ISSUER,
            AUDIENCE,
            lambda _kid, _alg: None,
            lambda: NOW,
        )
        unavailable = SupabaseJwtVerifier(
            ISSUER,
            AUDIENCE,
            lambda _kid, _alg: (_ for _ in ()).throw(KeyResolverUnavailable()),
            lambda: NOW,
        )
        failed = SupabaseJwtVerifier(
            ISSUER,
            AUDIENCE,
            lambda _kid, _alg: (_ for _ in ()).throw(RuntimeError()),
            lambda: NOW,
        )
        authorization = self.authorization()
        for code, verifier in (
            ("auth_key_not_found", absent),
            ("auth_key_resolution_unavailable", unavailable),
            ("auth_key_resolver_failed", failed),
        ):
            with self.subTest(code=code):
                self.assert_error(
                    code,
                    lambda verifier=verifier: verifier.verify_bearer(
                        authorization,
                        scopes=(ActorScope.READ_CLAIMS,),
                    ),
                )

    def test_bearer_parsing_is_exact_and_scheme_is_rfc_case_insensitive(self) -> None:
        token = self.token()
        actor = self.verifier.verify_bearer(
            f"bearer {token}",
            scopes=(ActorScope.READ_CLAIMS,),
        )
        self.assertEqual(actor.owner_user_id, OWNER_A)
        for authorization in (
            None,
            "",
            token,
            f"Bearer  {token}",
            f"Bearer {token} trailing",
            f"Basic {token}",
        ):
            with self.subTest(authorization=authorization):
                self.assert_error(
                    "auth_header_missing"
                    if authorization is None
                    else "auth_header_invalid",
                    lambda authorization=authorization: self.verifier.verify_bearer(
                        authorization,
                        scopes=(ActorScope.READ_CLAIMS,),
                    ),
                )

    def request_authenticator(self) -> GovernedMemoryRequestAuthenticator:
        return GovernedMemoryRequestAuthenticator(
            verifier=self.verifier,
            expected_service_token=SERVICE_TOKEN,
        )

    def valid_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.authorization(),
            "X-VS-Service-Token": SERVICE_TOKEN,
        }

    def test_request_authenticator_repr_redacts_service_token(self) -> None:
        rendered = repr(self.request_authenticator())
        self.assertNotIn(SERVICE_TOKEN, rendered)
        self.assertNotIn("expected_service_token", rendered)

    def test_request_boundary_requires_constant_time_service_token_comparison(self) -> None:
        authenticator = self.request_authenticator()
        headers = self.valid_headers()
        with patch(
            "rag_engine.governed_memory.http_auth.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as compare:
            actor = authenticator.authenticate(
                headers,
                scopes=(ActorScope.READ_CLAIMS,),
            )
        self.assertEqual(actor.owner_user_id, OWNER_A)
        compare.assert_called_once_with(
            SERVICE_TOKEN.encode("utf-8"), SERVICE_TOKEN.encode("utf-8")
        )

        missing = self.valid_headers()
        del missing["X-VS-Service-Token"]
        self.assert_error(
            "auth_service_token_missing",
            lambda: authenticator.authenticate(
                missing, scopes=(ActorScope.READ_CLAIMS,)
            ),
        )
        wrong = self.valid_headers()
        wrong["X-VS-Service-Token"] = "wrong"
        self.assert_error(
            "auth_service_token_invalid",
            lambda: authenticator.authenticate(
                wrong, scopes=(ActorScope.READ_CLAIMS,)
            ),
        )

    def test_request_boundary_rejects_explicit_owner_or_actor_headers(self) -> None:
        authenticator = self.request_authenticator()
        for name in (
            "X-Owner-User-ID",
            "X-Memory-Owner-ID",
            "X-VS-Actor-User-ID",
            "X-Custom-Owner-Reference",
            "Owner-User-ID",
        ):
            headers = self.valid_headers()
            headers[name] = str(OWNER_B)
            with self.subTest(name=name):
                self.assert_error(
                    "auth_explicit_authority_header_forbidden",
                    lambda headers=headers: authenticator.authenticate(
                        headers,
                        scopes=(ActorScope.READ_CLAIMS,),
                    ),
                )

    def test_case_variant_duplicate_headers_are_rejected(self) -> None:
        headers = self.valid_headers()
        headers["authorization"] = headers["Authorization"]
        self.assert_error(
            "auth_header_ambiguous",
            lambda: self.request_authenticator().authenticate(
                headers,
                scopes=(ActorScope.READ_CLAIMS,),
            ),
        )


if __name__ == "__main__":
    unittest.main()
