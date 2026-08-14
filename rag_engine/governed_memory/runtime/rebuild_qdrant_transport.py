from __future__ import annotations

"""Bounded loopback transport for successor rebuild administration only."""

import asyncio
from dataclasses import dataclass, field
import http.client
import json
import math
import re
from typing import Any, Mapping

from ..contracts import ContractViolation, require_exact_int
from .qdrant_transport import (
    MAX_QDRANT_REQUEST_BYTES,
    MAX_QDRANT_RESPONSE_BYTES,
    QDRANT_RUNTIME_HOST,
    QDRANT_RUNTIME_PORT,
    QDRANT_RUNTIME_URL,
)


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COLLECTION = r"governed_memory_9a54cf123493_[0-9]{6}"
_PATH_RE = re.compile(
    rf"(?:/collections/aliases|/collections/{_COLLECTION}"
    rf"(?:|/aliases|/index\?wait=true|/points|/points\?wait=true|/points/scroll))\Z",
    re.ASCII,
)
_METHOD_PATHS = {
    "GET": re.compile(rf"/collections/{_COLLECTION}(?:|/aliases)\Z", re.ASCII),
    "PUT": re.compile(
        rf"/collections/{_COLLECTION}(?:|/index\?wait=true|/points\?wait=true)\Z",
        re.ASCII,
    ),
    "POST": re.compile(
        rf"(?:/collections/aliases|/collections/{_COLLECTION}/points(?:|/scroll))\Z",
        re.ASCII,
    ),
}


class RebuildQdrantTransportFailure(RuntimeError):
    """Stable, content-free failure before a successful response."""


class RebuildQdrantOutcomeUnknown(RebuildQdrantTransportFailure):
    """A rebuild mutation may have reached Qdrant."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RebuildQdrantResponse:
    status: int
    body: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        require_exact_int(
            self.status,
            code="invalid_rebuild_qdrant_status",
            minimum=100,
            maximum=599,
        )
        if not isinstance(self.body, Mapping):
            raise ContractViolation("invalid_rebuild_qdrant_body")


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _wire(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ContractViolation("rebuild_qdrant_nonfinite_number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractViolation("rebuild_qdrant_non_string_key")
            result[key] = _wire(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    raise ContractViolation("rebuild_qdrant_unsupported_wire_value")


class LoopbackRebuildQdrantTransport:
    """Exact successor-generation paths; no discovery, redirect, or retry."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        connection_factory: Any = None,
    ) -> None:
        if base_url != QDRANT_RUNTIME_URL:
            raise ContractViolation("rebuild_qdrant_target_mismatch")
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or len(api_key.encode("utf-8")) > 1_024
            or _CONTROL_RE.search(api_key)
        ):
            raise ContractViolation("rebuild_qdrant_api_key_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60
        ):
            raise ContractViolation("rebuild_qdrant_timeout_invalid")
        self._api_key = api_key
        self._timeout_seconds = float(timeout_seconds)
        self._connection_factory = connection_factory or http.client.HTTPConnection

    @staticmethod
    def _target(method: object, path: object) -> tuple[str, str, bool]:
        normalized = method.upper() if isinstance(method, str) else ""
        if (
            normalized not in _METHOD_PATHS
            or not isinstance(path, str)
            or not _PATH_RE.fullmatch(path)
            or not _METHOD_PATHS[normalized].fullmatch(path)
        ):
            raise RebuildQdrantTransportFailure(
                "rebuild_qdrant_request_rejected_before_send"
            )
        return normalized, path, (
            normalized == "PUT" or path == "/collections/aliases"
        )

    @staticmethod
    def _body(body: Mapping[str, object] | None) -> bytes | None:
        if body is None:
            return None
        try:
            encoded = json.dumps(
                _wire(body),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (ContractViolation, TypeError, ValueError) as exc:
            raise RebuildQdrantTransportFailure(
                "rebuild_qdrant_request_rejected_before_send"
            ) from exc
        if not encoded or len(encoded) > MAX_QDRANT_REQUEST_BYTES:
            raise RebuildQdrantTransportFailure(
                "rebuild_qdrant_request_rejected_before_send"
            )
        return encoded

    def _request_sync(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        accepted_statuses: frozenset[int],
    ) -> RebuildQdrantResponse:
        normalized, checked_path, is_write = self._target(method, path)
        if (
            not isinstance(accepted_statuses, frozenset)
            or not accepted_statuses
            or any(
                not isinstance(status, int) or not 200 <= status <= 499
                for status in accepted_statuses
            )
        ):
            raise RebuildQdrantTransportFailure(
                "rebuild_qdrant_status_contract_invalid"
            )
        encoded = self._body(body)
        headers = {"Accept": "application/json", "api-key": self._api_key}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        connection = None
        dispatched = False
        try:
            connection = self._connection_factory(
                QDRANT_RUNTIME_HOST,
                port=QDRANT_RUNTIME_PORT,
                timeout=self._timeout_seconds,
            )
            dispatched = is_write
            connection.request(normalized, checked_path, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
            if len(raw) > MAX_QDRANT_RESPONSE_BYTES:
                raise RebuildQdrantTransportFailure(
                    "rebuild_qdrant_response_too_large"
                )
            if response.status not in accepted_statuses:
                raise RebuildQdrantTransportFailure(
                    "rebuild_qdrant_response_status_rejected"
                )
            content_type = response.getheader("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise RebuildQdrantTransportFailure(
                    "rebuild_qdrant_response_not_json"
                )
            try:
                value = json.loads(
                    raw,
                    object_pairs_hook=_closed_json_object,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("nonfinite_json_number")
                    ),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise RebuildQdrantTransportFailure(
                    "rebuild_qdrant_response_json_invalid"
                ) from exc
            if not isinstance(value, Mapping):
                raise RebuildQdrantTransportFailure(
                    "rebuild_qdrant_response_json_invalid"
                )
            return RebuildQdrantResponse(status=response.status, body=value)
        except RebuildQdrantOutcomeUnknown:
            raise
        except RebuildQdrantTransportFailure as exc:
            if is_write and dispatched:
                raise RebuildQdrantOutcomeUnknown(
                    "rebuild_qdrant_write_outcome_unknown"
                ) from exc
            raise
        except Exception as exc:
            if is_write and dispatched:
                raise RebuildQdrantOutcomeUnknown(
                    "rebuild_qdrant_write_outcome_unknown"
                ) from exc
            raise RebuildQdrantTransportFailure(
                "rebuild_qdrant_unavailable"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        accepted_statuses: frozenset[int] = frozenset({200}),
    ) -> RebuildQdrantResponse:
        return await asyncio.to_thread(
            self._request_sync,
            method,
            path,
            body,
            accepted_statuses,
        )


__all__ = [
    "LoopbackRebuildQdrantTransport",
    "RebuildQdrantOutcomeUnknown",
    "RebuildQdrantResponse",
    "RebuildQdrantTransportFailure",
]
