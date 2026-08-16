from __future__ import annotations

"""Write-only Zep canary that is disconnected from response context."""

import asyncio
import hashlib
import inspect
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol
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


class ZepShadowConfigurationError(ValueError):
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
                role="user",
                name="LifeSwitch User",
                content=user_message,
                metadata={
                    "lifeswitch_message_id": str(user_message_id),
                },
            ),
            self._message_type(
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

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _default_transport_factory(api_key: str) -> ZepShadowTransportV1:
    return ZepCloudShadowTransportV1(api_key)


class ZepShadowRuntimeV1:
    """Schedules canary writes without exposing Zep output to responses."""

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
        self._provision_lock = asyncio.Lock()
        self._provisioned_threads: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()

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

        task = asyncio.create_task(
            self._write_turn(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return "scheduled"

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
        if self._transport is None:
            self._transport = self._transport_factory(self._api_key)
        user_id = zep_user_id_v1(owner_user_id)
        zep_thread_id = zep_thread_id_v1(thread_id)
        provision_key = (user_id, zep_thread_id)
        if provision_key not in self._provisioned_threads:
            async with self._provision_lock:
                if provision_key not in self._provisioned_threads:
                    await self._transport.ensure_user_and_thread(
                        user_id=user_id,
                        thread_id=zep_thread_id,
                    )
                    self._provisioned_threads.add(provision_key)
        await self._transport.add_turn(
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
    "ZepShadowRuntimeV1",
    "ZepShadowSettingsV1",
    "zep_thread_id_v1",
    "zep_user_id_v1",
]
