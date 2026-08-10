from __future__ import annotations

"""Composition root for the separate governed-Memory HTTP candidate.

Importing this module and constructing the default off-mode application do not
open sockets, create a database pool, fetch signing keys, or contact Supabase.
The only executable server bind is the candidate's private seebx interface.
"""

from collections.abc import Callable, Mapping, Sequence
import argparse
import json
import os
import sys
from typing import Any

from fastapi import FastAPI

from ..http_service import (
    GovernedMemoryHttpServiceSettings,
    HttpServiceConfigurationError,
    create_governed_memory_http_service,
)
from .live_supabase import (
    LiveSupabaseAuthorityConfig,
    LiveSupabaseAuthorityVerifier,
    LiveSupabaseSessionVerifier,
    LiveSupabaseUserVerifier,
    SessionFetcher,
    UserFetcher,
)


API_BIND_HOST = "172.31.32.171"
API_BIND_PORT = 8091
FRONTEND_SOURCE_IPV4 = "172.31.43.160/32"
SUPABASE_API_KEY_ENV = "GOVERNED_MEMORY_SUPABASE_API_KEY"

HttpServiceFactory = Callable[..., FastAPI]
ServerRunner = Callable[..., Any]


def _required_secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise HttpServiceConfigurationError("governed_memory_secret_invalid")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HttpServiceConfigurationError(
            "governed_memory_secret_invalid"
        ) from exc
    if len(encoded) > 16_384 or any(byte < 33 or byte > 126 for byte in encoded):
        raise HttpServiceConfigurationError("governed_memory_secret_invalid")
    return value


def create_runtime_application(
    environment: Mapping[str, str] | None = None,
    *,
    user_fetcher: UserFetcher | None = None,
    session_fetcher: SessionFetcher | None = None,
    http_service_factory: HttpServiceFactory = create_governed_memory_http_service,
) -> FastAPI:
    """Compose the service without performing network or database I/O."""

    values = environment if environment is not None else os.environ
    settings = GovernedMemoryHttpServiceSettings.from_environment(values)
    if settings.mode == "off":
        return http_service_factory(settings=settings)

    if settings.supabase_issuer is None:
        raise HttpServiceConfigurationError(
            "governed_memory_supabase_configuration_invalid"
        )
    api_key = _required_secret(values, SUPABASE_API_KEY_ENV)
    config = LiveSupabaseAuthorityConfig(
        issuer=settings.supabase_issuer,
        api_key=api_key,
    )
    user_verifier_kwargs: dict[str, object] = {}
    if user_fetcher is not None:
        user_verifier_kwargs["fetcher"] = user_fetcher
    session_verifier_kwargs: dict[str, object] = {}
    if session_fetcher is not None:
        session_verifier_kwargs["fetcher"] = session_fetcher
    authority_verifier = LiveSupabaseAuthorityVerifier(
        user_verifier=LiveSupabaseUserVerifier(
            config,
            **user_verifier_kwargs,
        ),
        session_verifier=LiveSupabaseSessionVerifier(
            config,
            **session_verifier_kwargs,
        ),
    )
    return http_service_factory(
        settings=settings,
        authority_verifier=authority_verifier,
    )


def _contract_json() -> str:
    return json.dumps(
        {
            "api_bind": f"{API_BIND_HOST}:{API_BIND_PORT}",
            "default_mode": "off",
            "frontend_source_ipv4": FRONTEND_SOURCE_IPV4,
            "production_authorized": False,
            "schema_version": "governed-memory-runtime-entrypoint-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    server_runner: ServerRunner | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="governed-memory-http")
    parser.add_argument(
        "--print-contract",
        action="store_true",
        help="print the content-free bind contract without constructing an app",
    )
    arguments = parser.parse_args(argv)
    if arguments.print_contract:
        print(_contract_json())
        return 0

    values = environment if environment is not None else os.environ
    settings = GovernedMemoryHttpServiceSettings.from_environment(values)
    if settings.mode != "on":
        print(
            json.dumps(
                {
                    "error": {"code": "governed_memory_http_disabled"},
                    "schema_version": "governed-memory-runtime-refusal-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    application = create_runtime_application(values)
    if server_runner is None:
        import uvicorn

        server_runner = uvicorn.run
    server_runner(
        application,
        host=API_BIND_HOST,
        port=API_BIND_PORT,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        workers=1,
    )
    return 0


__all__ = [
    "API_BIND_HOST",
    "API_BIND_PORT",
    "FRONTEND_SOURCE_IPV4",
    "SUPABASE_API_KEY_ENV",
    "create_runtime_application",
    "main",
]
