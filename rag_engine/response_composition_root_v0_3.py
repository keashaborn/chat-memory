from __future__ import annotations

"""Inactive downstream composition root for LifeSwitch structured context.

The existing response-policy root remains authoritative for authentication,
safety, response mode, Memory, FM, and prior web provenance. This root starts
with that exact trusted plan and adds only the separately governed LifeSwitch
selection, provider projection, and versioned finalization path.
"""

import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_chat_request_v3 import (
    OpenAIChatCompletionsAdapterV2,
    OpenAIChatResponseV3,
)
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from rag_engine.response_finalization_v2 import (
    FinalizedTrustedResponseV2,
    finalize_trusted_response_v2,
)
from rag_engine.response_lifeswitch_integration_v1 import (
    TrustedLifeSwitchResponsePlanV1,
)
from rag_engine.response_orchestration_v0_2 import TrustedResponsePlanV0_2


class LifeSwitchCompositionError(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("inactive LifeSwitch response composition failed")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class LifeSwitchResponseStageTimingsV1(_StrictFrozenModel):
    context_selection_ms: int = Field(ge=0)
    prompt_augmentation_ms: int = Field(ge=0)
    answer_generation_ms: int = Field(ge=0)
    finalization_ms: int = Field(ge=0)
    pipeline_total_ms: int = Field(ge=0)


class TrustedLifeSwitchResponseExecutionV1(_StrictFrozenModel):
    trusted_plan: TrustedLifeSwitchResponsePlanV1 = Field(repr=False)
    provider_response: OpenAIChatResponseV3 = Field(repr=False)
    finalized: FinalizedTrustedResponseV2 = Field(repr=False)
    stage_timings: LifeSwitchResponseStageTimingsV1

    @model_validator(mode="after")
    def exact_execution(self) -> "TrustedLifeSwitchResponseExecutionV1":
        if self.finalized.attestation.trusted_plan_sha256 != self.trusted_plan.plan_sha256:
            raise ValueError("finalization differs from LifeSwitch response plan")
        if (
            self.finalized.attestation.provider_response_sha256
            != self.provider_response.response_sha256
        ):
            raise ValueError("finalization differs from provider response")
        return self


class LifeSwitchContextPreparationProviderV1(Protocol):
    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> LifeSwitchPreparedContextV1 | Awaitable[LifeSwitchPreparedContextV1]: ...


async def _await_context(
    value: LifeSwitchPreparedContextV1 | Awaitable[LifeSwitchPreparedContextV1],
) -> LifeSwitchPreparedContextV1:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if not isinstance(value, LifeSwitchPreparedContextV1):
        raise TypeError("LifeSwitch provider returned the wrong type")
    return LifeSwitchPreparedContextV1.model_validate_json(value.model_dump_json())


def _elapsed_ms(start_ns: int) -> int:
    return max(0, round((time.monotonic_ns() - start_ns) / 1_000_000))


class InactiveLifeSwitchResponseCompositionRootV0_3:
    """Candidate-only seam after trusted response policy orchestration."""

    def __init__(
        self,
        *,
        openai_client: Any,
        context_provider: LifeSwitchContextPreparationProviderV1,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        answer_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if openai_client is None:
            raise ValueError("an injected OpenAI client is required")
        self._openai_client = openai_client
        self._context_provider = context_provider
        self._generation_config = generation_config or OpenAIChatGenerationConfigV1()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._answer_id_factory = answer_id_factory or uuid4

    async def execute(
        self,
        *,
        base_response_plan: TrustedResponsePlanV0_2,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> TrustedLifeSwitchResponseExecutionV1:
        stage = "validation"
        pipeline_started_ns = time.monotonic_ns()
        timings: dict[str, int] = {}
        try:
            base = TrustedResponsePlanV0_2.model_validate_json(
                base_response_plan.model_dump_json()
            )
            snapshot = ConversationSnapshotV1.model_validate_json(
                conversation_snapshot.model_dump_json()
            )
            if snapshot.authenticated_actor_user_id != base.authenticated_actor_user_id:
                raise ValueError("snapshot actor differs from response plan")
            if snapshot.thread_id != base.thread_id:
                raise ValueError("snapshot thread differs from response plan")
            if snapshot.current_request_id != base.policy_input.request_id:
                raise ValueError("snapshot request differs from response plan")
            if snapshot.snapshot_sha256 != base.conversation_snapshot_sha256:
                raise ValueError("snapshot hash differs from response plan")

            stage = "lifeswitch_context_selection"
            started_ns = time.monotonic_ns()
            context = await _await_context(
                self._context_provider.prepare(
                    authenticated_actor_user_id=base.authenticated_actor_user_id,
                    conversation_snapshot=snapshot,
                )
            )
            timings["context_selection_ms"] = _elapsed_ms(started_ns)

            stage = "lifeswitch_prompt_augmentation"
            started_ns = time.monotonic_ns()
            plan = TrustedLifeSwitchResponsePlanV1.create(
                base_response_plan=base,
                lifeswitch_context=context,
            )
            timings["prompt_augmentation_ms"] = _elapsed_ms(started_ns)

            stage = "answer_generation"
            started_ns = time.monotonic_ns()
            response = await OpenAIChatCompletionsAdapterV2(
                self._openai_client
            ).complete_async(
                plan,
                generation_config=self._generation_config,
            )
            timings["answer_generation_ms"] = _elapsed_ms(started_ns)

            stage = "finalization"
            started_ns = time.monotonic_ns()
            finalized = finalize_trusted_response_v2(
                trusted_plan=plan,
                provider_response=response,
                generation_config=self._generation_config,
                answer_id=self._answer_id_factory(),
                created_at=self._clock(),
            )
            timings["finalization_ms"] = _elapsed_ms(started_ns)
            return TrustedLifeSwitchResponseExecutionV1(
                trusted_plan=plan,
                provider_response=response,
                finalized=finalized,
                stage_timings=LifeSwitchResponseStageTimingsV1(
                    **timings,
                    pipeline_total_ms=_elapsed_ms(pipeline_started_ns),
                ),
            )
        except LifeSwitchCompositionError:
            raise
        except Exception:
            raise LifeSwitchCompositionError(stage) from None


__all__ = [
    "InactiveLifeSwitchResponseCompositionRootV0_3",
    "LifeSwitchCompositionError",
    "LifeSwitchContextPreparationProviderV1",
    "LifeSwitchResponseStageTimingsV1",
    "TrustedLifeSwitchResponseExecutionV1",
]
