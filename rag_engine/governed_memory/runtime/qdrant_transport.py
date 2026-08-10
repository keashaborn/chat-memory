from __future__ import annotations

"""Bounded loopback HTTP transport for the exact successor Qdrant target."""

import asyncio
import http.client
import json
import re
from typing import Any, Mapping

from ..contracts import ContractViolation, canonical_json_bytes
from .qdrant_adapter import QdrantWriteOutcomeUnknown


QDRANT_RUNTIME_URL = "http://127.0.0.1:6343"
QDRANT_RUNTIME_HOST = "127.0.0.1"
QDRANT_RUNTIME_PORT = 6343
MAX_QDRANT_REQUEST_BYTES = 16_777_216
MAX_QDRANT_RESPONSE_BYTES = 16_777_216
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT"})


class QdrantTransportFailure(RuntimeError):
    """Stable, content-free Qdrant transport failure."""


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class LoopbackQdrantTransport:
    """No discovery, proxy, redirect, retry, create, or alternate target path."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        if base_url != QDRANT_RUNTIME_URL:
            raise ContractViolation("qdrant_runtime_target_mismatch")
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or len(api_key.encode("utf-8")) > 1_024
            or _CONTROL_RE.search(api_key)
        ):
            raise ContractViolation("qdrant_runtime_api_key_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60
        ):
            raise ContractViolation("qdrant_runtime_timeout_invalid")
        self._api_key = api_key
        self._timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _request_body(body: Mapping[str, object] | None) -> bytes | None:
        if body is None:
            return None
        try:
            encoded = canonical_json_bytes(body)
        except ContractViolation as error:
            raise QdrantTransportFailure(
                "qdrant_request_rejected_before_send"
            ) from error
        if not encoded or len(encoded) > MAX_QDRANT_REQUEST_BYTES:
            raise QdrantTransportFailure("qdrant_request_rejected_before_send")
        return encoded

    @staticmethod
    def _validate_target(method: str, path: str) -> tuple[str, str]:
        normalized_method = method.upper() if isinstance(method, str) else ""
        if normalized_method not in _ALLOWED_METHODS:
            raise QdrantTransportFailure("qdrant_method_rejected_before_send")
        if (
            not isinstance(path, str)
            or not path.startswith("/collections/")
            or "#" in path
            or _CONTROL_RE.search(path)
            or len(path.encode("ascii", errors="ignore")) != len(path)
            or len(path) > 512
        ):
            raise QdrantTransportFailure("qdrant_path_rejected_before_send")
        if "?" in path and not path.endswith("?wait=true"):
            raise QdrantTransportFailure("qdrant_path_rejected_before_send")
        return normalized_method, path

    @staticmethod
    def _is_write(method: str, path: str) -> bool:
        return (
            method == "PUT" and path.endswith("/points?wait=true")
        ) or (
            method == "POST" and path.endswith("/points/delete?wait=true")
        )

    def _request_sync(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
    ) -> Mapping[str, Any]:
        normalized_method, checked_path = self._validate_target(method, path)
        is_write = self._is_write(normalized_method, checked_path)
        encoded = self._request_body(body)
        headers = {
            "Accept": "application/json",
            "api-key": self._api_key,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            QDRANT_RUNTIME_HOST,
            port=QDRANT_RUNTIME_PORT,
            timeout=self._timeout_seconds,
        )
        dispatched = False
        try:
            # For a write, any failure from request() is ambiguous: http.client
            # may have transmitted some or all bytes before raising.  Mark the
            # uncertainty boundary before entering the socket write.
            dispatched = is_write
            connection.request(
                normalized_method,
                checked_path,
                body=encoded,
                headers=headers,
            )
            response = connection.getresponse()
            response_body = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_QDRANT_RESPONSE_BYTES:
                raise QdrantTransportFailure("qdrant_response_too_large")
            if response.status < 200 or response.status >= 300:
                if is_write:
                    raise QdrantWriteOutcomeUnknown(
                        "qdrant_write_response_not_successful"
                    )
                raise QdrantTransportFailure("qdrant_read_response_not_successful")
            content_type = response.getheader("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise QdrantTransportFailure("qdrant_response_not_json")
            try:
                value = json.loads(
                    response_body,
                    object_pairs_hook=_closed_json_object,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("nonfinite_json_number")
                    ),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise QdrantTransportFailure("qdrant_response_json_invalid") from error
            if not isinstance(value, Mapping):
                raise QdrantTransportFailure("qdrant_response_json_invalid")
            return value
        except QdrantWriteOutcomeUnknown:
            raise
        except QdrantTransportFailure as error:
            if is_write and dispatched:
                raise QdrantWriteOutcomeUnknown(
                    "qdrant_write_outcome_unknown"
                ) from error
            raise
        except Exception as error:
            if is_write and dispatched:
                raise QdrantWriteOutcomeUnknown(
                    "qdrant_write_outcome_unknown"
                ) from error
            raise QdrantTransportFailure("qdrant_unavailable") from error
        finally:
            try:
                connection.close()
            except Exception:
                pass

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._request_sync, method, path, body)


__all__ = [
    "LoopbackQdrantTransport",
    "MAX_QDRANT_REQUEST_BYTES",
    "MAX_QDRANT_RESPONSE_BYTES",
    "QDRANT_RUNTIME_HOST",
    "QDRANT_RUNTIME_PORT",
    "QDRANT_RUNTIME_URL",
    "QdrantTransportFailure",
]
