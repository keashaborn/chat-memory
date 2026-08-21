from __future__ import annotations

"""Immutable OFF contract for the retired prior-answer provenance path."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from seebx.capabilities.conversation.prior_lifeswitch_provenance import (
    PriorLifeSwitchProvenanceEnvelopeV1,
)
from seebx.capabilities.conversation.snapshot import ConversationSnapshotV1


PRIOR_LIFESWITCH_PREPARED_V1 = "prior_lifeswitch_prepared_context_v1"


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



__all__ = [
    "InactivePriorLifeSwitchProvenanceProviderV1",
    "PriorLifeSwitchPreparedContextV1",
]
