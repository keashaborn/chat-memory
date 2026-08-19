from __future__ import annotations

"""Restricted PostgreSQL adapter for LifeSwitch conversation context."""

import datetime as dt
import hashlib
import logging
from typing import Callable
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from seebx.capabilities.coaching.shadow_observer import (
    LifeSwitchSelfShadowObserverV2,
)
from seebx.capabilities.plans.data_plan import create_lifeswitch_data_plan_v1
from seebx.capabilities.plans.domain_context import (
    LifeSwitchDomainContextEnvelopeV1,
    TrustedLifeSwitchContextRequestV1,
    render_lifeswitch_context_v1,
)
from seebx.capabilities.plans.domain_provider import LifeSwitchDomainContextProviderV1
from seebx.adapters.lifeswitch_domain_postgres import (
    PostgresLifeSwitchDomainReaderV1,
)
from seebx.capabilities.conversation.lifeswitch_context import (
    LifeSwitchPreparedContextV1,
    LifeSwitchTimezoneSource,
)
from seebx.capabilities.conversation.snapshot import ConversationSnapshotV1


LIFESWITCH_READER_ROLE = "lifeswitch_chat_reader"
logger = logging.getLogger(__name__)


def _text_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class PostgresRestrictedLifeSwitchReadSessionV1:
    """Bind trusted ownership, then read only through restricted gateways."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        utc_clock: Callable[[], dt.datetime] | None = None,
        self_shadow_observer: LifeSwitchSelfShadowObserverV2 | None = None,
    ) -> None:
        self._pool = pool
        self._utc_clock = utc_clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._self_shadow_observer = self_shadow_observer

    async def _observe_self_shadow(
        self,
        *,
        conn: asyncpg.Connection,
        trusted: TrustedLifeSwitchContextRequestV1,
        envelope: LifeSwitchDomainContextEnvelopeV1,
        context_id: UUID,
        evaluated_at: dt.datetime,
    ) -> None:
        if self._self_shadow_observer is None:
            return
        if envelope.status not in {"SELECTED", "PARTIAL"}:
            return
        try:
            snapshot = await conn.fetchval("select pg_current_snapshot()::text")
            snapshot_text = str(snapshot or "").strip()
            if not snapshot_text:
                raise ValueError("transaction snapshot is unavailable")
            await self._self_shadow_observer.observe(
                request=trusted,
                envelope=envelope,
                context_snapshot_id=context_id,
                transaction_snapshot_digest=_text_sha256(snapshot_text),
                evaluated_at=evaluated_at,
            )
        except Exception:
            logger.warning("LifeSwitch V2 self shadow observation unavailable")

    @staticmethod
    async def _owner_timezone(
        conn: asyncpg.Connection,
        context_id: UUID,
    ) -> tuple[str | None, LifeSwitchTimezoneSource]:
        row = await conn.fetchrow(
            "select * from lifeswitch_chat.read_owner_timezone_v1($1)",
            context_id,
        )
        if row is None:
            return None, "unavailable"
        value = str(row["timezone_name"] or "").strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return None, "unavailable"
        source = str(row["timezone_source"] or "")
        if source not in {"account_setting", "reviewed_migration", "active_plan"}:
            return None, "unavailable"
        return value, "active_plan" if source == "active_plan" else "account_timezone"

    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        query: str,
    ) -> LifeSwitchPreparedContextV1:
        if conversation_snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise ValueError("LifeSwitch session actor differs from snapshot")
        async with self._pool.acquire() as conn:
            context_id: UUID | None = None
            async with conn.transaction():
                await conn.execute("set transaction read write")
                await conn.execute(
                    "select set_config('app.user_id', $1, true)",
                    str(authenticated_actor_user_id),
                )
                await conn.execute(
                    "select set_config('app.lifeswitch_owner_id', $1, true)",
                    str(authenticated_actor_user_id),
                )
                context_id = await conn.fetchval(
                    """
                    select lifeswitch_chat.begin_owner_read_context_v1(
                      $1,$2,$3,$4
                    )
                    """,
                    authenticated_actor_user_id,
                    conversation_snapshot.thread_id,
                    _text_sha256(conversation_snapshot.current_request_id),
                    conversation_snapshot.snapshot_sha256,
                )
                if not isinstance(context_id, UUID):
                    raise ValueError("LifeSwitch owner gateway did not return a UUID")
            try:
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    await conn.execute(
                        "select set_config('app.user_id', $1, true)",
                        str(authenticated_actor_user_id),
                    )
                    await conn.execute(
                        "select set_config('app.lifeswitch_owner_id', $1, true)",
                        str(authenticated_actor_user_id),
                    )
                    await conn.execute(f"set local role {LIFESWITCH_READER_ROLE}")
                    owner_timezone, source = await self._owner_timezone(
                        conn,
                        context_id,
                    )
                    now = self._utc_clock()
                    if now.tzinfo is None or now.utcoffset() is None:
                        raise ValueError("LifeSwitch read clock must be timezone-aware")
                    if owner_timezone is None:
                        plan = create_lifeswitch_data_plan_v1(
                            query,
                            today=now.astimezone(dt.timezone.utc).date(),
                        )
                        return LifeSwitchPreparedContextV1.create(
                            status="TIMEZONE_UNAVAILABLE",
                            timezone_source="unavailable",
                            database_accessed=True,
                            data_plan=plan,
                        )
                    local_today = now.astimezone(ZoneInfo(owner_timezone)).date()
                    plan = create_lifeswitch_data_plan_v1(query, today=local_today)
                    trusted = TrustedLifeSwitchContextRequestV1.create(
                        request_id=conversation_snapshot.current_request_id,
                        authenticated_actor_user_id=authenticated_actor_user_id,
                        owner_user_id=authenticated_actor_user_id,
                        thread_id=conversation_snapshot.thread_id,
                        conversation_snapshot_sha256=conversation_snapshot.snapshot_sha256,
                        owner_timezone=owner_timezone,
                        query=query,
                        data_plan=plan,
                    )
                    envelope = await LifeSwitchDomainContextProviderV1(
                        PostgresLifeSwitchDomainReaderV1(
                            conn,
                            context_id=context_id,
                        )
                    ).select(trusted)
                    await self._observe_self_shadow(
                        conn=conn,
                        trusted=trusted,
                        envelope=envelope,
                        context_id=context_id,
                        evaluated_at=now,
                    )
                    rendered = render_lifeswitch_context_v1(envelope)
                    return LifeSwitchPreparedContextV1.create(
                        status=envelope.status,
                        timezone_source=source,
                        database_accessed=True,
                        data_plan=plan,
                        envelope=envelope,
                        rendered=rendered,
                    )
            finally:
                if context_id is not None:
                    async with conn.transaction():
                        await conn.execute("set transaction read write")
                        removed = await conn.fetchval(
                            "select lifeswitch_chat.end_owner_read_context_v1($1)",
                            context_id,
                        )
                        if removed is not True:
                            raise RuntimeError("LifeSwitch owner gateway cleanup failed")


__all__ = [
    "PostgresRestrictedLifeSwitchReadSessionV1",
]
