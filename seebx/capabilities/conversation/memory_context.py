from __future__ import annotations

"""No-resource context provider for response surfaces that exclude memory."""

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from rag_engine.response_composition_root_v0_2 import GovernedMemoryAssemblyV1
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from rag_engine.response_source_awareness_v1 import MemorySourceStatusV1
from .memory_contracts import (
    MemoryAnswerProvenanceV1,
    MemoryNotApplicableReason,
    build_memory_not_applicable_provenance_v1,
)


class MemoryContextError(RuntimeError):
    """The response-memory context boundary was used inconsistently."""


def _canonical_model_bytes(value: BaseModel) -> bytes:
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
        raise MemoryContextError(error_code)
    try:
        return model_type.model_validate_json(_canonical_model_bytes(value))
    except Exception:
        raise MemoryContextError(error_code) from None


class InactiveMemoryContextProviderV1:
    """Produce an exact not-applicable receipt without touching a provider."""

    def __init__(self, reason: MemoryNotApplicableReason) -> None:
        if not isinstance(reason, MemoryNotApplicableReason):
            raise MemoryContextError("invalid_memory_not_applicable_reason")
        self._reason = reason
        self._prepared = False
        self._terminal = False

    @property
    def has_selected_claims(self) -> bool:
        return False

    @property
    def not_applicable_reason(self) -> MemoryNotApplicableReason:
        return self._reason

    def discard_selected_state(self) -> None:
        if self._prepared:
            self._terminal = True

    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        if not isinstance(authenticated_actor_user_id, UUID):
            raise MemoryContextError("invalid_memory_authenticated_actor")
        snapshot = _wire_revalidate(
            ConversationSnapshotV1,
            conversation_snapshot,
            "invalid_memory_snapshot",
        )
        _wire_revalidate(
            ResponsePolicySignalsV0_2,
            trusted_policy_signals,
            "invalid_memory_policy_signals",
        )
        if snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
            raise MemoryContextError("memory_request_binding_mismatch")
        if self._prepared:
            raise MemoryContextError("memory_context_provider_reused")
        self._prepared = True
        return GovernedMemoryAssemblyV1(
            source_status=MemorySourceStatusV1.NOT_APPLICABLE
        )

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> MemoryAnswerProvenanceV1:
        if not self._prepared:
            raise MemoryContextError("memory_selection_missing")
        if self._terminal:
            raise MemoryContextError("memory_binding_terminal")
        self._terminal = True
        return build_memory_not_applicable_provenance_v1(
            reason=self._reason,
            answer_id=answer_id,
            prompt_sha256=prompt_sha256,
            outbound_request_bytes=outbound_request_bytes,
        )


__all__ = ["InactiveMemoryContextProviderV1", "MemoryContextError"]
