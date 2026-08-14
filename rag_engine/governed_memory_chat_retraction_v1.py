from __future__ import annotations

"""Closed normal-chat adapter for explicit owner preference retractions."""

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
import unicodedata
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from rag_engine.governed_memory_erasure_proxy_v1 import (
    ErasureProxyTransport,
    ProxyResult,
    UnixSocketErasureTransport,
    governed_memory_proxy_service_token_is_valid,
)


SERVICE_TOKEN_ENV = "GOVERNED_MEMORY_SERVICE_TOKEN"
MAX_MESSAGE_BYTES = 2_048
MAX_PREFERENCE_CLAIMS = 32
MAX_RESPONSE_BODY_BYTES = 131_072
REQUEST_DEADLINE_SECONDS = 16.0
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_BEARER_RE = re.compile(
    r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
    re.ASCII | re.IGNORECASE,
)
_RETRACTION_SENTENCE_RE = re.compile(
    r"(?:^|[.!]\s+)(?:please\s+)?retract\s+(?:the|my)\s+"
    r"(?P<target>[^.!?\n]{1,160}?)\s+preference\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
_LIST_KEYS = frozenset(
    {
        "claim_id",
        "lifecycle_state",
        "revision_id",
        "revision_number",
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "object_kind",
        "predicate",
        "epistemic_state",
        "sensitivity",
        "updated_at",
    }
)
_DETAIL_KEYS = _LIST_KEYS | {
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
}


class ChatRetractionError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatRetractionReceiptV1:
    claim_id: UUID
    outcome: str


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def explicit_preference_retraction_target_v1(message: object) -> str | None:
    """Return the explicit preference target, or None for an ordinary turn."""

    if not isinstance(message, str):
        return None
    try:
        size = len(message.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if not message or size > MAX_MESSAGE_BYTES or "\x00" in message:
        return None
    if "?" in message or "\n" in message or "\r" in message:
        return None
    matches = tuple(_RETRACTION_SENTENCE_RE.finditer(message.strip()))
    if len(matches) != 1:
        return None
    target = _normalized_text(matches[0].group("target"))
    if not target or len(target) > 160:
        return None
    return target


def retraction_operation_id_v1(*, claim_id: UUID, message: str) -> UUID:
    normalized_sha256 = sha256(
        _normalized_text(message).encode("utf-8")
    ).hexdigest()
    return uuid5(
        NAMESPACE_URL,
        f"governed-memory:chat-retraction-v1:{claim_id}:{normalized_sha256}",
    )


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _closed_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _decode_json(result: ProxyResult) -> object:
    content_type = (
        result.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if (
        result.status_code != 200
        or content_type != "application/json"
        or len(result.body) > MAX_RESPONSE_BODY_BYTES
    ):
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    try:
        return json.loads(
            result.body,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ChatRetractionError("memory_retraction_unavailable", 503) from None


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    try:
        parsed = UUID(value)
    except ValueError:
        raise ChatRetractionError("memory_retraction_unavailable", 503) from None
    if str(parsed) != value:
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    return parsed


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    return value


def _validate_list_item(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _LIST_KEYS:
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    _uuid(value["claim_id"])
    _uuid(value["revision_id"])
    for name in (
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
    ):
        _sha256(value[name])
    if (
        type(value["revision_number"]) is not int
        or value["revision_number"] < 1
        or not isinstance(value["lifecycle_state"], str)
        or not isinstance(value["predicate"], str)
        or not isinstance(value["object_kind"], str)
    ):
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    return value


def _validate_detail(
    value: object,
    *,
    summary: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _DETAIL_KEYS:
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    for name in _LIST_KEYS:
        if value[name] != summary[name]:
            raise ChatRetractionError("memory_retraction_unavailable", 503)
    kind = value["object_kind"]
    literal = value["object_literal"]
    display_name = value["object_display_name"]
    if kind == "literal":
        if (
            not isinstance(literal, str)
            or not literal
            or len(literal.encode("utf-8")) > 4_096
            or "\x00" in literal
            or value["object_entity_type"] is not None
            or value["object_entity_key"] is not None
            or display_name is not None
        ):
            raise ChatRetractionError("memory_retraction_unavailable", 503)
    elif kind == "entity":
        if (
            literal is not None
            or not isinstance(value["object_entity_type"], str)
            or not value["object_entity_type"]
            or not isinstance(value["object_entity_key"], str)
            or not value["object_entity_key"]
            or not isinstance(display_name, str)
            or not display_name
            or len(display_name.encode("utf-8")) > 4_096
            or "\x00" in display_name
        ):
            raise ChatRetractionError("memory_retraction_unavailable", 503)
    else:
        raise ChatRetractionError("memory_retraction_unavailable", 503)
    return value


def _claim_target_text(detail: Mapping[str, object]) -> str:
    if detail["object_kind"] == "literal":
        return str(detail["object_literal"])
    return str(detail["object_display_name"])


def _target_contains_claim_value(*, target: str, claim_value: str) -> bool:
    normalized_value = _normalized_text(claim_value)
    if not normalized_value:
        return False
    return re.search(
        rf"(?<!\w){re.escape(normalized_value)}(?!\w)",
        target,
        re.UNICODE,
    ) is not None


class ChatRetractionRuntimeV1:
    def __init__(
        self,
        *,
        service_token: str | None,
        transport: ErasureProxyTransport | None = None,
    ) -> None:
        self._service_token = (
            service_token
            if governed_memory_proxy_service_token_is_valid(service_token)
            else ""
        )
        self._transport = transport or UnixSocketErasureTransport()

    @classmethod
    def from_environment(cls) -> "ChatRetractionRuntimeV1":
        return cls(service_token=os.getenv(SERVICE_TOKEN_ENV))

    async def _request(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        body: bytes = b"",
    ) -> object:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._transport.request,
                    method=method,
                    path=path,
                    authorization=authorization,
                    service_token=self._service_token,
                    body=body,
                ),
                timeout=REQUEST_DEADLINE_SECONDS,
            )
        except ChatRetractionError:
            raise
        except (asyncio.TimeoutError, OSError, ValueError):
            raise ChatRetractionError("memory_retraction_unavailable", 503) from None
        return _decode_json(result)

    async def apply_if_requested(
        self,
        *,
        message: str,
        authorization: str,
    ) -> ChatRetractionReceiptV1 | None:
        target = explicit_preference_retraction_target_v1(message)
        if target is None:
            return None
        if not self._service_token:
            raise ChatRetractionError("memory_retraction_unconfigured", 503)
        if _BEARER_RE.fullmatch(authorization) is None:
            raise ChatRetractionError("memory_authentication_required", 401)

        raw_claims = await self._request(
            method="GET",
            path="/memory/claims",
            authorization=authorization,
        )
        if not isinstance(raw_claims, list):
            raise ChatRetractionError("memory_retraction_unavailable", 503)
        summaries: list[Mapping[str, object]] = []
        seen_ids: set[UUID] = set()
        for raw in raw_claims:
            summary = _validate_list_item(raw)
            claim_id = _uuid(summary["claim_id"])
            if claim_id in seen_ids:
                raise ChatRetractionError("memory_retraction_unavailable", 503)
            seen_ids.add(claim_id)
            if (
                summary["predicate"] == "preference.personal"
                and summary["object_kind"] in {"literal", "entity"}
                and summary["lifecycle_state"] in {"active", "retracted"}
            ):
                summaries.append(summary)
        if len(summaries) > MAX_PREFERENCE_CLAIMS:
            raise ChatRetractionError("memory_retraction_target_ambiguous", 409)

        active: list[Mapping[str, object]] = []
        retracted: list[Mapping[str, object]] = []
        for summary in summaries:
            claim_id = _uuid(summary["claim_id"])
            detail = _validate_detail(
                await self._request(
                    method="GET",
                    path=f"/memory/claims/{claim_id}",
                    authorization=authorization,
                ),
                summary=summary,
            )
            if not _target_contains_claim_value(
                target=target,
                claim_value=_claim_target_text(detail),
            ):
                continue
            if summary["lifecycle_state"] == "active":
                active.append(detail)
            else:
                retracted.append(detail)

        if len(active) > 1 or (not active and len(retracted) > 1):
            raise ChatRetractionError("memory_retraction_target_ambiguous", 409)
        if not active:
            if len(retracted) == 1:
                return ChatRetractionReceiptV1(
                    claim_id=_uuid(retracted[0]["claim_id"]),
                    outcome="replayed",
                )
            raise ChatRetractionError("memory_retraction_target_not_found", 409)

        claim = active[0]
        claim_id = _uuid(claim["claim_id"])
        operation_id = retraction_operation_id_v1(
            claim_id=claim_id,
            message=message,
        )
        body = json.dumps(
            {
                "expected_revision_sha256": _sha256(claim["revision_sha256"]),
                "expected_state_sha256": _sha256(claim["current_state_sha256"]),
                "operation_id": str(operation_id),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        receipt = await self._request(
            method="POST",
            path=f"/memory/claims/{claim_id}/retract",
            authorization=authorization,
            body=body,
        )
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "outbox_id"}
            or receipt["outcome"] not in {"retracted", "replayed"}
            or not isinstance(receipt["outbox_id"], str)
        ):
            raise ChatRetractionError("memory_retraction_unavailable", 503)
        _uuid(receipt["outbox_id"])
        return ChatRetractionReceiptV1(
            claim_id=claim_id,
            outcome=str(receipt["outcome"]),
        )


__all__ = [
    "ChatRetractionError",
    "ChatRetractionReceiptV1",
    "ChatRetractionRuntimeV1",
    "explicit_preference_retraction_target_v1",
    "retraction_operation_id_v1",
]
