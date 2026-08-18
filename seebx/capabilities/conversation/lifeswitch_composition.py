from __future__ import annotations

"""LifeSwitch context augmentation for canonical conversation responses.

The generic conversation composer remains authoritative for identity, safety,
memory, search, attachments, and the trusted base plan. This module adds only
owner-scoped LifeSwitch context and LifeSwitch-specific answer generation.
"""

import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from seebx.capabilities.conversation.memory_contracts import (
    MemoryAnswerProvenanceV1,
)
from rag_engine.lifeswitch_prior_answer_provenance_runtime_v1 import (
    PriorLifeSwitchPreparedContextV1,
)
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_chat_request_v4 import (
    OpenAIChatCompletionsAdapterV3,
    OpenAIChatRequestV4,
    OpenAIChatResponseV3,
)
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from seebx.capabilities.conversation.composition import (
    AuthenticatedResponseCommandV0_2,
    ConversationResponseComposer,
)
from rag_engine.response_finalization_v3 import (
    FinalizedTrustedResponseV3,
    finalize_trusted_response_v3,
)
from rag_engine.response_lifeswitch_integration_v2 import (
    TrustedLifeSwitchResponsePlanV2,
)
from rag_engine.response_orchestration_v0_2 import TrustedResponsePlanV0_2


