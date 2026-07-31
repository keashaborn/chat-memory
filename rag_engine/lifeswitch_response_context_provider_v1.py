from __future__ import annotations

"""Backend-owned LifeSwitch selection with an isolated read-only transaction."""

import datetime as dt
import hashlib
import json
from typing import Any, Callable, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_engine.lifeswitch_data_plan_v1 import (
    LifeSwitchDataPlanV1,
    create_lifeswitch_data_plan_v1,
)
from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchDomainContextEnvelopeV1,
    LifeSwitchRenderedContextV1,
    TrustedLifeSwitchContextRequestV1,
    render_lifeswitch_context_v1,
)
from rag_engine.lifeswitch_domain_provider_v1 import LifeSwitchDomainContextProviderV1
from rag_engine.lifeswitch_postgres_domain_reader_v1 import (
    PostgresLifeSwitchDomainReaderV1,
)
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1


LIFESWITCH_PREPARED_CONTEXT_VERSION = "lifeswitch_prepared_context_v1"
LIFESWITCH_READER_ROLE = "lifeswitch_chat_reader_v1"

LifeSwitchPreparationStatus = Literal[
    "OFF",
    "TIMEZONE_UNAVAILABLE",
    "EMPTY",
    "SELECTED",
    "PARTIAL",
]
LifeSwitchTimezoneSource = Literal[
    "not_requested",
    "account_timezone",
    "active_plan",
    "unavailable",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class LifeSwitchPreparedContextV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PREPARED_CONTEXT_VERSION] = (
        LIFESWITCH_PREPARED_CONTEXT_VERSION
    )
    status: LifeSwitchPreparationStatus
    timezone_source: LifeSwitchTimezoneSource
    database_accessed: bool
    data_plan: LifeSwitchDataPlanV1
    envelope: LifeSwitchDomainContextEnvelopeV1 | None = Field(
        default=None,
        repr=False,
    )
    rendered: LifeSwitchRenderedContextV1 | None = Field(default=None, repr=False)
    manifest_sha256: str

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchPreparedContextV1":
        if self.status == "OFF":
            if self.database_accessed or self.timezone_source != "not_requested":
                raise ValueError("OFF must perform zero LifeSwitch database reads")
            if self.data_plan.data_access or self.envelope or self.rendered:
                raise ValueError("OFF cannot carry LifeSwitch context")
        elif self.status == "TIMEZONE_UNAVAILABLE":
            if not self.database_accessed or self.timezone_source != "unavailable":
                raise ValueError("timezone failure must be a closed database result")
            if self.envelope or self.rendered:
                raise ValueError("timezone failure cannot carry context")
        else:
            if not self.database_accessed or self.envelope is None:
                raise ValueError("selected LifeSwitch result requires database context")
            if self.envelope.status != self.status:
                raise ValueError("prepared status differs from LifeSwitch envelope")
            expected = render_lifeswitch_context_v1(self.envelope)
            if self.rendered != expected:
                raise ValueError("prepared LifeSwitch render is not canonical")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != _sha256(payload):
            raise ValueError("prepared LifeSwitch manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        status: LifeSwitchPreparationStatus,
        timezone_source: LifeSwitchTimezoneSource,
        database_accessed: bool,
        data_plan: LifeSwitchDataPlanV1,
        envelope: LifeSwitchDomainContextEnvelopeV1 | None = None,
        rendered: LifeSwitchRenderedContextV1 | None = None,
    ) -> "LifeSwitchPreparedContextV1":
        payload = {
            "contract_version": LIFESWITCH_PREPARED_CONTEXT_VERSION,
            "status": status,
            "timezone_source": timezone_source,
            "database_accessed": database_accessed,
            "data_plan": data_plan,
            "envelope": envelope,
            "rendered": rendered,
        }
        return cls(**payload, manifest_sha256=_sha256(payload))


class LifeSwitchRestrictedReadSessionV1(Protocol):
    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        query: str,
    ) -> LifeSwitchPreparedContextV1: ...


class LifeSwitchResponseContextProviderV1:
    """Run the semantic OFF gate before opening any database session."""

    def __init__(
        self,
        restricted_session: LifeSwitchRestrictedReadSessionV1,
        *,
        utc_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._restricted_session = restricted_session
        self._utc_clock = utc_clock or (lambda: dt.datetime.now(dt.timezone.utc))

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> LifeSwitchPreparedContextV1:
        if conversation_snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise ValueError("LifeSwitch actor differs from conversation snapshot")
        query = conversation_snapshot.messages[-1].content
        probe_day = self._utc_clock().astimezone(dt.timezone.utc).date()
        preliminary = create_lifeswitch_data_plan_v1(query, today=probe_day)
        if not preliminary.data_access:
            return LifeSwitchPreparedContextV1.create(
                status="OFF",
                timezone_source="not_requested",
                database_accessed=False,
                data_plan=preliminary,
            )
        return await self._restricted_session.select(
            authenticated_actor_user_id=authenticated_actor_user_id,
            conversation_snapshot=conversation_snapshot,
            query=query,
        )


class PostgresRestrictedLifeSwitchReadSessionV1:
    """Use only the no-login LifeSwitch reader role in one repeatable snapshot."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        utc_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._utc_clock = utc_clock or (lambda: dt.datetime.now(dt.timezone.utc))

    @staticmethod
    async def _owner_timezone(
        conn: asyncpg.Connection,
        owner_user_id: UUID,
    ) -> tuple[str | None, LifeSwitchTimezoneSource]:
        row = await conn.fetchrow(
            """
            /* lifeswitch_chat_context:trusted_owner_timezone */
            with candidate as (
              select timezone_name, 1 as precedence
              from lifeswitch_chat.account_timezone_v1
              where owner_user_id = $1
              union all
              select version.owner_timezone as timezone_name, 2 as precedence
              from lifeswitch_agentic.plan_owner_state state
              join lifeswitch_agentic.plan_versions version
                on version.owner_user_id = state.owner_user_id
               and version.id = state.active_plan_version_id
              where state.owner_user_id = $1
            )
            select timezone_name, precedence
            from candidate
            order by precedence
            limit 1
            """,
            owner_user_id,
        )
        if row is None:
            return None, "unavailable"
        value = str(row["timezone_name"] or "").strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return None, "unavailable"
        return value, "account_timezone" if int(row["precedence"]) == 1 else "active_plan"

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
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                await conn.execute(
                    "select set_config('app.lifeswitch_owner_id', $1, true)",
                    str(authenticated_actor_user_id),
                )
                await conn.execute(f"set local role {LIFESWITCH_READER_ROLE}")
                owner_timezone, source = await self._owner_timezone(
                    conn,
                    authenticated_actor_user_id,
                )
                if owner_timezone is None:
                    now = self._utc_clock()
                    if now.tzinfo is None or now.utcoffset() is None:
                        raise ValueError("LifeSwitch read clock must be timezone-aware")
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
                now = self._utc_clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("LifeSwitch read clock must be timezone-aware")
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
                    PostgresLifeSwitchDomainReaderV1(conn)
                ).select(trusted)
                rendered = render_lifeswitch_context_v1(envelope)
                return LifeSwitchPreparedContextV1.create(
                    status=envelope.status,
                    timezone_source=source,
                    database_accessed=True,
                    data_plan=plan,
                    envelope=envelope,
                    rendered=rendered,
                )


__all__ = [
    "LifeSwitchPreparedContextV1",
    "LifeSwitchResponseContextProviderV1",
    "LifeSwitchRestrictedReadSessionV1",
    "PostgresRestrictedLifeSwitchReadSessionV1",
]
