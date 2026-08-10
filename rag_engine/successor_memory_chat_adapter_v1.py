from __future__ import annotations

"""Host-chat bridge for the closed governed-Memory successor contracts."""

import hashlib
import inspect
import json
from typing import Any, Awaitable, Protocol
from uuid import UUID

from pydantic import BaseModel

from rag_engine.governed_memory.contracts import ContractViolation, require_uuid
from rag_engine.governed_memory.response_contracts import (
    SuccessorMemoryAssemblyV1,
    SuccessorResponseRequestV1,
)
from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryAnswerProvenanceV1,
)
from rag_engine.prompt_assembler_v1 import PromptReferenceContextBlockV1
from rag_engine.response_composition_root_v0_2 import GovernedMemoryAssemblyV1
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2


def _canonical_json_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _wire_revalidate(
    model_type: type[BaseModel],
    value: object,
    error_code: str,
) -> Any:
    if not isinstance(value, model_type):
        raise ContractViolation(error_code)
    try:
        return model_type.model_validate_json(_canonical_json_bytes(value))
    except Exception:
        raise ContractViolation(error_code) from None


class SuccessorMemoryCoreProviderV1(Protocol):
    def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1 | Awaitable[SuccessorMemoryAssemblyV1]: ...

    @property
    def has_selected_claims(self) -> bool: ...

    def discard_selected_state(self) -> None: ...

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> SuccessorMemoryAnswerProvenanceV1: ...


class SuccessorMemoryChatAdapterV1:
    """Translate validated host-chat models into closed successor DTOs."""

    def __init__(self, core_provider: SuccessorMemoryCoreProviderV1) -> None:
        if core_provider is None:
            raise ContractViolation("response_memory_adapter_required")
        self._core_provider = core_provider

    @property
    def has_selected_claims(self) -> bool:
        return self._core_provider.has_selected_claims

    def discard_selected_state(self) -> None:
        self._core_provider.discard_selected_state()

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        actor = require_uuid(
            authenticated_actor_user_id,
            "invalid_response_memory_authenticated_actor",
        )
        snapshot = _wire_revalidate(
            ConversationSnapshotV1,
            conversation_snapshot,
            "invalid_response_memory_snapshot",
        )
        policy_signals = _wire_revalidate(
            ResponsePolicySignalsV0_2,
            trusted_policy_signals,
            "invalid_response_memory_policy_signals",
        )
        if snapshot.authenticated_actor_user_id != actor:
            raise ContractViolation("response_memory_request_binding_mismatch")

        request = SuccessorResponseRequestV1(
            authenticated_actor_user_id=actor,
            thread_id=snapshot.thread_id,
            request_id=snapshot.current_request_id,
            current_message=snapshot.messages[-1].content,
            conversation_snapshot_sha256=snapshot.snapshot_sha256,
            trusted_policy_signals_sha256=hashlib.sha256(
                _canonical_json_bytes(policy_signals)
            ).hexdigest(),
        )
        prepared = self._core_provider.prepare(request=request)
        if inspect.isawaitable(prepared):
            prepared = await prepared
        assembly = _wire_revalidate(
            SuccessorMemoryAssemblyV1,
            prepared,
            "invalid_response_memory_assembly",
        )
        local_block = assembly.successor_memory_context_block
        host_block = None
        if local_block is not None:
            try:
                host_block = PromptReferenceContextBlockV1.model_validate_json(
                    _canonical_json_bytes(local_block)
                )
            except Exception:
                raise ContractViolation("invalid_response_memory_context_block") from None
        return GovernedMemoryAssemblyV1(
            successor_memory_context_block=host_block,
        )

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> SuccessorMemoryAnswerProvenanceV1:
        return await self._core_provider.persist_dispatched_answer_binding(
            answer_id=answer_id,
            prompt_sha256=prompt_sha256,
            outbound_request_bytes=outbound_request_bytes,
        )


__all__ = [
    "SuccessorMemoryChatAdapterV1",
    "SuccessorMemoryCoreProviderV1",
]
