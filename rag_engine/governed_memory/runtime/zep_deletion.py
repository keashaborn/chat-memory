from __future__ import annotations

"""Content-free, idempotent Zep erasure for governed chat deletion."""

import asyncio
from dataclasses import dataclass, field
import http.client
import json
import re
import ssl
from typing import Protocol
from urllib.parse import quote, urlencode
from uuid import UUID

from ..contracts import ContractViolation
from ..deletion_contracts import (
    ConversationErasureLease,
    ConversationErasureTarget,
    DeletionSelectorKind,
)


ZEP_API_HOST = "api.getzep.com"
ZEP_API_PREFIX = "/api/v2"
ZEP_MESSAGE_ID_METADATA_KEY = "lifeswitch_message_id"
ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT = 20
ZEP_THREAD_PAGE_SIZE = 100
ZEP_MAX_THREAD_MESSAGES = 100_000
_MAX_RESPONSE_BYTES = 2_097_152
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ZepDeletionUnavailable(RuntimeError):
    """A retryable provider or network failure with a content-free code."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ZepHttpResponse:
    status: int
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ContractViolation("invalid_zep_http_status")
        if not isinstance(self.body, bytes):
            raise ContractViolation("invalid_zep_http_body")


class ZepHttpTransport(Protocol):
    def request(self, *, method: str, path: str) -> ZepHttpResponse: ...


class BoundedZepHttpsTransport:
    """One verified-TLS request per call; no redirects, retries, or proxies."""

    def __init__(self, *, api_key: str, timeout_seconds: float = 12.0) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or len(api_key.encode("utf-8")) > 16_384
            or _CONTROL_RE.search(api_key)
        ):
            raise ContractViolation("invalid_zep_api_key")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 30.0
        ):
            raise ContractViolation("invalid_zep_timeout")
        self._authorization = f"Api-Key {api_key}"
        self._timeout_seconds = float(timeout_seconds)
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls_context = context

    def request(self, *, method: str, path: str) -> ZepHttpResponse:
        if method not in {"DELETE", "GET"}:
            raise ContractViolation("invalid_zep_http_method")
        if (
            not isinstance(path, str)
            or not path.startswith(ZEP_API_PREFIX + "/")
            or _CONTROL_RE.search(path)
            or len(path) > 4_096
        ):
            raise ContractViolation("invalid_zep_http_path")
        connection: http.client.HTTPSConnection | None = None
        try:
            connection = http.client.HTTPSConnection(
                ZEP_API_HOST,
                port=443,
                timeout=self._timeout_seconds,
                context=self._tls_context,
            )
            connection.request(
                method,
                path,
                headers={
                    "Accept": "application/json",
                    "Authorization": self._authorization,
                },
            )
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ZepDeletionUnavailable("zep_response_too_large")
            return ZepHttpResponse(status=response.status, body=body)
        except (ContractViolation, ZepDeletionUnavailable):
            raise
        except Exception as error:
            raise ZepDeletionUnavailable("zep_request_failed") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def zep_user_id(owner_user_id: UUID) -> str:
    if not isinstance(owner_user_id, UUID):
        raise ContractViolation("invalid_zep_deletion_owner")
    return f"lifeswitch-user-{owner_user_id}"


def zep_thread_id(thread_id: UUID) -> str:
    if not isinstance(thread_id, UUID):
        raise ContractViolation("invalid_zep_deletion_thread")
    return f"lifeswitch-thread-{thread_id}"


def _encoded(value: str) -> str:
    return quote(value, safe="")


class ZepConversationDeletionV1:
    """Translate one bound erasure lease into idempotent Zep deletion calls."""

    def __init__(self, *, transport: ZepHttpTransport) -> None:
        if transport is None:
            raise ContractViolation("zep_deletion_transport_required")
        self._transport = transport

    def _request(self, *, method: str, path: str) -> ZepHttpResponse:
        response = self._transport.request(method=method, path=path)
        if response.status in {401, 403}:
            raise ContractViolation("zep_deletion_authorization_failed")
        if response.status == 429 or response.status >= 500:
            raise ZepDeletionUnavailable("zep_deletion_unavailable")
        return response

    def _delete_path(self, path: str) -> None:
        response = self._request(method="DELETE", path=path)
        if response.status not in {200, 204, 404}:
            raise ContractViolation("unexpected_zep_delete_response")

    def _delete_user(self, owner_user_id: UUID) -> None:
        self._delete_path(
            f"{ZEP_API_PREFIX}/users/{_encoded(zep_user_id(owner_user_id))}"
        )

    def _delete_thread(self, thread_id: UUID) -> None:
        self._delete_path(
            f"{ZEP_API_PREFIX}/threads/{_encoded(zep_thread_id(thread_id))}"
        )

    def _delete_episode(self, episode_id: str) -> None:
        if (
            not isinstance(episode_id, str)
            or not episode_id
            or len(episode_id) > 256
            or _CONTROL_RE.search(episode_id)
        ):
            raise ContractViolation("invalid_zep_episode_id")
        self._delete_path(
            f"{ZEP_API_PREFIX}/graph/episodes/{_encoded(episode_id)}"
        )

    def _message_page(
        self,
        *,
        thread_id: UUID,
        cursor: int,
    ) -> tuple[list[object], int]:
        query = urlencode({"limit": ZEP_THREAD_PAGE_SIZE, "cursor": cursor})
        path = (
            f"{ZEP_API_PREFIX}/threads/{_encoded(zep_thread_id(thread_id))}"
            f"/messages?{query}"
        )
        response = self._request(method="GET", path=path)
        if response.status == 404:
            return [], 0
        if response.status != 200:
            raise ContractViolation("unexpected_zep_thread_response")
        try:
            value = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ContractViolation("invalid_zep_thread_response") from None
        if not isinstance(value, dict):
            raise ContractViolation("invalid_zep_thread_response")
        messages = value.get("messages")
        total_count = value.get("total_count")
        if not isinstance(messages, list):
            raise ContractViolation("invalid_zep_thread_response")
        if type(total_count) is not int or not 0 <= total_count <= ZEP_MAX_THREAD_MESSAGES:
            raise ContractViolation("invalid_zep_thread_response")
        return messages, total_count

    def _resolve_episode_ids(
        self,
        *,
        thread_id: UUID,
        message_ids: frozenset[UUID],
    ) -> tuple[str, ...]:
        wanted = {str(value) for value in message_ids}
        resolved: dict[str, str] = {}
        cursor = 0
        while cursor < ZEP_MAX_THREAD_MESSAGES and wanted - resolved.keys():
            messages, total_count = self._message_page(
                thread_id=thread_id,
                cursor=cursor,
            )
            for message in messages:
                if not isinstance(message, dict):
                    raise ContractViolation("invalid_zep_thread_response")
                metadata = message.get("metadata")
                episode_id = message.get("uuid", message.get("uuid_"))
                if not isinstance(metadata, dict) or not isinstance(episode_id, str):
                    continue
                lifeswitch_id = metadata.get(ZEP_MESSAGE_ID_METADATA_KEY)
                if isinstance(lifeswitch_id, str) and lifeswitch_id in wanted:
                    previous = resolved.setdefault(lifeswitch_id, episode_id)
                    if previous != episode_id:
                        raise ContractViolation("duplicate_zep_message_mapping")
            cursor += len(messages)
            if not messages or cursor >= total_count:
                break
        return tuple(resolved[key] for key in sorted(resolved))

    def _delete_message_targets(
        self,
        targets: tuple[ConversationErasureTarget, ...],
    ) -> None:
        by_thread: dict[UUID, set[UUID]] = {}
        for target in targets:
            by_thread.setdefault(target.thread_id, set()).add(target.message_id)
        episode_ids: list[str] = []
        for thread_id in sorted(by_thread, key=str):
            episode_ids.extend(
                self._resolve_episode_ids(
                    thread_id=thread_id,
                    message_ids=frozenset(by_thread[thread_id]),
                )
            )
        bounded = episode_ids[:ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT]
        for episode_id in bounded:
            self._delete_episode(episode_id)
        if len(episode_ids) > len(bounded):
            raise ZepDeletionUnavailable("zep_deletion_more_work")

    def _erase_sync(
        self,
        *,
        lease: ConversationErasureLease,
        targets: tuple[ConversationErasureTarget, ...],
    ) -> None:
        if lease.selector_kind is DeletionSelectorKind.ALL_CONVERSATIONS:
            self._delete_user(lease.owner_user_id)
            return
        if lease.selector_kind is DeletionSelectorKind.THREAD:
            for thread_id in sorted({target.thread_id for target in targets}, key=str):
                self._delete_thread(thread_id)
            return
        if lease.selector_kind in {
            DeletionSelectorKind.MESSAGE_TAIL,
            DeletionSelectorKind.RECENT,
        }:
            self._delete_message_targets(targets)
            return
        raise ContractViolation("unsupported_zep_deletion_selector")

    async def erase(
        self,
        *,
        lease: ConversationErasureLease,
        targets: tuple[ConversationErasureTarget, ...],
    ) -> None:
        if not isinstance(lease, ConversationErasureLease):
            raise ContractViolation("invalid_zep_deletion_lease")
        if not isinstance(targets, tuple) or any(
            not isinstance(target, ConversationErasureTarget)
            for target in targets
        ):
            raise ContractViolation("invalid_zep_deletion_targets")
        await asyncio.to_thread(self._erase_sync, lease=lease, targets=targets)


__all__ = [
    "BoundedZepHttpsTransport",
    "ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT",
    "ZEP_MESSAGE_ID_METADATA_KEY",
    "ZepConversationDeletionV1",
    "ZepDeletionUnavailable",
    "ZepHttpResponse",
    "zep_thread_id",
    "zep_user_id",
]
