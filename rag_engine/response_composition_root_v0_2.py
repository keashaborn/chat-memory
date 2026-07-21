from __future__ import annotations

"""Inactive production composition root for RESSE response orchestration.

Nothing imports this module from the live request path.  It demonstrates and
tests the only intended authority flow: authenticated route values, an owner-
scoped database snapshot, backend safety and mode classifiers, independently
governed Memory, typed prompt assembly, provider execution, and final binding.
"""

import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.memory_prompt_renderer_v1 import MemoryPromptApplicationResultV1
from rag_engine.memory_v1_selection_envelope import MemoryPromptAssemblyInputV1
from rag_engine.openai_chat_provider_v1 import (
    OpenAIChatCompletionsAdapterV1,
    OpenAIChatGenerationConfigV1,
    OpenAIChatResponseV1,
    safety_identifier_v1,
)
from rag_engine.openai_moderation_adapter_v0_2 import OpenAIModerationAdapterV0_2
from rag_engine.response_conversation_snapshot_v1 import (
    ConversationSnapshotV1,
    create_current_only_conversation_snapshot_v1,
    load_response_conversation_snapshot_v1,
)
from rag_engine.response_finalization_v1 import (
    FinalizedTrustedResponseV1,
    finalize_trusted_response_v1,
)
from rag_engine.response_orchestration_v0_2 import (
    TrustedPolicySignalsEnvelopeV0_2,
    TrustedResponseOrchestratorV0_2,
    TrustedResponsePlanV0_2,
    TrustedResponseRequestV0_2,
)
from rag_engine.response_policy_v0_2 import (
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
)
from rag_engine.server_response_signal_classifier_v0_2 import (
    OpenAIServerResponseSignalClassifierV0_2,
)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")


class ResponseCompositionError(RuntimeError):
    pass


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

    @model_validator(mode="after")
    def bounded_message_bytes(self) -> "AuthenticatedResponseCommandV0_2":
        if len(self.current_message.encode("utf-8")) > 32_768:
            raise ValueError("current message exceeds the byte limit")
        return self


class GovernedMemoryAssemblyV1(_StrictFrozenModel):
    memory_input: MemoryPromptAssemblyInputV1 | None = Field(default=None, repr=False)
    memory_application: MemoryPromptApplicationResultV1 | None = Field(
        default=None,
        repr=False,
    )

    @model_validator(mode="after")
    def paired(self) -> "GovernedMemoryAssemblyV1":
        if (self.memory_input is None) != (self.memory_application is None):
            raise ValueError("Memory input and application must be paired")
        return self


class TrustedResponseExecutionV0_2(_StrictFrozenModel):
    """Private execution result used by trusted post-generation adapters."""

    trusted_plan: TrustedResponsePlanV0_2 = Field(repr=False)
    provider_response: OpenAIChatResponseV1 = Field(repr=False)
    finalized: FinalizedTrustedResponseV1 = Field(repr=False)

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
        return self


class GovernedMemoryAssemblyProviderV1(Protocol):
    """Independent Memory V1 intent, selection, render, and control boundary."""

    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1 | Awaitable[GovernedMemoryAssemblyV1]: ...


class NoGovernedMemoryAssemblyProviderV1:
    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        del authenticated_actor_user_id, conversation_snapshot, trusted_policy_signals
        return GovernedMemoryAssemblyV1()


async def _await_memory(
    value: GovernedMemoryAssemblyV1 | Awaitable[GovernedMemoryAssemblyV1],
) -> GovernedMemoryAssemblyV1:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if not isinstance(value, GovernedMemoryAssemblyV1):
        raise ResponseCompositionError(
            "Memory provider must return GovernedMemoryAssemblyV1"
        )
    try:
        return GovernedMemoryAssemblyV1.model_validate_json(value.model_dump_json())
    except Exception:
        raise ResponseCompositionError("Memory provider result is invalid") from None


class InactiveResponseCompositionRootV0_2:
    """Integration-ready root; intentionally absent from live route imports."""

    def __init__(
        self,
        *,
        openai_client: Any,
        classifier_model: str,
        memory_provider: GovernedMemoryAssemblyProviderV1 | None = None,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
        clock: Callable[[], datetime] | None = None,
        answer_id_factory: Callable[[], UUID] | None = None,
        correlation_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if openai_client is None:
            raise ResponseCompositionError("an injected OpenAI client is required")
        self._openai_client = openai_client
        self._classifier_model = classifier_model
        self._memory_provider = memory_provider or NoGovernedMemoryAssemblyProviderV1()
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

        try:
            if not isinstance(command, AuthenticatedResponseCommandV0_2):
                raise TypeError("authenticated command type mismatch")
            command = AuthenticatedResponseCommandV0_2.model_validate_json(
                command.model_dump_json()
            )
            if command.stateless:
                snapshot = create_current_only_conversation_snapshot_v1(
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    thread_id=command.thread_id,
                    current_request_id=command.request_id,
                    current_message=command.current_message,
                )
            else:
                snapshot = await load_response_conversation_snapshot_v1(
                    conn,
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    thread_id=command.thread_id,
                    current_request_id=command.request_id,
                    current_message=command.current_message,
                )
            policy_input = ResponsePolicyInputV0_2.create(
                request_id=snapshot.current_request_id,
                conversation=snapshot.messages,
                requested_assistant_profile_id=None,
                request_field_names=command.request_field_names,
            )
            classifier = OpenAIServerResponseSignalClassifierV0_2(
                self._openai_client,
                model=self._classifier_model,
                safety_identifier=safety_identifier_v1(
                    command.authenticated_actor_user_id
                ),
            )
            classification = classifier.classify(policy_input)
            signal_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
                conversation_snapshot=snapshot,
                signals=classification.signals,
            )
            memory = await _await_memory(
                self._memory_provider.prepare(
                    authenticated_actor_user_id=command.authenticated_actor_user_id,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=classification.signals,
                )
            )
            trusted_request = TrustedResponseRequestV0_2.create_from_snapshot(
                authenticated_actor_user_id=command.authenticated_actor_user_id,
                conversation_snapshot=snapshot,
                request_field_names=command.request_field_names,
                trusted_policy_signals_envelope=signal_envelope,
                memory_input=memory.memory_input,
                memory_application=memory.memory_application,
                fm_token_budget=command.fm_token_budget,
            )
            orchestrator = TrustedResponseOrchestratorV0_2(
                OpenAIModerationAdapterV0_2(self._openai_client),
                clock=self._clock,
                correlation_id_factory=self._correlation_id_factory,
            )
            plan = await orchestrator.build_plan(trusted_request)
            response = await OpenAIChatCompletionsAdapterV1(
                self._openai_client
            ).complete_async(
                plan,
                generation_config=self._generation_config,
            )
            finalized = finalize_trusted_response_v1(
                trusted_plan=plan,
                provider_response=response,
                generation_config=self._generation_config,
                answer_id=self._answer_id_factory(),
                created_at=self._clock(),
            )
            return TrustedResponseExecutionV0_2(
                trusted_plan=plan,
                provider_response=response,
                finalized=finalized,
            )
        except ResponseCompositionError:
            raise
        except Exception:
            raise ResponseCompositionError("inactive response composition failed") from None


__all__ = [
    "AuthenticatedResponseCommandV0_2",
    "GovernedMemoryAssemblyProviderV1",
    "GovernedMemoryAssemblyV1",
    "InactiveResponseCompositionRootV0_2",
    "NoGovernedMemoryAssemblyProviderV1",
    "ResponseCompositionError",
    "TrustedResponseExecutionV0_2",
]
