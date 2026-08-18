from __future__ import annotations

"""Owner-bound Zep Cloud adapter with duplicate-safe turn reconciliation."""

import asyncio
import hashlib
import inspect
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Mapping, Protocol
from uuid import UUID


ZEP_API_KEY_ENV = "ZEP_API_KEY"
ZEP_SYNC_MODE_ENV = "ZEP_SYNC_MODE"
ZEP_SYNC_OWNER_IDS_ENV = "ZEP_SYNC_OWNER_IDS"
ZEP_TIMEOUT_SECONDS_ENV = "ZEP_TIMEOUT_SECONDS"

ZEP_SYNC_MODE_OFF = "off"
ZEP_SYNC_MODE_CANARY = "canary"
ZEP_SYNC_MODE_ON = "on"
_VALID_MODES = frozenset(
    (ZEP_SYNC_MODE_OFF, ZEP_SYNC_MODE_CANARY, ZEP_SYNC_MODE_ON)
)
_DEFAULT_TIMEOUT_SECONDS = 12.0
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_CONTEXT_BYTES = 1_048_576
_MAX_GRAPH_SEARCH_QUERY_CHARACTERS = 400
_GRAPH_SEARCH_CONTEXT_CHARACTERS = 8_000
_MAX_MESSAGE_CHARACTERS = 4_096


class ZepConfigurationError(ValueError):
    pass


class ZepOwnershipError(RuntimeError):
    pass


class ZepSyncPermanentError(RuntimeError):
    """A retry cannot make the exact turn safe to submit."""


