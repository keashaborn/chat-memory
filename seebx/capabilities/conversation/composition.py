from __future__ import annotations

"""Canonical conversation response planning, execution, and finalization."""

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.capabilities.conversation.memory_contracts import (
    MemoryAnswerProvenanceV1,
)
from seebx.adapters.openai_chat import (
    OpenAIChatCompletionsAdapterV1,
    OpenAIChatGenerationConfigV1,
    OpenAIChatRequestV1,
    OpenAIChatResponseV1,
    safety_identifier_v1,
)
from seebx.adapters.openai_moderation import OpenAIModerationAdapterV0_2
from seebx.capabilities.conversation.prior_web_provenance import (
    PriorWebProvenanceError,
    load_prior_web_provenance_v1,
)
from seebx.capabilities.conversation.prompt import PromptReferenceContextBlockV1
from seebx.adapters.conversation_snapshot import (
    load_conversation_snapshot_v1,
)
from seebx.capabilities.conversation.snapshot import (
    ConversationSnapshotV1,
    create_current_only_conversation_snapshot_v1,
)
from seebx.capabilities.conversation.finalization import (
    FinalizedTrustedResponseV1,
    finalize_trusted_response_v1,
)
from seebx.capabilities.conversation.orchestration import (
    TrustedPolicySignalsEnvelopeV0_2,
    TrustedResponseOrchestratorV0_2,
    TrustedResponsePlanV0_2,
    TrustedResponseRequestV0_2,
)
from seebx.capabilities.conversation.policy import (
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
)
from seebx.capabilities.conversation.source_awareness import MemorySourceStatusV1
from seebx.capabilities.conversation.signal_classifier import (
    OpenAIServerResponseSignalClassifierV0_2,
)
from seebx.contracts.search import SearchCapabilityManifestV1
from seebx.contracts.voice_language import (
    DEFAULT_VOICE_LANGUAGE,
    SUPPORTED_VOICE_LANGUAGE_IDS,
)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")


class ResponseCompositionError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "not_applicable") -> None:
        self.stage = stage
        super().__init__(message)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class AuthenticatedResponseCommandV0_2(_StrictFrozenModel):
    """Values established by authentication and the public chat route."""

    authenticated_actor_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    request_id: str = Field(min_length=1, max_length=160, repr=False)
    current_message: str = Field(min_length=1, max_length=32_768, repr=False)
    request_field_names: tuple[str, ...] = ()
    fm_token_budget: int | None = Field(default=None, ge=0, le=1600)
    stateless: bool = False
    search_capability_manifest: SearchCapabilityManifestV1 | None = Field(
        default=None,
        repr=False,
    )
    response_language: str = DEFAULT_VOICE_LANGUAGE
    attachment_context_block: PromptReferenceContextBlockV1 | None = Field(
        default=None,
        repr=False,
    )

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request id is invalid")
        return value

    @field_validator("request_field_names")
    @classmethod
    def valid_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("request field names must be sorted and unique")
        if any(not _FIELD_NAME_RE.fullmatch(item) for item in value):
            raise ValueError("request field name is invalid")
        return value

    @field_validator("response_language")
    @classmethod
    def valid_response_language(cls, value: str) -> str:
        if value not in SUPPORTED_VOICE_LANGUAGE_IDS:
            raise ValueError("response language is unsupported")
        return value

    @model_validator(mode="after")
    def bounded_message_bytes(self) -> "AuthenticatedResponseCommandV0_2":
        if len(self.current_message.encode("utf-8")) > 32_768:
            raise ValueError("current message exceeds the byte limit")
        if self.attachment_context_block is not None:
            if (
                self.attachment_context_block.request_id_sha256
                != hashlib.sha256(self.request_id.encode("utf-8")).hexdigest()
                or self.attachment_context_block.query_sha256
                != hashlib.sha256(self.current_message.encode("utf-8")).hexdigest()
            ):
                raise ValueError("attachment context differs from authenticated command")
        return self


