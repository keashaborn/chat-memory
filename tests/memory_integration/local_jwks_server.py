from __future__ import annotations

"""Invocation-local JWKS authority for the disposable successor test only."""

import base64
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import ec
import jwt


JWKS_HOST = "127.0.0.1"
JWKS_PORT = 18091
JWKS_ISSUER = f"http://{JWKS_HOST}:{JWKS_PORT}/auth/v1"
JWKS_PATH = "/auth/v1/.well-known/jwks.json"
JWKS_URL = f"{JWKS_ISSUER}/.well-known/jwks.json"


def _base64url(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode(
        "ascii"
    )


class _LoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalJwksAuthority:
    """Own one ephemeral ES256 key and publish only its public JWK."""

    def __init__(self, *, invocation_id: UUID) -> None:
        if not isinstance(invocation_id, UUID):
            raise ValueError("invalid_invocation_id")
        self.invocation_id = invocation_id
        self.key_id = f"successor-{invocation_id.hex[:16]}"
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        numbers = self._private_key.public_key().public_numbers()
        self.jwk = {
            "alg": "ES256",
            "crv": "P-256",
            "kid": self.key_id,
            "kty": "EC",
            "use": "sig",
            "x": _base64url(numbers.x),
            "y": _base64url(numbers.y),
        }
        self.document = {"keys": [self.jwk]}
        self._fetch_count = 0
        self._fetch_lock = Lock()
        authority = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path == "/healthz":
                    payload = json.dumps(
                        {
                            "invocation_id": str(authority.invocation_id),
                            "status": "ok",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                elif self.path == JWKS_PATH:
                    with authority._fetch_lock:
                        authority._fetch_count += 1
                    payload = json.dumps(
                        authority.document,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self._server = _LoopbackServer((JWKS_HOST, JWKS_PORT), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name=f"successor-jwks-{invocation_id.hex[:12]}",
            daemon=True,
        )

    @property
    def fetch_count(self) -> int:
        with self._fetch_lock:
            return self._fetch_count

    def start(self) -> None:
        self._thread.start()
        request = Request(
            f"http://{JWKS_HOST}:{JWKS_PORT}/healthz",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("local_jwks_not_ready")
        except (HTTPError, URLError, TimeoutError) as exc:
            self.close()
            raise RuntimeError("local_jwks_not_ready") from exc

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("local_jwks_thread_not_stopped")

    def token(
        self,
        subject: UUID,
        *,
        session_id: UUID | None = None,
        claims: Mapping[str, Any] | None = None,
        omit_claims: tuple[str, ...] = (),
        key_id: str | None = None,
        private_key: object | None = None,
        algorithm: str = "ES256",
    ) -> str:
        if not isinstance(subject, UUID):
            raise ValueError("invalid_subject")
        now = int(datetime.now(UTC).timestamp())
        material: dict[str, Any] = {
            "aud": "authenticated",
            "exp": now + 300,
            "iat": now - 5,
            "is_anonymous": False,
            "iss": JWKS_ISSUER,
            "role": "authenticated",
            "session_id": str(session_id or uuid4()),
            "sub": str(subject),
        }
        if claims:
            material.update(dict(claims))
        if (
            not isinstance(omit_claims, tuple)
            or len(set(omit_claims)) != len(omit_claims)
            or any(
                not isinstance(name, str) or name not in material
                for name in omit_claims
            )
        ):
            raise ValueError("invalid_omitted_claims")
        for name in omit_claims:
            del material[name]
        return jwt.encode(
            material,
            private_key or self._private_key,
            algorithm=algorithm,
            headers={"kid": key_id or self.key_id, "typ": "JWT"},
        )


__all__ = [
    "JWKS_HOST",
    "JWKS_ISSUER",
    "JWKS_PATH",
    "JWKS_PORT",
    "JWKS_URL",
    "LocalJwksAuthority",
]