class ZepTransport(Protocol):
    async def ensure_user_and_thread(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> None: ...

    async def reconcile_turn(
        self,
        *,
        thread_id: str,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
        user_created_at: datetime,
        assistant_created_at: datetime,
    ) -> str: ...

    async def search_owner_context(
        self,
        *,
        user_id: str,
        query: str,
    ) -> str: ...

    async def delete_owner(self, *, user_id: str) -> None: ...

    async def close(self) -> None: ...


ZepTransportFactory = Callable[[str], ZepTransport]


def zep_user_id(owner_user_id: UUID) -> str:
    if not isinstance(owner_user_id, UUID):
        raise TypeError("owner_user_id must be UUID")
    return f"lifeswitch-user-{owner_user_id}"


def zep_thread_id(thread_id: UUID) -> str:
    if not isinstance(thread_id, UUID):
        raise TypeError("thread_id must be UUID")
    return f"lifeswitch-thread-{thread_id}"


def _sha256_identifier(value: UUID) -> str:
    return hashlib.sha256(str(value).encode("ascii")).hexdigest()


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ZepSyncPermanentError("invalid_message_timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ZepSettings:
    mode: str
    owner_user_ids: frozenset[UUID]
    timeout_seconds: float

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ZepSettings":
        mode = str(environment.get(ZEP_SYNC_MODE_ENV, "")).strip().casefold()
        if not mode:
            mode = ZEP_SYNC_MODE_OFF
        if mode not in _VALID_MODES:
            raise ZepConfigurationError("invalid_zep_sync_mode")

        raw_owner_ids = str(
            environment.get(ZEP_SYNC_OWNER_IDS_ENV, "")
        ).strip()
        try:
            owner_ids = {
                UUID(item.strip())
                for item in raw_owner_ids.split(",")
                if item.strip()
            }
        except (TypeError, ValueError):
            raise ZepConfigurationError("invalid_zep_sync_owner_ids") from None
        if mode == ZEP_SYNC_MODE_CANARY and not owner_ids:
            raise ZepConfigurationError("zep_sync_canary_owner_ids_required")

        raw_timeout = str(
            environment.get(ZEP_TIMEOUT_SECONDS_ENV, "")
        ).strip()
        try:
            timeout = (
                float(raw_timeout)
                if raw_timeout
                else _DEFAULT_TIMEOUT_SECONDS
            )
        except ValueError:
            raise ZepConfigurationError("invalid_zep_timeout") from None
        if not 0.1 <= timeout <= _MAX_TIMEOUT_SECONDS:
            raise ZepConfigurationError("invalid_zep_timeout")
        return cls(
            mode=mode,
            owner_user_ids=frozenset(owner_ids),
            timeout_seconds=timeout,
        )

    def enabled_for(self, owner_user_id: UUID) -> bool:
        return self.mode == ZEP_SYNC_MODE_ON or (
            self.mode == ZEP_SYNC_MODE_CANARY
            and owner_user_id in self.owner_user_ids
        )


class ZepCloudTransport:
    """Lazy wrapper around the official Zep Cloud SDK."""

    def __init__(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ZepConfigurationError("zep_api_key_required")
        from zep_cloud.client import AsyncZep
        from zep_cloud.types import Message

        self._client = AsyncZep(api_key=api_key.strip())
        self._message_type = Message

    async def ensure_user_and_thread(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> None:
        try:
            user = await self._client.user.get(user_id)
        except Exception as error:
            if _status_code(error) != 404:
                raise
            try:
                user = await self._client.user.add(user_id=user_id)
            except Exception as create_error:
                if _status_code(create_error) != 409:
                    raise
                user = await self._client.user.get(user_id)
        if str(getattr(user, "user_id", "")) != user_id:
            raise ZepOwnershipError("zep_user_identity_mismatch")

        try:
            thread = await self._client.thread.get(thread_id)
        except Exception as error:
            if _status_code(error) != 404:
                raise
            try:
                thread = await self._client.thread.create(
                    thread_id=thread_id,
                    user_id=user_id,
                )
            except Exception as create_error:
                if _status_code(create_error) != 409:
                    raise
                thread = await self._client.thread.get(thread_id)
        if str(getattr(thread, "user_id", "")) != user_id:
            raise ZepOwnershipError("zep_thread_owner_mismatch")
        returned_thread_id = getattr(thread, "thread_id", None)
        if returned_thread_id is not None and str(returned_thread_id) != thread_id:
            raise ZepOwnershipError("zep_thread_identity_mismatch")

    async def _episode_exists(
        self,
        *,
        message_id: UUID,
        thread_id: str,
    ) -> bool:
        try:
            episode = await self._client.graph.episode.get(str(message_id))
        except Exception as error:
            if _status_code(error) == 404:
                return False
            raise
        if str(getattr(episode, "uuid_", "")) != str(message_id):
            raise ZepSyncPermanentError("zep_episode_identity_mismatch")
        if str(getattr(episode, "thread_id", "")) != thread_id:
            raise ZepSyncPermanentError("zep_episode_thread_mismatch")
        return True

    async def _add_messages(
        self,
        *,
        thread_id: str,
        values: list[tuple[UUID, str, str, str, datetime]],
    ) -> None:
        messages = []
        expected: list[str] = []
        for message_id, role, name, content, created_at in values:
            if not isinstance(content, str) or not content.strip():
                raise ZepSyncPermanentError("invalid_zep_message_content")
            if len(content) > _MAX_MESSAGE_CHARACTERS:
                raise ZepSyncPermanentError("zep_message_too_large")
            expected.append(str(message_id))
            messages.append(
                self._message_type(
                    uuid_=str(message_id),
                    role=role,
                    name=name,
                    content=content,
                    created_at=_rfc3339(created_at),
                    metadata={"lifeswitch_message_id": str(message_id)},
                )
            )
        response = await self._client.thread.add_messages(
            thread_id,
            messages=messages,
            ignore_roles=["assistant"],
            return_context=False,
        )
        returned = list(getattr(response, "message_uuids", None) or ())
        if returned != expected:
            raise ZepSyncPermanentError("zep_message_receipt_mismatch")
        for message_id, *_ in values:
            if not await self._episode_exists(
                message_id=message_id,
                thread_id=thread_id,
            ):
                raise RuntimeError("zep_message_write_unverified")

    async def reconcile_turn(
        self,
        *,
        thread_id: str,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
        user_created_at: datetime,
        assistant_created_at: datetime,
    ) -> str:
        user_exists = await self._episode_exists(
            message_id=user_message_id,
            thread_id=thread_id,
        )
        assistant_exists = await self._episode_exists(
            message_id=assistant_message_id,
            thread_id=thread_id,
        )
        if user_exists and assistant_exists:
            return "already_present"
        if assistant_exists and not user_exists:
            raise ZepSyncPermanentError("zep_turn_order_conflict")
        values = []
        if not user_exists:
            values.append(
                (
                    user_message_id,
                    "user",
                    "LifeSwitch User",
                    user_message,
                    user_created_at,
                )
            )
        values.append(
            (
                assistant_message_id,
                "assistant",
                "LifeSwitch Assistant",
                assistant_message,
                assistant_created_at,
            )
        )
        await self._add_messages(thread_id=thread_id, values=values)
        return "added"

    async def search_owner_context(self, *, user_id: str, query: str) -> str:
        bounded_query = _bounded_graph_search_query(query)
        response = await self._client.graph.search(
            user_id=user_id,
            query=bounded_query,
            scope="auto",
            max_characters=_GRAPH_SEARCH_CONTEXT_CHARACTERS,
        )
        context = getattr(response, "context", None)
        if not isinstance(context, str):
            raise ZepConfigurationError("invalid_zep_search_context")
        if len(context.encode("utf-8")) > _MAX_CONTEXT_BYTES:
            raise ZepConfigurationError("zep_search_context_too_large")
        return context

    async def delete_owner(self, *, user_id: str) -> None:
        try:
            await self._client.user.delete(user_id=user_id)
        except Exception as error:
            if _status_code(error) != 404:
                raise
        try:
            await self._client.user.get(user_id=user_id)
        except Exception as error:
            if _status_code(error) == 404:
                return
            raise
        raise ZepConfigurationError("zep_owner_delete_unverified")

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _default_transport_factory(api_key: str) -> ZepTransport:
    return ZepCloudTransport(api_key)


def _bounded_graph_search_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ZepConfigurationError("invalid_zep_search_query")
    value = query.strip()
    if len(value) <= _MAX_GRAPH_SEARCH_QUERY_CHARACTERS:
        return value
    separator = "\n...\n"
    remaining = _MAX_GRAPH_SEARCH_QUERY_CHARACTERS - len(separator)
    leading = remaining // 2
    trailing = remaining - leading
    return value[:leading] + separator + value[-trailing:]


class ZepRuntime:
    """Owns bounded owner-scoped Zep prompt, sync, and erasure I/O."""

    def __init__(
        self,
        *,
        settings: ZepSettings,
        api_key: str,
        transport_factory: ZepTransportFactory = _default_transport_factory,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._transport_factory = transport_factory
        self._logger = logger or logging.getLogger("uvicorn.error")
        self._transport: ZepTransport | None = None
        self._transport_lock = asyncio.Lock()
        self._provision_lock = asyncio.Lock()
        self._deletion_lock = asyncio.Lock()
        self._provisioned_threads: set[tuple[str, str]] = set()
        self._owner_tasks: dict[UUID, set[asyncio.Task[object]]] = {}
        self._blocked_owners: set[UUID] = set()

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        transport_factory: ZepTransportFactory = _default_transport_factory,
        logger: logging.Logger | None = None,
    ) -> "ZepRuntime":
        return cls(
            settings=ZepSettings.from_environment(environment),
            api_key=str(environment.get(ZEP_API_KEY_ENV, "")),
            transport_factory=transport_factory,
            logger=logger,
        )

    def sync_enabled_for(self, owner_user_id: UUID) -> bool:
        return self._settings.enabled_for(owner_user_id)

    @property
    def sync_configured(self) -> bool:
        return self._settings.mode != ZEP_SYNC_MODE_OFF

    async def _transport_client(self) -> ZepTransport:
        if not self._api_key:
            raise ZepConfigurationError("zep_api_key_required")
        if self._transport is None:
            async with self._transport_lock:
                if self._transport is None:
                    self._transport = self._transport_factory(self._api_key)
        return self._transport

    @asynccontextmanager
    async def _owner_io(self, owner_user_id: UUID) -> AsyncIterator[None]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise ZepConfigurationError("zep_task_required")
        if owner_user_id in self._blocked_owners:
            raise ZepConfigurationError("zep_owner_erasure_active")
        async with self._deletion_lock:
            if owner_user_id in self._blocked_owners:
                raise ZepConfigurationError("zep_owner_erasure_active")
            self._owner_tasks.setdefault(owner_user_id, set()).add(current_task)
        try:
            yield
        finally:
            pending = self._owner_tasks.get(owner_user_id)
            if pending is not None:
                pending.discard(current_task)
                if not pending:
                    self._owner_tasks.pop(owner_user_id, None)

    async def _ensure_owner_thread(
        self,
        *,
        transport: ZepTransport,
        user_id: str,
        thread_id: str,
    ) -> None:
        key = (user_id, thread_id)
        if key in self._provisioned_threads:
            return
        async with self._provision_lock:
            if key not in self._provisioned_threads:
                await transport.ensure_user_and_thread(
                    user_id=user_id,
                    thread_id=thread_id,
                )
                self._provisioned_threads.add(key)

    async def synchronize_turn(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
        user_created_at: datetime,
        assistant_created_at: datetime,
    ) -> str:
        if not self._settings.enabled_for(owner_user_id):
            raise ZepConfigurationError("zep_sync_owner_disabled")
        owner_hash = _sha256_identifier(owner_user_id)
        thread_hash = _sha256_identifier(thread_id)
        started_ns = time.monotonic_ns()
        async with self._owner_io(owner_user_id):
            transport = await self._transport_client()
            user_id = zep_user_id(owner_user_id)
            provider_thread_id = zep_thread_id(thread_id)
            await asyncio.wait_for(
                self._ensure_owner_thread(
                    transport=transport,
                    user_id=user_id,
                    thread_id=provider_thread_id,
                ),
                timeout=self._settings.timeout_seconds,
            )
            outcome = await asyncio.wait_for(
                transport.reconcile_turn(
                    thread_id=provider_thread_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    user_created_at=user_created_at,
                    assistant_created_at=assistant_created_at,
                ),
                timeout=self._settings.timeout_seconds,
            )
        self._logger.info(
            "[zep_sync] turn_synchronized owner_sha256=%s thread_sha256=%s "
            "outcome=%s latency_ms=%s",
            owner_hash,
            thread_hash,
            outcome,
            max(0, round((time.monotonic_ns() - started_ns) / 1_000_000)),
        )
        return outcome

    async def retrieve_prompt_context(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        current_message: str,
    ) -> str:
        if not isinstance(current_message, str) or not current_message.strip():
            raise ZepConfigurationError("invalid_zep_prompt_request")
        owner_hash = _sha256_identifier(owner_user_id)
        thread_hash = _sha256_identifier(thread_id)
        started_ns = time.monotonic_ns()
        try:
            async with self._owner_io(owner_user_id):
                transport = await self._transport_client()
                context = await asyncio.wait_for(
                    transport.search_owner_context(
                        user_id=zep_user_id(owner_user_id),
                        query=current_message,
                    ),
                    timeout=self._settings.timeout_seconds,
                )
            context_bytes = context.encode("utf-8")
            self._logger.info(
                "[zep_prompt] retrieval_succeeded owner_sha256=%s "
                "thread_sha256=%s context_sha256=%s context_bytes=%s "
                "latency_ms=%s",
                owner_hash,
                thread_hash,
                hashlib.sha256(context_bytes).hexdigest(),
                len(context_bytes),
                max(0, round((time.monotonic_ns() - started_ns) / 1_000_000)),
            )
            return context
        except Exception as error:
            self._logger.error(
                "[zep_prompt] retrieval_failed owner_sha256=%s "
                "thread_sha256=%s error_type=%s",
                owner_hash,
                thread_hash,
                type(error).__name__,
            )
            raise

    @asynccontextmanager
    async def owner_erasure_barrier(
        self,
        owner_user_id: UUID,
    ) -> AsyncIterator[None]:
        if not isinstance(owner_user_id, UUID):
            raise ZepConfigurationError("invalid_zep_deletion_owner")
        if not self._api_key:
            raise ZepConfigurationError("zep_api_key_required")
        async with self._deletion_lock:
            self._blocked_owners.add(owner_user_id)
            try:
                pending = tuple(self._owner_tasks.get(owner_user_id, ()))
                if pending:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=self._settings.timeout_seconds,
                    )
                yield
            finally:
                self._blocked_owners.discard(owner_user_id)

    async def delete_owner_memory(self, owner_user_id: UUID) -> None:
        if owner_user_id not in self._blocked_owners:
            raise ZepConfigurationError("zep_erasure_barrier_required")
        transport = await self._transport_client()
        await asyncio.wait_for(
            transport.delete_owner(user_id=zep_user_id(owner_user_id)),
            timeout=self._settings.timeout_seconds,
        )
        owner_id = zep_user_id(owner_user_id)
        self._provisioned_threads = {
            value for value in self._provisioned_threads if value[0] != owner_id
        }
        self._logger.info(
            "[zep_sync] owner_delete_succeeded owner_sha256=%s",
            _sha256_identifier(owner_user_id),
        )

    async def close(self) -> None:
        if self._transport is not None:
            await self._transport.close()


__all__ = [
    "ZEP_API_KEY_ENV",
    "ZEP_SYNC_MODE_CANARY",
    "ZEP_SYNC_MODE_ENV",
    "ZEP_SYNC_MODE_OFF",
    "ZEP_SYNC_MODE_ON",
    "ZEP_SYNC_OWNER_IDS_ENV",
    "ZEP_TIMEOUT_SECONDS_ENV",
    "ZepCloudTransport",
    "ZepConfigurationError",
    "ZepOwnershipError",
    "ZepRuntime",
    "ZepSettings",
    "ZepSyncPermanentError",
    "zep_thread_id",
    "zep_user_id",
]