class ZepMemoryAssemblyV1(_StrictFrozenModel):
    source_status: MemorySourceStatusV1 = MemorySourceStatusV1.NOT_APPLICABLE
    successor_memory_context_block: PromptReferenceContextBlockV1 | None = Field(
        default=None,
        repr=False,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def zep_context_only(self) -> "ZepMemoryAssemblyV1":
        if (
            self.source_status is MemorySourceStatusV1.SELECTED
        ) != (self.successor_memory_context_block is not None):
            raise ValueError("Memory source status differs from selected context")
        if self.successor_memory_context_block is not None and (
            self.successor_memory_context_block.block_id != "zep_memory_v1"
        ):
            raise ValueError("invalid successor Memory context")
        return self


class ResponseStageTimingsV1(_StrictFrozenModel):
    """Content-free wall timings for one governed response execution."""

    command_validation_ms: int = Field(ge=0)
    conversation_snapshot_ms: int = Field(ge=0)
    policy_input_ms: int = Field(ge=0)
    signal_classification_ms: int = Field(ge=0)
    signal_binding_ms: int = Field(ge=0)
    memory_selection_ms: int = Field(ge=0)
    trusted_request_ms: int = Field(ge=0)
    orchestration_ms: int = Field(ge=0)
    answer_generation_ms: int = Field(ge=0)
    finalization_ms: int = Field(ge=0)
    pipeline_total_ms: int = Field(ge=0)


class ResponsePreparationTimingsV1(_StrictFrozenModel):
    """Content-free timings through trusted-plan construction only."""

    command_validation_ms: int = Field(ge=0)
    conversation_snapshot_ms: int = Field(ge=0)
    policy_input_ms: int = Field(ge=0)
    signal_classification_ms: int = Field(ge=0)
    signal_binding_ms: int = Field(ge=0)
    memory_selection_ms: int = Field(ge=0)
    trusted_request_ms: int = Field(ge=0)
    orchestration_ms: int = Field(ge=0)
    preparation_total_ms: int = Field(ge=0)


class TrustedResponsePreparationV0_2(_StrictFrozenModel):
    """Private seam for adding independent context before provider execution."""

    trusted_plan: TrustedResponsePlanV0_2 = Field(repr=False)
    conversation_snapshot: ConversationSnapshotV1 = Field(repr=False)
    stage_timings: ResponsePreparationTimingsV1

    @model_validator(mode="after")
    def bound(self) -> "TrustedResponsePreparationV0_2":
        if (
            self.conversation_snapshot.authenticated_actor_user_id
            != self.trusted_plan.authenticated_actor_user_id
        ):
            raise ValueError("prepared snapshot actor differs from trusted plan")
        if self.conversation_snapshot.thread_id != self.trusted_plan.thread_id:
            raise ValueError("prepared snapshot thread differs from trusted plan")
        if (
            self.conversation_snapshot.current_request_id
            != self.trusted_plan.policy_input.request_id
        ):
            raise ValueError("prepared snapshot request differs from trusted plan")
        if (
            self.conversation_snapshot.snapshot_sha256
            != self.trusted_plan.conversation_snapshot_sha256
        ):
            raise ValueError("prepared snapshot hash differs from trusted plan")
        return self


class TrustedResponseExecutionV0_2(_StrictFrozenModel):
    """Private execution result used by trusted post-generation adapters."""

    trusted_plan: TrustedResponsePlanV0_2 = Field(repr=False)
    provider_response: OpenAIChatResponseV1 = Field(repr=False)
    finalized: FinalizedTrustedResponseV1 = Field(repr=False)
    successor_memory_provenance: MemoryAnswerProvenanceV1 | None = Field(
        default=None,
        repr=False,
        exclude_if=lambda value: value is None,
    )
    stage_timings: ResponseStageTimingsV1

    @model_validator(mode="after")
    def bound(self) -> "TrustedResponseExecutionV0_2":
        attestation = self.finalized.attestation
        if attestation.trusted_plan_sha256 != self.trusted_plan.plan_sha256:
            raise ValueError("execution finalization differs from its trusted plan")
        if (
            attestation.provider_response_sha256
            != self.provider_response.response_sha256
        ):
            raise ValueError("execution finalization differs from its provider response")
        if self.successor_memory_provenance is not None:
            if self.successor_memory_provenance.answer_id != self.finalized.answer_id:
                raise ValueError("successor provenance differs from finalized answer")
        return self


def _elapsed_ms(start_ns: int) -> int:
    return max(0, round((time.monotonic_ns() - start_ns) / 1_000_000))


class ZepMemoryAssemblyProviderV1(Protocol):
    """Independent Zep Memory context boundary."""

    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> ZepMemoryAssemblyV1 | Awaitable[ZepMemoryAssemblyV1]: ...


class ZepMemoryAnswerLifecycleV1(Protocol):
    """Post-generation binding hook for the exact selected Zep claims."""

    @property
    def has_selected_claims(self) -> bool: ...

    def discard_selected_state(self) -> None: ...

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> MemoryAnswerProvenanceV1: ...


class NoZepMemoryAssemblyProviderV1:
    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> ZepMemoryAssemblyV1:
        del authenticated_actor_user_id, conversation_snapshot, trusted_policy_signals
        return ZepMemoryAssemblyV1()


async def _await_memory(
    value: ZepMemoryAssemblyV1 | Awaitable[ZepMemoryAssemblyV1],
) -> ZepMemoryAssemblyV1:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if not isinstance(value, ZepMemoryAssemblyV1):
        raise ResponseCompositionError(
            "Memory provider must return ZepMemoryAssemblyV1"
        )
    try:
        return ZepMemoryAssemblyV1.model_validate_json(value.model_dump_json())
    except Exception:
        raise ResponseCompositionError("Memory provider result is invalid") from None


class ConversationResponseComposer:
    """Compose one authenticated conversation response."""

    def __init__(
        self,
        *,
        openai_client: Any,
        classifier_model: str,
        memory_provider: ZepMemoryAssemblyProviderV1 | None = None,
        zep_memory_lifecycle: ZepMemoryAnswerLifecycleV1 | None = None,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        answer_id_factory: Callable[[], UUID] | None = None,
        correlation_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if openai_client is None:
            raise ResponseCompositionError("an injected OpenAI client is required")
        self._openai_client = openai_client
        self._classifier_model = classifier_model
        self._memory_provider = memory_provider or NoZepMemoryAssemblyProviderV1()
        if (
            zep_memory_lifecycle is not None
            and zep_memory_lifecycle is not self._memory_provider
        ):
            raise ResponseCompositionError(
                "Zep Memory provider and lifecycle must be identical"
            )
        self._zep_memory_lifecycle = zep_memory_lifecycle
        self._generation_config = generation_config or OpenAIChatGenerationConfigV1()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._answer_id_factory = answer_id_factory or uuid4
        self._correlation_id_factory = correlation_id_factory or uuid4

    async def execute(
        self,
        conn: Any,
        command: AuthenticatedResponseCommandV0_2,
    ) -> FinalizedTrustedResponseV1:
        """Execute the typed path while preserving the existing public result."""

        return (await self.execute_detailed(conn, command)).finalized

    async def execute_detailed(
        self,
        conn: Any,
        command: AuthenticatedResponseCommandV0_2,
    ) -> TrustedResponseExecutionV0_2:
        """Execute and retain private typed artifacts for trusted adapters."""

        pipeline_started_ns = time.monotonic_ns()
        stage = "trusted_plan_preparation"
        try:
            prepared = await self.prepare_detailed(conn, command)
            plan = prepared.trusted_plan
            stage_timings = prepared.stage_timings.model_dump(
                mode="python",
                exclude={"preparation_total_ms"},
            )
            stage = "answer_generation"
            stage_started_ns = time.monotonic_ns()
            answer_id = self._answer_id_factory()
            exact_request = OpenAIChatRequestV1.create(
                trusted_plan=plan,
                generation_config=self._generation_config,
            )
            response = await OpenAIChatCompletionsAdapterV1(
                self._openai_client
            ).complete_async(
                plan,
                generation_config=self._generation_config,
            )
            if response.provider_request_sha256 != exact_request.request_sha256:
                raise ResponseCompositionError(
                    "provider response differs from prepared request",
                    stage="answer_generation",
                )
            stage_timings["answer_generation_ms"] = _elapsed_ms(stage_started_ns)
            zep_memory_provenance = None
            if self.has_zep_memory_lifecycle:
                stage = "successor_memory_answer_binding"
                zep_memory_provenance = (
                    await self.persist_zep_memory_answer_binding(
                        answer_id=answer_id,
                        prompt_sha256=(
                            plan.assembled_prompt.manifest.assembly_sha256
                        ),
                        outbound_request_bytes=(
                            exact_request.provider_kwargs_json_bytes()
                        ),
                    )
                )
            stage = "finalization"
            stage_started_ns = time.monotonic_ns()
            finalized = finalize_trusted_response_v1(
                trusted_plan=plan,
                provider_response=response,
                generation_config=self._generation_config,
                answer_id=answer_id,
                created_at=self._clock(),
            )
            stage_timings["finalization_ms"] = _elapsed_ms(stage_started_ns)
            stage = "execution_binding"
            return TrustedResponseExecutionV0_2(
                trusted_plan=plan,
                provider_response=response,
                finalized=finalized,
                successor_memory_provenance=zep_memory_provenance,
                stage_timings=ResponseStageTimingsV1(
                    **stage_timings,
                    pipeline_total_ms=_elapsed_ms(pipeline_started_ns),
                ),
            )
        except ResponseCompositionError as exc:
            self.discard_zep_memory_selection()
            if exc.stage == "not_applicable":
                raise ResponseCompositionError(str(exc), stage=stage) from None
            raise
        except Exception:
            self.discard_zep_memory_selection()
            raise ResponseCompositionError(
                "conversation response composition failed",
                stage=stage,
            ) from None

    @property
    def has_selected_zep_memory(self) -> bool:
        return bool(
            self._zep_memory_lifecycle is not None
            and self._zep_memory_lifecycle.has_selected_claims
        )

    @property
    def has_zep_memory_lifecycle(self) -> bool:
        return self._zep_memory_lifecycle is not None

    async def persist_zep_memory_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> MemoryAnswerProvenanceV1:
        lifecycle = self._zep_memory_lifecycle
        if lifecycle is None:
            raise ResponseCompositionError(
                "Zep Memory lifecycle is unavailable"
            )
        provenance = await lifecycle.persist_dispatched_answer_binding(
            answer_id=answer_id,
            prompt_sha256=prompt_sha256,
            outbound_request_bytes=outbound_request_bytes,
        )
        if not isinstance(provenance, BaseModel):
            raise ResponseCompositionError(
                "Zep Memory lifecycle returned invalid provenance"
            )
        try:
            return MemoryAnswerProvenanceV1.model_validate_json(
                provenance.model_dump_json()
            )
        except Exception:
            raise ResponseCompositionError(
                "Zep Memory lifecycle returned invalid provenance"
            ) from None

    def discard_zep_memory_selection(self) -> None:
        lifecycle = self._zep_memory_lifecycle
        if lifecycle is not None:
            lifecycle.discard_selected_state()

    async def prepare_detailed(
        self,
        conn: Any,
        command: AuthenticatedResponseCommandV0_2,
    ) -> TrustedResponsePreparationV0_2:
        """Construct the exact trusted plan without calling the answer model."""

        stage = "command_validation"
        preparation_started_ns = time.monotonic_ns()
        stage_started_ns = preparation_started_ns
        stage_timings: dict[str, int] = {}
        try:
            if not isinstance(command, AuthenticatedResponseCommandV0_2):
                raise TypeError("authenticated command type mismatch")
            command = AuthenticatedResponseCommandV0_2.model_validate_json(
                command.model_dump_json()
            )
            stage_timings["command_validation_ms"] = _elapsed_ms(stage_started_ns)
            stage = "conversation_snapshot"
            stage_started_ns = time.monotonic_ns()
            if command.stateless:
                snapshot = create_current_only_conversation_snapshot_v1(
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    thread_id=command.thread_id,
                    current_request_id=command.request_id,
                    current_message=command.current_message,
                )
            else:
                snapshot = await load_conversation_snapshot_v1(
                    conn,
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    thread_id=command.thread_id,
                    current_request_id=command.request_id,
                    current_message=command.current_message,
                )
            prior_web_provenance = None
            if not command.stateless:
                try:
                    prior_web_provenance = await load_prior_web_provenance_v1(
                        conn,
                        authenticated_actor_user_id=(
                            command.authenticated_actor_user_id
                        ),
                        conversation_snapshot=snapshot,
                    )
                except PriorWebProvenanceError:
                    # Provenance is optional lower-authority context. Failure
                    # cannot weaken transcript ownership or block normal chat.
                    prior_web_provenance = None
            stage_timings["conversation_snapshot_ms"] = _elapsed_ms(stage_started_ns)
            stage = "policy_input"
            stage_started_ns = time.monotonic_ns()
            policy_input = ResponsePolicyInputV0_2.create(
                request_id=snapshot.current_request_id,
                conversation=snapshot.messages,
                requested_assistant_profile_id=None,
                request_field_names=command.request_field_names,
            )
            stage_timings["policy_input_ms"] = _elapsed_ms(stage_started_ns)
            stage = "signal_classification"
            stage_started_ns = time.monotonic_ns()
            classifier = OpenAIServerResponseSignalClassifierV0_2(
                self._openai_client,
                model=self._classifier_model,
                safety_identifier=safety_identifier_v1(
                    command.authenticated_actor_user_id
                ),
            )
            classification = classifier.classify(policy_input)
            stage_timings["signal_classification_ms"] = _elapsed_ms(stage_started_ns)
            stage = "signal_binding"
            stage_started_ns = time.monotonic_ns()
            signal_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
                conversation_snapshot=snapshot,
                signals=classification.signals,
            )
            stage_timings["signal_binding_ms"] = _elapsed_ms(stage_started_ns)
            stage = "memory_selection"
            stage_started_ns = time.monotonic_ns()
            memory = await _await_memory(
                self._memory_provider.prepare(
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=classification.signals,
                )
            )
            stage_timings["memory_selection_ms"] = _elapsed_ms(stage_started_ns)
            stage = "trusted_request"
            stage_started_ns = time.monotonic_ns()
            trusted_request = TrustedResponseRequestV0_2.create_from_snapshot(
                authenticated_actor_user_id=command.authenticated_actor_user_id,
                conversation_snapshot=snapshot,
                request_field_names=command.request_field_names,
                trusted_policy_signals_envelope=signal_envelope,
                successor_memory_context_block=(
                    memory.successor_memory_context_block
                ),
                memory_source_status=memory.source_status,
                prior_web_provenance=prior_web_provenance,
                attachment_context_block=command.attachment_context_block,
                fm_token_budget=command.fm_token_budget,
                search_capability_manifest=command.search_capability_manifest,
                response_language=command.response_language,
            )
            stage_timings["trusted_request_ms"] = _elapsed_ms(stage_started_ns)
            stage = "orchestration"
            stage_started_ns = time.monotonic_ns()
            orchestrator = TrustedResponseOrchestratorV0_2(
                OpenAIModerationAdapterV0_2(self._openai_client),
                clock=self._clock,
                correlation_id_factory=self._correlation_id_factory,
            )
            plan = await orchestrator.build_plan(trusted_request)
            stage_timings["orchestration_ms"] = _elapsed_ms(stage_started_ns)
            return TrustedResponsePreparationV0_2(
                trusted_plan=plan,
                conversation_snapshot=snapshot,
                stage_timings=ResponsePreparationTimingsV1(
                    **stage_timings,
                    preparation_total_ms=_elapsed_ms(preparation_started_ns),
                ),
            )
        except ResponseCompositionError as exc:
            if exc.stage == "not_applicable":
                raise ResponseCompositionError(str(exc), stage=stage) from None
            raise
        except Exception:
            raise ResponseCompositionError(
                "conversation response composition failed",
                stage=stage,
            ) from None


__all__ = [
    "AuthenticatedResponseCommandV0_2",
    "ZepMemoryAssemblyProviderV1",
    "ZepMemoryAssemblyV1",
    "ConversationResponseComposer",
    "NoZepMemoryAssemblyProviderV1",
    "ResponseCompositionError",
    "ResponsePreparationTimingsV1",
    "ResponseStageTimingsV1",
    "ZepMemoryAnswerLifecycleV1",
    "TrustedResponseExecutionV0_2",
    "TrustedResponsePreparationV0_2",
]
