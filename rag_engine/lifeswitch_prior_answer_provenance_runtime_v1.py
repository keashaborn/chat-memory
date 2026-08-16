from __future__ import annotations

"""Optional, owner-bound retrieval of prior LifeSwitch answer provenance."""

import hashlib
import json
from typing import Any, Literal, Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_engine.prior_lifeswitch_provenance_v1 import (
    PriorLifeSwitchProvenanceEnvelopeV1,
    prior_lifeswitch_provenance_requested_v1,
    select_prior_lifeswitch_provenance_v1,
)
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1


PRIOR_LIFESWITCH_PREPARED_V1 = "prior_lifeswitch_prepared_context_v1"
LIFESWITCH_READER_ROLE = "lifeswitch_chat_reader"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json")
                if isinstance(item, BaseModel)
                else str(item)
            ),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class PriorLifeSwitchPreparedContextV1(_StrictFrozenModel):
    contract_version: Literal[PRIOR_LIFESWITCH_PREPARED_V1] = (
        PRIOR_LIFESWITCH_PREPARED_V1
    )
    status: Literal["OFF", "EMPTY", "SELECTED", "UNAVAILABLE"]
    database_accessed: bool
    envelope: PriorLifeSwitchProvenanceEnvelopeV1 | None = Field(
        default=None,
        repr=False,
    )
    manifest_sha256: str

    @model_validator(mode="after")
    def coherent(self) -> "PriorLifeSwitchPreparedContextV1":
        if self.status == "OFF" and self.database_accessed:
            raise ValueError("OFF must perform zero provenance reads")
        if self.status == "SELECTED":
            if not self.database_accessed or self.envelope is None:
                raise ValueError("selected provenance requires a database result")
        elif self.envelope is not None:
            raise ValueError("non-selected provenance cannot carry an envelope")
        if self.status in {"EMPTY", "UNAVAILABLE"} and not self.database_accessed:
            raise ValueError("database result status requires an attempted read")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != _sha256(payload):
            raise ValueError("prepared prior provenance hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        status: Literal["OFF", "EMPTY", "SELECTED", "UNAVAILABLE"],
        database_accessed: bool,
        envelope: PriorLifeSwitchProvenanceEnvelopeV1 | None = None,
    ) -> "PriorLifeSwitchPreparedContextV1":
        values = {
            "contract_version": PRIOR_LIFESWITCH_PREPARED_V1,
            "status": status,
            "database_accessed": database_accessed,
            "envelope": envelope,
        }
        return cls(**values, manifest_sha256=_sha256(values))


class PriorLifeSwitchRestrictedReadSessionV1(Protocol):
    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1: ...


class InactivePriorLifeSwitchProvenanceProviderV1:
    """Return OFF without opening the retired provenance database path."""

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1:
        snapshot = ConversationSnapshotV1.model_validate_json(
            conversation_snapshot.model_dump_json()
        )
        if snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise ValueError("prior provenance actor differs from snapshot")
        return PriorLifeSwitchPreparedContextV1.create(
            status="OFF",
            database_accessed=False,
        )


class PriorLifeSwitchProvenanceProviderV1:
    """Run the narrow source-question trigger before opening a DB session."""

    def __init__(self, restricted_session: PriorLifeSwitchRestrictedReadSessionV1) -> None:
        self._restricted_session = restricted_session

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1:
        snapshot = ConversationSnapshotV1.model_validate_json(
            conversation_snapshot.model_dump_json()
        )
        if snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise ValueError("prior provenance actor differs from snapshot")
        if not prior_lifeswitch_provenance_requested_v1(snapshot.messages[-1].content):
            return PriorLifeSwitchPreparedContextV1.create(
                status="OFF",
                database_accessed=False,
            )
        try:
            return await self._restricted_session.select(
                authenticated_actor_user_id=authenticated_actor_user_id,
                conversation_snapshot=snapshot,
            )
        except Exception:
            return PriorLifeSwitchPreparedContextV1.create(
                status="UNAVAILABLE",
                database_accessed=True,
            )


class PostgresPriorLifeSwitchRestrictedReadSessionV1:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1:
        snapshot = ConversationSnapshotV1.model_validate_json(
            conversation_snapshot.model_dump_json()
        )
        if snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise ValueError("prior provenance session actor differs from snapshot")
        async with self._pool.acquire() as conn:
            context_id: UUID | None = None
            async with conn.transaction():
                await conn.execute("set transaction read write")
                await conn.execute(
                    "select set_config('app.user_id',$1,true)",
                    str(authenticated_actor_user_id),
                )
                await conn.execute(
                    "select set_config('app.lifeswitch_owner_id',$1,true)",
                    str(authenticated_actor_user_id),
                )
                context_id = await conn.fetchval(
                    "select lifeswitch_chat.begin_owner_read_context_v1($1,$2,$3,$4)",
                    authenticated_actor_user_id,
                    snapshot.thread_id,
                    _text_sha256(snapshot.current_request_id),
                    snapshot.snapshot_sha256,
                )
                if not isinstance(context_id, UUID):
                    raise ValueError("prior provenance gateway returned no context")
            try:
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    await conn.execute(
                        "select set_config('app.user_id',$1,true)",
                        str(authenticated_actor_user_id),
                    )
                    await conn.execute(
                        "select set_config('app.lifeswitch_owner_id',$1,true)",
                        str(authenticated_actor_user_id),
                    )
                    await conn.execute(f"set local role {LIFESWITCH_READER_ROLE}")
                    envelope = await select_prior_lifeswitch_provenance_v1(
                        conn,
                        context_id=context_id,
                        authenticated_actor_user_id=authenticated_actor_user_id,
                        conversation_snapshot=snapshot,
                    )
                return PriorLifeSwitchPreparedContextV1.create(
                    status="SELECTED" if envelope is not None else "EMPTY",
                    database_accessed=True,
                    envelope=envelope,
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
                            raise RuntimeError("prior provenance gateway cleanup failed")


class LazyPostgresPriorLifeSwitchRestrictedReadSessionV1:
    """Obtain the existing dedicated pool only after the provenance trigger."""

    def __init__(self, pool_manager: Any) -> None:
        self._pool_manager = pool_manager

    async def select(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1:
        pool = await self._pool_manager.pool()
        return await PostgresPriorLifeSwitchRestrictedReadSessionV1(pool).select(
            authenticated_actor_user_id=authenticated_actor_user_id,
            conversation_snapshot=conversation_snapshot,
        )


__all__ = [
    "InactivePriorLifeSwitchProvenanceProviderV1",
    "PostgresPriorLifeSwitchRestrictedReadSessionV1",
    "LazyPostgresPriorLifeSwitchRestrictedReadSessionV1",
    "PriorLifeSwitchPreparedContextV1",
    "PriorLifeSwitchProvenanceProviderV1",
    "PriorLifeSwitchRestrictedReadSessionV1",
]
