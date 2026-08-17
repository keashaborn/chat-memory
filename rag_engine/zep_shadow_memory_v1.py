from __future__ import annotations

"""Zep write/retrieval shadow disconnected from response context."""

import asyncio
import hashlib
import inspect
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Mapping, Protocol
from uuid import UUID


ZEP_API_KEY_ENV = "ZEP_API_KEY"
ZEP_SHADOW_MODE_ENV = "ZEP_SHADOW_MODE"
ZEP_SHADOW_OWNER_IDS_ENV = "ZEP_SHADOW_OWNER_IDS"
ZEP_SHADOW_TIMEOUT_SECONDS_ENV = "ZEP_SHADOW_TIMEOUT_SECONDS"

ZEP_SHADOW_MODE_OFF = "off"
ZEP_SHADOW_MODE_CANARY = "canary"
ZEP_SHADOW_MODE_ON = "on"
_VALID_MODES = frozenset(
    (ZEP_SHADOW_MODE_OFF, ZEP_SHADOW_MODE_CANARY, ZEP_SHADOW_MODE_ON)
)
_DEFAULT_TIMEOUT_SECONDS = 12.0
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_CONTEXT_BYTES = 1_048_576
_MAX_GRAPH_SEARCH_QUERY_CHARACTERS = 400
_GRAPH_SEARCH_CONTEXT_CHARACTERS = 8_000


class ZepShadowConfigurationError(ValueError):
    pass


class ZepShadowOwnershipError(RuntimeError):
    pass


