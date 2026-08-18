from __future__ import annotations

"""Contained HTTP transport for governed realtime voice turns."""

from typing import Any, Callable

import httpx


BRAINS_INTERNAL_BASE_URL = "http://127.0.0.1:8088"


class GovernedVoiceTurnTimeoutError(RuntimeError):
    pass


class GovernedVoiceTurnUnavailableError(RuntimeError):
    pass


class GovernedVoiceTurnHTTPTransport:
    def __init__(
        self,
        *,
        internal_base_url: str | None = None,
        http_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._internal_base_url = (
            internal_base_url or BRAINS_INTERNAL_BASE_URL
        ).rstrip("/")
        self._http_client_factory = (
            http_client_factory or httpx.AsyncClient
        )
        self._client_context: Any | None = None
        self._client: Any | None = None

    async def __aenter__(self) -> "GovernedVoiceTurnHTTPTransport":
        context = self._http_client_factory(
            timeout=httpx.Timeout(100.0, connect=5.0),
        )
        self._client_context = context
        self._client = await context.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        context = self._client_context
        self._client = None
        self._client_context = None
        if context is not None:
            await context.__aexit__(*args)

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("governed voice transport is not open")
        return self._client

    async def _post(
        self,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        try:
            return await self._require_client().post(
                f"{self._internal_base_url}{path}",
                headers=headers,
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise GovernedVoiceTurnTimeoutError(
                "governed_response_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise GovernedVoiceTurnUnavailableError(
                "governed_response_unavailable"
            ) from exc

    async def execute_search(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        return await self._post(
            "/search/execute",
            headers=headers,
            payload=payload,
        )

    async def persist_transcript(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        return await self._post(
            "/log",
            headers=headers,
            payload=payload,
        )

    async def generate_response(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        return await self._post(
            "/response/query",
            headers=headers,
            payload=payload,
        )


__all__ = [
    "GovernedVoiceTurnHTTPTransport",
    "GovernedVoiceTurnTimeoutError",
    "GovernedVoiceTurnUnavailableError",
]
