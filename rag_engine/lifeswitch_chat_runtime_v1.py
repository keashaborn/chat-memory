from __future__ import annotations

"""Server-owned activation and dedicated pool for LifeSwitch chat context."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_coaching_self_shadow_observer_v2 import (
    LifeSwitchSelfShadowInspectionSinkV1,
    LifeSwitchSelfShadowObserverV2,
)
from rag_engine.lifeswitch_coaching_self_shadow_runner_v2 import (
    SelfShadowInspectionV1,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
    PostgresRestrictedLifeSwitchReadSessionV1,
)
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1


LifeSwitchRuntimeMode = Literal["off", "canary", "on"]
LifeSwitchSelfShadowRuntimeMode = Literal["off", "canary", "on"]
PoolFactory = Callable[..., Awaitable[asyncpg.Pool]]
logger = logging.getLogger("uvicorn.error")


class LifeSwitchChatRuntimeSettingsV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    mode: LifeSwitchRuntimeMode = "off"
    dsn: str | None = Field(default=None, repr=False)
    canary_owner_ids: frozenset[UUID] = frozenset()
    self_shadow_mode: LifeSwitchSelfShadowRuntimeMode = "off"
    self_shadow_canary_owner_ids: frozenset[UUID] = frozenset()
    pool_max_size: int = Field(default=4, ge=1, le=8)
    command_timeout_seconds: float = Field(default=15.0, ge=1.0, le=30.0)

    @field_validator("dsn")
    @classmethod
    def nonempty_dsn(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def active_configuration_is_complete(self) -> "LifeSwitchChatRuntimeSettingsV1":
        if self.mode != "off" and self.dsn is None:
            raise ValueError("active LifeSwitch chat context requires a dedicated DSN")
        if self.mode == "canary" and not self.canary_owner_ids:
            raise ValueError("LifeSwitch canary mode requires an owner allowlist")
        if self.mode != "canary" and self.canary_owner_ids:
            raise ValueError("LifeSwitch canary owners require canary mode")
        if self.self_shadow_mode != "off" and self.mode == "off":
            raise ValueError("LifeSwitch self shadow requires active context")
        if self.self_shadow_mode == "canary" and not self.self_shadow_canary_owner_ids:
            raise ValueError("LifeSwitch self shadow canary requires an owner allowlist")
        if self.self_shadow_mode != "canary" and self.self_shadow_canary_owner_ids:
            raise ValueError("LifeSwitch self shadow owners require canary mode")
        return self

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "LifeSwitchChatRuntimeSettingsV1":
        values = environment if environment is not None else os.environ
        mode = (values.get("LIFESWITCH_CHAT_CONTEXT_MODE") or "off").strip().lower()
        if mode not in {"off", "canary", "on"}:
            raise ValueError("invalid LifeSwitch chat context mode")
        raw_owners = values.get("LIFESWITCH_CHAT_CANARY_OWNER_IDS") or ""
        owners = frozenset(
            UUID(item.strip())
            for item in raw_owners.split(",")
            if item.strip()
        )
        raw_pool_size = values.get("LIFESWITCH_CHAT_POOL_MAX_SIZE") or "4"
        try:
            pool_max_size = int(raw_pool_size)
        except ValueError:
            raise ValueError("invalid LifeSwitch pool size") from None
        self_shadow_mode = (
            values.get("LIFESWITCH_COACHING_CONTEXT_V2_SHADOW_MODE") or "off"
        ).strip().lower()
        if self_shadow_mode not in {"off", "canary", "on"}:
            raise ValueError("invalid LifeSwitch self shadow mode")
        raw_shadow_owners = (
            values.get("LIFESWITCH_COACHING_CONTEXT_V2_SHADOW_CANARY_OWNER_IDS")
            or ""
        )
        shadow_owners = frozenset(
            UUID(item.strip())
            for item in raw_shadow_owners.split(",")
            if item.strip()
        )
        return cls(
            mode=mode,
            dsn=values.get("LIFESWITCH_CHAT_POSTGRES_DSN"),
            canary_owner_ids=owners,
            self_shadow_mode=self_shadow_mode,
            self_shadow_canary_owner_ids=shadow_owners,
            pool_max_size=pool_max_size,
        )

    def enabled_for(self, owner_user_id: UUID) -> bool:
        if self.mode == "off":
            return False
        if self.mode == "on":
            return True
        return owner_user_id in self.canary_owner_ids

    def self_shadow_enabled_for(self, owner_user_id: UUID) -> bool:
        if not self.enabled_for(owner_user_id):
            return False
        if self.self_shadow_mode == "off":
            return False
        if self.self_shadow_mode == "on":
            return True
        return owner_user_id in self.self_shadow_canary_owner_ids


class LoggingLifeSwitchSelfShadowInspectionSinkV1:
    """Emit only the validated content-free shadow inspection."""

    async def record(self, inspection: SelfShadowInspectionV1) -> None:
        safe = SelfShadowInspectionV1.model_validate_json(
            inspection.model_dump_json()
        )
        logger.info("lifeswitch_self_s1_shadow %s", safe.model_dump_json())


async def _initialize_connection_v1(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        """
        select
          session_user,
          current_user,
          coalesce((select rolsuper from pg_catalog.pg_roles where rolname=session_user),true)
            as is_superuser,
          coalesce((select rolbypassrls from pg_catalog.pg_roles where rolname=session_user),true)
            as bypasses_rls
        """
    )
    if row is None:
        raise RuntimeError("LifeSwitch pool identity is unavailable")
    if row["session_user"] != "brains_app" or row["current_user"] != "brains_app":
        raise RuntimeError("LifeSwitch pool must connect as brains_app")
    if row["is_superuser"] or row["bypasses_rls"]:
        raise RuntimeError("LifeSwitch pool role has excessive privileges")


async def _reset_connection_v1(conn: asyncpg.Connection) -> None:
    await conn.reset()


class LifeSwitchChatPoolManagerV1:
    """Create no connections until an authorized LifeSwitch read is required."""

    def __init__(
        self,
        settings: LifeSwitchChatRuntimeSettingsV1,
        *,
        pool_factory: PoolFactory = asyncpg.create_pool,
    ) -> None:
        self.settings = settings
        self._pool_factory = pool_factory
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def pool(self) -> asyncpg.Pool:
        if self.settings.mode == "off" or self.settings.dsn is None:
            raise RuntimeError("LifeSwitch chat context is disabled")
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await self._pool_factory(
                    dsn=self.settings.dsn,
                    min_size=0,
                    max_size=self.settings.pool_max_size,
                    max_queries=500,
                    max_inactive_connection_lifetime=60.0,
                    command_timeout=self.settings.command_timeout_seconds,
                    init=_initialize_connection_v1,
                    reset=_reset_connection_v1,
                    server_settings={
                        "application_name": "brains_lifeswitch_chat_context_v1",
                        "statement_timeout": "15000",
                        "lock_timeout": "1000",
                        "idle_in_transaction_session_timeout": "15000",
                    },
                )
        return self._pool

    async def close(self) -> None:
        async with self._lock:
            pool = self._pool
            self._pool = None
        if pool is not None:
            await pool.close()


class LazyPostgresRestrictedLifeSwitchReadSessionV1:
    """Preserve OFF zero-read behavior while sharing one dedicated pool."""

    def __init__(
        self,
        pool_manager: LifeSwitchChatPoolManagerV1,
        *,
        self_shadow_sink: LifeSwitchSelfShadowInspectionSinkV1 | None = None,
    ) -> None:
        self._pool_manager = pool_manager
        self._self_shadow_sink = (
            self_shadow_sink or LoggingLifeSwitchSelfShadowInspectionSinkV1()
        )

    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        query: str,
    ) -> LifeSwitchPreparedContextV1:
        pool = await self._pool_manager.pool()
        observer = None
        if self._pool_manager.settings.self_shadow_enabled_for(
            authenticated_actor_user_id
        ):
            observer = LifeSwitchSelfShadowObserverV2(self._self_shadow_sink)
        return await PostgresRestrictedLifeSwitchReadSessionV1(
            pool,
            self_shadow_observer=observer,
        ).select(
            authenticated_actor_user_id=authenticated_actor_user_id,
            conversation_snapshot=conversation_snapshot,
            query=query,
        )


__all__ = [
    "LazyPostgresRestrictedLifeSwitchReadSessionV1",
    "LifeSwitchSelfShadowRuntimeMode",
    "LoggingLifeSwitchSelfShadowInspectionSinkV1",
    "LifeSwitchChatPoolManagerV1",
    "LifeSwitchChatRuntimeSettingsV1",
    "LifeSwitchRuntimeMode",
]
