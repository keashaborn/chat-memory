from __future__ import annotations

"""Optional failure-isolated observer for self-owned LifeSwitch V2 shadow runs."""

import datetime as dt
from typing import Protocol
from uuid import UUID

from rag_engine.lifeswitch_coaching_self_shadow_runner_v2 import (
    SelfShadowExecutionAuthorityV1,
    SelfShadowInspectionV1,
    run_lifeswitch_self_s1_shadow_v2,
)
from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchDomainContextEnvelopeV1,
    TrustedLifeSwitchContextRequestV1,
)


SELF_S1_SHADOW_OBSERVER_CONTRACT = "lifeswitch_self_s1_shadow_observer_v1"
SELF_S1_SHADOW_AUTHORIZATION_EPOCH = "lifeswitch-self-s1-shadow-policy-1"


class LifeSwitchSelfShadowInspectionSinkV1(Protocol):
    async def record(self, inspection: SelfShadowInspectionV1) -> None: ...


class LifeSwitchSelfShadowObserverV2:
    """Run the pure V2 shadow projection and expose only its safe inspection."""

    def __init__(
        self,
        sink: LifeSwitchSelfShadowInspectionSinkV1 | None = None,
        *,
        authorization_epoch: str = SELF_S1_SHADOW_AUTHORIZATION_EPOCH,
    ) -> None:
        if not authorization_epoch or len(authorization_epoch) > 256:
            raise ValueError("shadow authorization epoch is invalid")
        self._sink = sink
        self._authorization_epoch = authorization_epoch

    async def observe(
        self,
        *,
        request: TrustedLifeSwitchContextRequestV1,
        envelope: LifeSwitchDomainContextEnvelopeV1,
        context_snapshot_id: UUID,
        transaction_snapshot_digest: str,
        evaluated_at: dt.datetime,
    ) -> SelfShadowInspectionV1:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("shadow evaluation time must be timezone-aware")
        authority = SelfShadowExecutionAuthorityV1(
            context_snapshot_id=str(context_snapshot_id),
            transaction_snapshot_digest=transaction_snapshot_digest,
            authorization_epoch=self._authorization_epoch,
            evaluated_at=evaluated_at.astimezone(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        run = run_lifeswitch_self_s1_shadow_v2(
            request=request,
            envelope=envelope,
            authority=authority,
        )
        inspection = run.inspection
        if self._sink is not None:
            await self._sink.record(inspection)
        return inspection


__all__ = [
    "LifeSwitchSelfShadowInspectionSinkV1",
    "LifeSwitchSelfShadowObserverV2",
    "SELF_S1_SHADOW_AUTHORIZATION_EPOCH",
    "SELF_S1_SHADOW_OBSERVER_CONTRACT",
]
