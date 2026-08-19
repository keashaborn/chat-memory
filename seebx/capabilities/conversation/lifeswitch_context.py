from __future__ import annotations

"""LifeSwitch context decision, validation, and preparation contracts."""

import datetime as dt
import hashlib
import json
from typing import Any, Callable, Literal, Protocol
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

from seebx.capabilities.plans.data_plan import (
    LifeSwitchDataPlanV1,
    create_lifeswitch_data_plan_v1,
)
from seebx.capabilities.plans.domain_context import (
    LifeSwitchDomainContextEnvelopeV1,
    LifeSwitchRenderedContextV1,
    render_lifeswitch_context_v1,
)
from seebx.capabilities.conversation.snapshot import ConversationSnapshotV1


LIFESWITCH_PREPARED_CONTEXT_VERSION = "lifeswitch_prepared_context_v1"

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




__all__ = [
    "LifeSwitchPreparedContextV1",
    "LifeSwitchPreparationStatus",
    "LifeSwitchResponseContextProviderV1",
    "LifeSwitchRestrictedReadSessionV1",
    "LifeSwitchTimezoneSource",
]