class ZepShadowTransportV1(Protocol):
    async def ensure_user_and_thread(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> None: ...

    async def add_turn(
        self,
        *,
        thread_id: str,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> None: ...

    async def get_owner_context(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> str: ...

    async def search_owner_context(
        self,
        *,
        user_id: str,
        query: str,
    ) -> str: ...

    async def delete_owner(self, *, user_id: str) -> None: ...

    async def close(self) -> None: ...


ZepShadowTransportFactoryV1 = Callable[[str], ZepShadowTransportV1]


def zep_user_id_v1(owner_user_id: UUID) -> str:
    if not isinstance(owner_user_id, UUID):
        raise TypeError("owner_user_id must be UUID")
    return f"lifeswitch-user-{owner_user_id}"


def zep_thread_id_v1(thread_id: UUID) -> str:
    if not isinstance(thread_id, UUID):
        raise TypeError("thread_id must be UUID")
    return f"lifeswitch-thread-{thread_id}"


def _sha256_identifier(value: UUID) -> str:
    return hashlib.sha256(str(value).encode("ascii")).hexdigest()


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class ZepShadowSettingsV1:
    mode: str
    owner_user_ids: frozenset[UUID]
    timeout_seconds: float

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> "ZepShadowSettingsV1":
        mode = str(environment.get(ZEP_SHADOW_MODE_ENV, "")).strip().casefold()
        if not mode:
            mode = ZEP_SHADOW_MODE_OFF
        if mode not in _VALID_MODES:
            raise ZepShadowConfigurationError("invalid_zep_shadow_mode")

        raw_owner_ids = str(
            environment.get(ZEP_SHADOW_OWNER_IDS_ENV, "")
        ).strip()
        owner_ids: set[UUID] = set()
        if raw_owner_ids:
            try:
                owner_ids = {
                    UUID(item.strip())
                    for item in raw_owner_ids.split(",")
                    if item.strip()
                }
            except (TypeError, ValueError):
                raise ZepShadowConfigurationError(
                    "invalid_zep_shadow_owner_ids"
                ) from None
        if mode == ZEP_SHADOW_MODE_CANARY and not owner_ids:
            raise ZepShadowConfigurationError(
                "zep_shadow_canary_owner_ids_required"
            )

        raw_timeout = str(
            environment.get(ZEP_SHADOW_TIMEOUT_SECONDS_ENV, "")
        ).strip()
        try:
            timeout = (
                float(raw_timeout)
                if raw_timeout
                else _DEFAULT_TIMEOUT_SECONDS
            )
        except ValueError:
            raise ZepShadowConfigurationError(
                "invalid_zep_shadow_timeout"
            ) from None
        if not 0.1 <= timeout <= _MAX_TIMEOUT_SECONDS:
            raise ZepShadowConfigurationError("invalid_zep_shadow_timeout")

        return cls(
            mode=mode,
            owner_user_ids=frozenset(owner_ids),
            timeout_seconds=timeout,
        )

    def enabled_for(self, owner_user_id: UUID) -> bool:
        return (
            self.mode == ZEP_SHADOW_MODE_ON
            or (
                self.mode == ZEP_SHADOW_MODE_CANARY
                and owner_user_id in self.owner_user_ids
            )
        )


class ZepCloudShadowTransportV1:
    """Small lazy wrapper around the official Zep Cloud SDK."""

    def __init__(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ZepShadowConfigurationError("zep_api_key_required")
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
            await self._client.user.get(user_id)
        except Exception as error:
            if _status_code(error) != 404:
                raise
            try:
                await self._client.user.add(user_id=user_id)
            except Exception as create_error:
                if _status_code(create_error) != 409:
                    raise

        try:
            await self._client.thread.get(thread_id)
        except Exception as error:
            if _status_code(error) != 404:
                raise
            try:
                await self._client.thread.create(
                    thread_id=thread_id,
                    user_id=user_id,
                )
            except Exception as create_error:
                if _status_code(create_error) != 409:
                    raise

    async def add_turn(
        self,
        *,
        thread_id: str,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> None:
        messages = [
            self._message_type(
                uuid_=str(user_message_id),
                role="user",
                name="LifeSwitch User",
                content=user_message,
                metadata={
                    "lifeswitch_message_id": str(user_message_id),
                },
            ),
            self._message_type(
                uuid_=str(assistant_message_id),
                role="assistant",
                name="LifeSwitch Assistant",
                content=assistant_message,
                metadata={
                    "lifeswitch_message_id": str(assistant_message_id),
                },
            ),
        ]
        await self._client.thread.add_messages(
            thread_id,
            messages=messages,
            ignore_roles=["assistant"],
            return_context=False,
        )

    async def get_owner_context(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> str:
        owner_threads = await self._client.user.get_threads(user_id=user_id)
        matches = [
            item
            for item in owner_threads
            if getattr(item, "thread_id", None) == thread_id
            and getattr(item, "user_id", None) == user_id
        ]
        if len(matches) != 1:
            raise ZepShadowOwnershipError("zep_thread_owner_mismatch")
        response = await self._client.thread.get_user_context(
            thread_id=thread_id,
        )
        context = getattr(response, "context", None)
        if not isinstance(context, str):
            raise ZepShadowConfigurationError("invalid_zep_context")
        if len(context.encode("utf-8")) > _MAX_CONTEXT_BYTES:
            raise ZepShadowConfigurationError("zep_context_too_large")
        return context

    async def search_owner_context(
        self,
        *,
        user_id: str,
        query: str,
    ) -> str:
        bounded_query = _bounded_graph_search_query(query)
        response = await self._client.graph.search(
            user_id=user_id,
            query=bounded_query,
            scope="auto",
            max_characters=_GRAPH_SEARCH_CONTEXT_CHARACTERS,
        )
        context = getattr(response, "context", None)
        if not isinstance(context, str):
            raise ZepShadowConfigurationError("invalid_zep_search_context")
        if len(context.encode("utf-8")) > _MAX_CONTEXT_BYTES:
            raise ZepShadowConfigurationError("zep_search_context_too_large")
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
        raise ZepShadowConfigurationError("zep_owner_delete_unverified")

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _default_transport_factory(api_key: str) -> ZepShadowTransportV1:
    return ZepCloudShadowTransportV1(api_key)


def _bounded_graph_search_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ZepShadowConfigurationError("invalid_zep_search_query")
    value = query.strip()
    if len(value) <= _MAX_GRAPH_SEARCH_QUERY_CHARACTERS:
        return value
    separator = "\n...\n"
    remaining = _MAX_GRAPH_SEARCH_QUERY_CHARACTERS - len(separator)
    leading = remaining // 2
    trailing = remaining - leading
    return value[:leading] + separator + value[-trailing:]


class ZepShadowRuntimeV1:
    """Owns bounded Zep I/O for shadow observation and prompt retrieval."""

    def __init__(
        self,
        *,
        settings: ZepShadowSettingsV1,
        api_key: str,
        transport_factory: ZepShadowTransportFactoryV1 = (
            _default_transport_factory
        ),
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._transport_factory = transport_factory
        self._logger = logger or logging.getLogger("uvicorn.error")
        self._transport: ZepShadowTransportV1 | None = None
        self._transport_lock = asyncio.Lock()
        self._provision_lock = asyncio.Lock()
        self._deletion_lock = asyncio.Lock()
        self._provisioned_threads: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._owner_tasks: dict[UUID, set[asyncio.Task[None]]] = {}
        self._blocked_owners: set[UUID] = set()

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        transport_factory: ZepShadowTransportFactoryV1 = (
            _default_transport_factory
        ),
        logger: logging.Logger | None = None,
    ) -> "ZepShadowRuntimeV1":
        return cls(
            settings=ZepShadowSettingsV1.from_environment(environment),
            api_key=str(environment.get(ZEP_API_KEY_ENV, "")),
            transport_factory=transport_factory,
            logger=logger,
        )

    def dispatch_turn(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> str:
        if not isinstance(owner_user_id, UUID) or not isinstance(
            thread_id, UUID
        ):
            return "excluded"
        if owner_user_id in self._blocked_owners:
            return "blocked"
        if not self._settings.enabled_for(owner_user_id):
            return "disabled"
        if not self._api_key:
            self._logger.error("[zep_shadow] disabled code=api_key_missing")
            return "misconfigured"
        if not isinstance(user_message, str) or not user_message.strip():
            return "excluded"
        if not isinstance(assistant_message, str) or not assistant_message.strip():
            return "excluded"
        if not isinstance(user_message_id, UUID) or not isinstance(
            assistant_message_id, UUID
        ):
            return "excluded"

        self._schedule_owner_task(
            owner_user_id,
            self._write_turn(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                user_message=user_message,
                assistant_message=assistant_message,
            ),
        )
        return "scheduled"

    def dispatch_retrieval(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
    ) -> str:
        if not isinstance(owner_user_id, UUID) or not isinstance(
            thread_id, UUID
        ):
            return "excluded"
        if owner_user_id in self._blocked_owners:
            return "blocked"
        if not self._settings.enabled_for(owner_user_id):
            return "disabled"
        if not self._api_key:
            self._logger.error("[zep_shadow] disabled code=api_key_missing")
            return "misconfigured"

        self._schedule_owner_task(
            owner_user_id,
            self._retrieve_context(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            ),
        )
        return "scheduled"

    def _schedule_owner_task(
        self,
        owner_user_id: UUID,
        awaitable: Awaitable[None],
    ) -> None:
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        owner_tasks = self._owner_tasks.setdefault(owner_user_id, set())
        owner_tasks.add(task)

        def discard(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            pending = self._owner_tasks.get(owner_user_id)
            if pending is None:
                return
            pending.discard(completed)
            if not pending:
                self._owner_tasks.pop(owner_user_id, None)

        task.add_done_callback(discard)

    async def _drain_owner_tasks(self, owner_user_id: UUID) -> None:
        pending = tuple(self._owner_tasks.get(owner_user_id, ()))
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=self._settings.timeout_seconds,
            )
        except asyncio.TimeoutError:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    @asynccontextmanager
    async def owner_erasure_barrier(
        self,
        owner_user_id: UUID,
    ) -> AsyncIterator[None]:
        if not isinstance(owner_user_id, UUID):
            raise ZepShadowConfigurationError("invalid_zep_deletion_owner")
        if not self._api_key:
            raise ZepShadowConfigurationError("zep_api_key_required")
        async with self._deletion_lock:
            self._blocked_owners.add(owner_user_id)
            try:
                await self._drain_owner_tasks(owner_user_id)
                yield
            finally:
                self._blocked_owners.discard(owner_user_id)

    async def delete_owner_memory(self, owner_user_id: UUID) -> None:
        if not isinstance(owner_user_id, UUID):
            raise ZepShadowConfigurationError("invalid_zep_deletion_owner")
        if owner_user_id not in self._blocked_owners:
            raise ZepShadowConfigurationError("zep_erasure_barrier_required")
        transport = await self._transport_client()
        await asyncio.wait_for(
            transport.delete_owner(user_id=zep_user_id_v1(owner_user_id)),
            timeout=self._settings.timeout_seconds,
        )
        zep_user_id = zep_user_id_v1(owner_user_id)
        self._provisioned_threads = {
            value
            for value in self._provisioned_threads
            if value[0] != zep_user_id
        }
        self._logger.info(
            "[zep_shadow] owner_delete_succeeded owner_sha256=%s",
            _sha256_identifier(owner_user_id),
        )

    async def _transport_client(self) -> ZepShadowTransportV1:
        if self._transport is None:
            async with self._transport_lock:
                if self._transport is None:
                    self._transport = self._transport_factory(self._api_key)
        return self._transport

    async def retrieve_prompt_context(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        current_message: str,
    ) -> str:
        if not isinstance(owner_user_id, UUID) or not isinstance(
            thread_id, UUID
        ):
            raise ZepShadowConfigurationError("invalid_zep_prompt_request")
        if not isinstance(current_message, str) or not current_message.strip():
            raise ZepShadowConfigurationError("invalid_zep_prompt_request")
        if not self._api_key:
            raise ZepShadowConfigurationError("zep_api_key_required")
        current_task = asyncio.current_task()
        if current_task is None:
            raise ZepShadowConfigurationError("zep_prompt_task_required")
        async with self._deletion_lock:
            if owner_user_id in self._blocked_owners:
                raise ZepShadowConfigurationError("zep_owner_erasure_active")
            self._owner_tasks.setdefault(owner_user_id, set()).add(current_task)
        owner_hash = _sha256_identifier(owner_user_id)
        thread_hash = _sha256_identifier(thread_id)
        started_ns = time.monotonic_ns()
        try:
            context = await asyncio.wait_for(
                self._search_prompt_context_bounded(
                    owner_user_id=owner_user_id,
                    current_message=current_message,
                ),
                timeout=self._settings.timeout_seconds,
            )
            context_bytes = context.encode("utf-8")
            self._logger.info(
                "[zep_prompt] retrieval_succeeded owner_sha256=%s "
                "thread_sha256=%s context_sha256=%s context_bytes=%s "
                "latency_ms=%s prompt_bound=true",
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
                "thread_sha256=%s error_type=%s prompt_bound=true",
                owner_hash,
                thread_hash,
                type(error).__name__,
            )
            raise
        finally:
            pending = self._owner_tasks.get(owner_user_id)
            if pending is not None:
                pending.discard(current_task)
                if not pending:
                    self._owner_tasks.pop(owner_user_id, None)

    async def _retrieve_context(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
    ) -> None:
        owner_hash = _sha256_identifier(owner_user_id)
        thread_hash = _sha256_identifier(thread_id)
        started_ns = time.monotonic_ns()
        try:
            context = await asyncio.wait_for(
                self._retrieve_context_bounded(
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                ),
                timeout=self._settings.timeout_seconds,
            )
            context_bytes = context.encode("utf-8")
            context_sha256 = hashlib.sha256(context_bytes).hexdigest()
            latency_ms = max(
                0,
                round((time.monotonic_ns() - started_ns) / 1_000_000),
            )
            self._logger.info(
                "[zep_shadow] retrieval_succeeded owner_sha256=%s "
                "thread_sha256=%s context_sha256=%s context_bytes=%s "
                "latency_ms=%s prompt_bound=false",
                owner_hash,
                thread_hash,
                context_sha256,
                len(context_bytes),
                latency_ms,
            )
        except Exception as error:
            self._logger.error(
                "[zep_shadow] retrieval_failed owner_sha256=%s "
                "thread_sha256=%s error_type=%s prompt_bound=false",
                owner_hash,
                thread_hash,
                type(error).__name__,
            )

    async def _retrieve_context_bounded(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
    ) -> str:
        transport = await self._transport_client()
        return await transport.get_owner_context(
            user_id=zep_user_id_v1(owner_user_id),
            thread_id=zep_thread_id_v1(thread_id),
        )

    async def _search_prompt_context_bounded(
        self,
        *,
        owner_user_id: UUID,
        current_message: str,
    ) -> str:
        transport = await self._transport_client()
        return await transport.search_owner_context(
            user_id=zep_user_id_v1(owner_user_id),
            query=current_message,
        )

    async def _ensure_owner_thread(
        self,
        *,
        transport: ZepShadowTransportV1,
        user_id: str,
        thread_id: str,
    ) -> None:
        provision_key = (user_id, thread_id)
        if provision_key in self._provisioned_threads:
            return
        async with self._provision_lock:
            if provision_key not in self._provisioned_threads:
                await transport.ensure_user_and_thread(
                    user_id=user_id,
                    thread_id=thread_id,
                )
                self._provisioned_threads.add(provision_key)

    async def _write_turn(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> None:
        owner_hash = _sha256_identifier(owner_user_id)
        thread_hash = _sha256_identifier(thread_id)
        try:
            await asyncio.wait_for(
                self._write_turn_bounded(
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                ),
                timeout=self._settings.timeout_seconds,
            )
            self._logger.info(
                "[zep_shadow] write_succeeded owner_sha256=%s thread_sha256=%s",
                owner_hash,
                thread_hash,
            )
        except Exception as error:
            self._logger.error(
                "[zep_shadow] write_failed owner_sha256=%s thread_sha256=%s "
                "error_type=%s",
                owner_hash,
                thread_hash,
                type(error).__name__,
            )

    async def _write_turn_bounded(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> None:
        transport = await self._transport_client()
        user_id = zep_user_id_v1(owner_user_id)
        zep_thread_id = zep_thread_id_v1(thread_id)
        await self._ensure_owner_thread(
            transport=transport,
            user_id=user_id,
            thread_id=zep_thread_id,
        )
        await transport.add_turn(
            thread_id=zep_thread_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def close(self) -> None:
        pending = tuple(self._tasks)
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=self._settings.timeout_seconds,
                )
            except asyncio.TimeoutError:
                for task in pending:
                    task.cancel()
        if self._transport is not None:
            await self._transport.close()


__all__ = [
    "ZEP_API_KEY_ENV",
    "ZEP_SHADOW_MODE_CANARY",
    "ZEP_SHADOW_MODE_ENV",
    "ZEP_SHADOW_MODE_OFF",
    "ZEP_SHADOW_MODE_ON",
    "ZEP_SHADOW_OWNER_IDS_ENV",
    "ZEP_SHADOW_TIMEOUT_SECONDS_ENV",
    "ZepShadowConfigurationError",
    "ZepShadowOwnershipError",
    "ZepShadowRuntimeV1",
    "ZepShadowSettingsV1",
    "zep_thread_id_v1",
    "zep_user_id_v1",
]