class LifeSwitchCompositionError(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("LifeSwitch response composition failed")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class LifeSwitchResponseStageTimingsV2(_StrictFrozenModel):
    context_selection_ms: int = Field(ge=0)
    prior_lifeswitch_provenance_selection_ms: int = Field(ge=0)
    prompt_augmentation_ms: int = Field(ge=0)
    answer_generation_ms: int = Field(ge=0)
    finalization_ms: int = Field(ge=0)
    pipeline_total_ms: int = Field(ge=0)


class IntegratedLifeSwitchResponseStageTimingsV2(_StrictFrozenModel):
    command_validation_ms: int = Field(ge=0)
    conversation_snapshot_ms: int = Field(ge=0)
    policy_input_ms: int = Field(ge=0)
    signal_classification_ms: int = Field(ge=0)
    signal_binding_ms: int = Field(ge=0)
    memory_selection_ms: int = Field(ge=0)
    trusted_request_ms: int = Field(ge=0)
    orchestration_ms: int = Field(ge=0)
    lifeswitch_context_selection_ms: int = Field(ge=0)
    prior_lifeswitch_provenance_selection_ms: int = Field(ge=0)
    lifeswitch_prompt_augmentation_ms: int = Field(ge=0)
    answer_generation_ms: int = Field(ge=0)
    finalization_ms: int = Field(ge=0)
    pipeline_total_ms: int = Field(ge=0)


class TrustedLifeSwitchResponseExecutionV2(_StrictFrozenModel):
    trusted_plan: TrustedLifeSwitchResponsePlanV2 = Field(repr=False)
    provider_response: OpenAIChatResponseV3 = Field(repr=False)
    finalized: FinalizedTrustedResponseV3 = Field(repr=False)
    stage_timings: LifeSwitchResponseStageTimingsV2

    @model_validator(mode="after")
    def exact_execution(self) -> "TrustedLifeSwitchResponseExecutionV2":
        if self.finalized.attestation.trusted_plan_sha256 != self.trusted_plan.plan_sha256:
            raise ValueError("finalization differs from LifeSwitch response plan")
        if (
            self.finalized.attestation.provider_response_sha256
            != self.provider_response.response_sha256
        ):
            raise ValueError("finalization differs from provider response")
        return self


class IntegratedTrustedLifeSwitchResponseExecutionV2(_StrictFrozenModel):
    trusted_plan: TrustedLifeSwitchResponsePlanV2 = Field(repr=False)
    provider_response: OpenAIChatResponseV3 = Field(repr=False)
    finalized: FinalizedTrustedResponseV3 = Field(repr=False)
    successor_memory_provenance: MemoryAnswerProvenanceV1 | None = Field(
        default=None,
        repr=False,
        exclude_if=lambda value: value is None,
    )
    stage_timings: IntegratedLifeSwitchResponseStageTimingsV2

    @model_validator(mode="after")
    def exact_execution(self) -> "IntegratedTrustedLifeSwitchResponseExecutionV2":
        if self.finalized.attestation.trusted_plan_sha256 != self.trusted_plan.plan_sha256:
            raise ValueError("finalization differs from LifeSwitch response plan")
        if (
            self.finalized.attestation.provider_response_sha256
            != self.provider_response.response_sha256
        ):
            raise ValueError("finalization differs from provider response")
        if self.successor_memory_provenance is not None:
            if self.successor_memory_provenance.answer_id != self.finalized.answer_id:
                raise ValueError("successor provenance differs from finalized answer")
        return self


class LifeSwitchContextPreparationProviderV1(Protocol):
    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> LifeSwitchPreparedContextV1 | Awaitable[LifeSwitchPreparedContextV1]: ...


class PriorLifeSwitchProvenancePreparationProviderV1(Protocol):
    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> PriorLifeSwitchPreparedContextV1 | Awaitable[PriorLifeSwitchPreparedContextV1]: ...


async def _await_context(
    value: LifeSwitchPreparedContextV1 | Awaitable[LifeSwitchPreparedContextV1],
) -> LifeSwitchPreparedContextV1:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if not isinstance(value, LifeSwitchPreparedContextV1):
        raise TypeError("LifeSwitch provider returned the wrong type")
    return LifeSwitchPreparedContextV1.model_validate_json(value.model_dump_json())


async def _await_prior_context(
    value: PriorLifeSwitchPreparedContextV1 | Awaitable[PriorLifeSwitchPreparedContextV1],
) -> PriorLifeSwitchPreparedContextV1:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if not isinstance(value, PriorLifeSwitchPreparedContextV1):
        raise TypeError("prior LifeSwitch provider returned the wrong type")
    return PriorLifeSwitchPreparedContextV1.model_validate_json(value.model_dump_json())


def _elapsed_ms(start_ns: int) -> int:
    return max(0, round((time.monotonic_ns() - start_ns) / 1_000_000))


class LifeSwitchResponseStage:
    """Add LifeSwitch context and generate the LifeSwitch response."""

    def __init__(
        self,
        *,
        openai_client: Any,
        context_provider: LifeSwitchContextPreparationProviderV1,
        prior_provenance_provider: PriorLifeSwitchProvenancePreparationProviderV1,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        answer_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if openai_client is None:
            raise ValueError("an injected OpenAI client is required")
        self._openai_client = openai_client
        self._context_provider = context_provider
        self._prior_provenance_provider = prior_provenance_provider
        self._generation_config = generation_config or OpenAIChatGenerationConfigV1()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._answer_id_factory = answer_id_factory or uuid4

    async def execute(
        self,
        *,
        base_response_plan: TrustedResponsePlanV0_2,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> TrustedLifeSwitchResponseExecutionV2:
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

            stage = "prior_lifeswitch_provenance_selection"
            started_ns = time.monotonic_ns()
            prior_context = await _await_prior_context(
                self._prior_provenance_provider.prepare(
                    authenticated_actor_user_id=base.authenticated_actor_user_id,
                    conversation_snapshot=snapshot,
                )
            )
            timings["prior_lifeswitch_provenance_selection_ms"] = _elapsed_ms(started_ns)

            stage = "lifeswitch_prompt_augmentation"
            started_ns = time.monotonic_ns()
            plan = TrustedLifeSwitchResponsePlanV2.create(
                base_response_plan=base,
                lifeswitch_context=context,
                prior_lifeswitch_context=prior_context,
            )
            timings["prompt_augmentation_ms"] = _elapsed_ms(started_ns)

            stage = "answer_generation"
            started_ns = time.monotonic_ns()
            response = await OpenAIChatCompletionsAdapterV3(
                self._openai_client
            ).complete_async(
                plan,
                generation_config=self._generation_config,
            )
            timings["answer_generation_ms"] = _elapsed_ms(started_ns)

            stage = "finalization"
            started_ns = time.monotonic_ns()
            finalized = finalize_trusted_response_v3(
                trusted_plan=plan,
                provider_response=response,
                generation_config=self._generation_config,
                answer_id=self._answer_id_factory(),
                created_at=self._clock(),
            )
            timings["finalization_ms"] = _elapsed_ms(started_ns)
            return TrustedLifeSwitchResponseExecutionV2(
                trusted_plan=plan,
                provider_response=response,
                finalized=finalized,
                stage_timings=LifeSwitchResponseStageTimingsV2(
                    **timings,
                    pipeline_total_ms=_elapsed_ms(pipeline_started_ns),
                ),
            )
        except LifeSwitchCompositionError:
            raise
        except Exception:
            raise LifeSwitchCompositionError(stage) from None


class LifeSwitchConversationComposer:
    """Compose one conversation response with LifeSwitch domain context."""

    def __init__(
        self,
        *,
        base_composer: ConversationResponseComposer,
        openai_client: Any,
        context_provider: LifeSwitchContextPreparationProviderV1,
        prior_provenance_provider: PriorLifeSwitchProvenancePreparationProviderV1,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        answer_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._base_composer = base_composer
        self._generation_config = generation_config or OpenAIChatGenerationConfigV1()
        self._lifeswitch_stage = LifeSwitchResponseStage(
            openai_client=openai_client,
            context_provider=context_provider,
            prior_provenance_provider=prior_provenance_provider,
            generation_config=self._generation_config,
            clock=clock,
            answer_id_factory=answer_id_factory,
        )

    async def execute_detailed(
        self,
        conn: Any,
        command: AuthenticatedResponseCommandV0_2,
    ) -> IntegratedTrustedLifeSwitchResponseExecutionV2:
        pipeline_started_ns = time.monotonic_ns()
        prepared = await self._base_composer.prepare_detailed(conn, command)
        successor_memory_provenance = None
        try:
            downstream = await self._lifeswitch_stage.execute(
                base_response_plan=prepared.trusted_plan,
                conversation_snapshot=prepared.conversation_snapshot,
            )
            if self._base_composer.has_successor_memory_lifecycle:
                exact_request = OpenAIChatRequestV4.create(
                    source_plan=downstream.trusted_plan,
                    generation_config=self._generation_config,
                )
                if (
                    downstream.provider_response.provider_request_sha256
                    != exact_request.request_sha256
                ):
                    raise LifeSwitchCompositionError(
                        "successor_memory_answer_binding"
                    )
                successor_memory_provenance = (
                    await self._base_composer.persist_successor_memory_answer_binding(
                        answer_id=downstream.finalized.answer_id,
                        prompt_sha256=(
                            downstream.trusted_plan.assembled_prompt.manifest.assembly_sha256
                        ),
                        outbound_request_bytes=(
                            exact_request.provider_kwargs_json_bytes()
                        ),
                    )
                )
        except Exception:
            self._base_composer.discard_successor_memory_selection()
            raise
        base = prepared.stage_timings
        life = downstream.stage_timings
        return IntegratedTrustedLifeSwitchResponseExecutionV2(
            trusted_plan=downstream.trusted_plan,
            provider_response=downstream.provider_response,
            finalized=downstream.finalized,
            successor_memory_provenance=successor_memory_provenance,
            stage_timings=IntegratedLifeSwitchResponseStageTimingsV2(
                command_validation_ms=base.command_validation_ms,
                conversation_snapshot_ms=base.conversation_snapshot_ms,
                policy_input_ms=base.policy_input_ms,
                signal_classification_ms=base.signal_classification_ms,
                signal_binding_ms=base.signal_binding_ms,
                memory_selection_ms=base.memory_selection_ms,
                trusted_request_ms=base.trusted_request_ms,
                orchestration_ms=base.orchestration_ms,
                lifeswitch_context_selection_ms=life.context_selection_ms,
                prior_lifeswitch_provenance_selection_ms=(
                    life.prior_lifeswitch_provenance_selection_ms
                ),
                lifeswitch_prompt_augmentation_ms=life.prompt_augmentation_ms,
                answer_generation_ms=life.answer_generation_ms,
                finalization_ms=life.finalization_ms,
                pipeline_total_ms=_elapsed_ms(pipeline_started_ns),
            ),
        )


__all__ = [
    "LifeSwitchConversationComposer",
    "IntegratedLifeSwitchResponseStageTimingsV2",
    "IntegratedTrustedLifeSwitchResponseExecutionV2",
    "LifeSwitchResponseStage",
    "LifeSwitchCompositionError",
    "LifeSwitchContextPreparationProviderV1",
    "PriorLifeSwitchProvenancePreparationProviderV1",
    "LifeSwitchResponseStageTimingsV2",
    "TrustedLifeSwitchResponseExecutionV2",
]
