from __future__ import annotations

"""Inactive real-chat adapter for the clean governed-Memory successor.

The adapter is deliberately composed from injected PostgreSQL, Qdrant, and
embedding boundaries.  Importing it and selecting the default ``off`` mode do
not open resources.  Qdrant supplies only bounded candidate identifiers;
PostgreSQL rows are revalidated before any prompt content is constructed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from typing import Awaitable, Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
    require_sha256,
    require_utc,
    require_uuid,
)
from rag_engine.governed_memory.exclusive_cutover import (
    EXCLUSIVE_MODE_ENV,
    ExclusiveMemoryConfigurationError,
    ExclusiveMemoryMode,
    exclusive_memory_mode,
)
from rag_engine.governed_memory.retrieval import (
    ANSWER_RENDERER_SHA256,
    RetrievalPolicy,
    build_answer_binding,
    mark_answer_binding_dispatched,
    render_memory_context,
    revalidate_candidates,
)
from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryAnswerProvenanceV1,
    SuccessorMemoryNotApplicableReason,
    build_successor_exposed_provenance_v1,
    build_successor_no_memory_selected_provenance_v1,
    build_successor_not_applicable_provenance_v1,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision
from rag_engine.prompt_assembler_v1 import (
    ContextKind,
    PromptReferenceContextBlockV1,
    PromptReferenceFragmentV1,
)
from rag_engine.response_composition_root_v0_2 import GovernedMemoryAssemblyV1
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2


EXCLUSIVE_MODE_LEGACY = ExclusiveMemoryMode.LEGACY.value
EXCLUSIVE_MODE_SUCCESSOR = ExclusiveMemoryMode.SUCCESSOR_PILOT.value
SUCCESSOR_CONTEXT_CONTRACT = "governed-memory-answer-context-v1"


class SuccessorResponseConfigurationError(RuntimeError):
    """A successor response lane was requested without exact runtime wiring."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SuccessorResponseActorBinding:
    owner_user_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str
    request_id: str
    thread_id: UUID
    eligible: bool

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_response_memory_owner")
        require_uuid(self.session_id, "invalid_response_memory_session")
        require_sha256(
            self.authentication_manifest_sha256,
            "invalid_response_memory_authentication_manifest",
        )
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or len(self.request_id.encode("utf-8")) > 200
        ):
            raise ContractViolation("invalid_response_memory_request")
        require_uuid(self.thread_id, "invalid_response_memory_thread")
        if type(self.eligible) is not bool:
            raise ContractViolation("invalid_response_memory_eligibility")


class SuccessorResponseRepository(Protocol):
    async def read_claims(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        claim_ids: Sequence[UUID],
    ) -> tuple[Mapping[str, object], ...]: ...

    async def persist_answer_binding(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        operation_id: UUID,
        thread_id: UUID,
        binding: Mapping[str, object],
        memory_block: str,
        outbound_request: str,
    ) -> None: ...


class SuccessorResponseVectorIndex(Protocol):
    async def search_owner_candidates(
        self,
        *,
        owner_user_id: UUID,
        query_vector: Sequence[float | int],
        allowed_predicates: Sequence[str],
        limit: int,
        calibration: CalibrationDecision,
    ) -> tuple[dict[str, object], ...]: ...


class SuccessorResponseEmbedder(Protocol):
    def embed(self, text: str) -> Sequence[float] | Awaitable[Sequence[float]]: ...


class InactiveSuccessorMemoryProviderV1:
    """No-resource successor provider for explicitly excluded chat surfaces."""

    def __init__(self, reason: SuccessorMemoryNotApplicableReason) -> None:
        if not isinstance(reason, SuccessorMemoryNotApplicableReason):
            raise ContractViolation("invalid_successor_not_applicable_reason")
        self._reason = reason
        self._prepared = False
        self._terminal = False

    @property
    def has_selected_claims(self) -> bool:
        return False

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
        del authenticated_actor_user_id, conversation_snapshot, trusted_policy_signals
        if self._prepared:
            raise ContractViolation("response_memory_provider_reused")
        self._prepared = True
        return GovernedMemoryAssemblyV1()

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> SuccessorMemoryAnswerProvenanceV1:
        if not self._prepared:
            raise ContractViolation("response_memory_selection_missing")
        if self._terminal:
            raise ContractViolation("response_memory_binding_terminal")
        self._terminal = True
        return build_successor_not_applicable_provenance_v1(
            reason=self._reason,
            answer_id=answer_id,
            prompt_sha256=prompt_sha256,
            outbound_request_bytes=outbound_request_bytes,
        )


def response_mode_from_environment(environment: Mapping[str, str]) -> str:
    try:
        return exclusive_memory_mode(environment).value
    except ExclusiveMemoryConfigurationError as exc:
        raise SuccessorResponseConfigurationError(
            "response_memory_mode_invalid"
        ) from exc


def choose_response_memory_provider(
    *,
    mode: str,
    legacy_factory: Callable[[], object],
    successor_factory: Callable[[], object],
) -> tuple[object, object | None]:
    """Construct exactly one provider; successor failures never fall back."""

    if mode == EXCLUSIVE_MODE_LEGACY:
        return legacy_factory(), None
    if mode == EXCLUSIVE_MODE_SUCCESSOR:
        successor = successor_factory()
        return successor, successor
    raise SuccessorResponseConfigurationError("response_memory_mode_invalid")


async def _await_embedding(
    value: Sequence[float] | Awaitable[Sequence[float]],
) -> tuple[float, ...]:
    if hasattr(value, "__await__"):
        value = await value  # type: ignore[misc]
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, (list, tuple)
    ):
        raise ContractViolation("invalid_response_memory_embedding")
    result = tuple(float(item) for item in value)
    if not result or any(not math.isfinite(item) for item in result):
        raise ContractViolation("invalid_response_memory_embedding")
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _default_policy(predicate_catalog: Mapping[str, object]) -> RetrievalPolicy:
    predicates = predicate_catalog.get("predicates")
    if not isinstance(predicates, Mapping):
        raise ContractViolation("invalid_response_memory_predicate_catalog")
    allowed = tuple(sorted(str(item) for item in predicates))
    return RetrievalPolicy(
        explicit_recall=False,
        allowed_predicates=allowed,
        max_records=8,
    )


class SuccessorGovernedMemoryAssemblyProviderV1:
    """One-request successor selection, prompt, and answer-binding lifecycle."""

    def __init__(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        repository: SuccessorResponseRepository,
        vector_index: SuccessorResponseVectorIndex,
        embedder: SuccessorResponseEmbedder,
        predicate_catalog: Mapping[str, object],
        calibration: CalibrationDecision,
        policy_factory: Callable[[Mapping[str, object]], RetrievalPolicy] = (
            _default_policy
        ),
        clock: Callable[[], datetime] | None = None,
        operation_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if not isinstance(actor, SuccessorResponseActorBinding):
            raise ContractViolation("invalid_response_memory_actor")
        if repository is None or vector_index is None or embedder is None:
            raise ContractViolation("response_memory_adapter_required")
        if not isinstance(predicate_catalog, Mapping):
            raise ContractViolation("invalid_response_memory_predicate_catalog")
        if not isinstance(calibration, CalibrationDecision):
            raise ContractViolation("invalid_retrieval_calibration")
        self._actor = actor
        self._repository = repository
        self._vector_index = vector_index
        self._embedder = embedder
        self._predicate_catalog = dict(predicate_catalog)
        self._calibration = calibration
        self._policy_factory = policy_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._operation_id = (operation_id_factory or uuid4)()
        require_uuid(self._operation_id, "invalid_response_memory_operation")
        self._prepared = False
        self._terminal = False
        self._no_selection_pending = False
        self._policy: RetrievalPolicy | None = None
        self._selected_claims: tuple[dict[str, object], ...] = ()
        self._rendered_context: dict[str, object] | None = None
        self._query_sha256: str | None = None

    @property
    def has_selected_claims(self) -> bool:
        return bool(self._selected_claims) and not self._terminal

    def _finish_without_selection(self) -> GovernedMemoryAssemblyV1:
        self._no_selection_pending = True
        self._policy = None
        self._selected_claims = ()
        self._rendered_context = None
        self._query_sha256 = None
        return GovernedMemoryAssemblyV1()

    def _clear_selected_state(self) -> None:
        self._terminal = True
        self._no_selection_pending = False
        self._policy = None
        self._selected_claims = ()
        self._rendered_context = None
        self._query_sha256 = None

    def discard_selected_state(self) -> None:
        if self._prepared and not self._terminal:
            self._clear_selected_state()

    def _verified_snapshot(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> ConversationSnapshotV1:
        if not isinstance(trusted_policy_signals, ResponsePolicySignalsV0_2):
            raise ContractViolation("invalid_response_memory_policy_signals")
        ResponsePolicySignalsV0_2.model_validate_json(
            trusted_policy_signals.model_dump_json()
        )
        if not isinstance(conversation_snapshot, ConversationSnapshotV1):
            raise ContractViolation("invalid_response_memory_snapshot")
        snapshot = ConversationSnapshotV1.model_validate_json(
            conversation_snapshot.model_dump_json()
        )
        actor = require_uuid(
            authenticated_actor_user_id,
            "invalid_response_memory_authenticated_actor",
        )
        if (
            actor != self._actor.owner_user_id
            or snapshot.authenticated_actor_user_id != self._actor.owner_user_id
            or snapshot.current_request_id != self._actor.request_id
            or snapshot.thread_id != self._actor.thread_id
            or not snapshot.messages
            or snapshot.messages[-1].role.value != "user"
        ):
            raise ContractViolation("response_memory_request_binding_mismatch")
        return snapshot

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        try:
            return await self._prepare_once(
                authenticated_actor_user_id=authenticated_actor_user_id,
                conversation_snapshot=conversation_snapshot,
                trusted_policy_signals=trusted_policy_signals,
            )
        except Exception:
            self._clear_selected_state()
            raise

    async def _prepare_once(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        if self._prepared:
            raise ContractViolation("response_memory_provider_reused")
        self._prepared = True
        snapshot = self._verified_snapshot(
            authenticated_actor_user_id=authenticated_actor_user_id,
            conversation_snapshot=conversation_snapshot,
            trusted_policy_signals=trusted_policy_signals,
        )
        query = snapshot.messages[-1].content
        self._query_sha256 = _sha256_text(query)
        if not self._actor.eligible:
            raise ContractViolation("ineligible_successor_response_provider")
        if not self._calibration.retrieval_enabled:
            raise ContractViolation("successor_response_calibration_disabled")

        policy = self._policy_factory(self._predicate_catalog)
        if not isinstance(policy, RetrievalPolicy):
            raise ContractViolation("invalid_response_memory_retrieval_policy")
        self._policy = policy
        query_vector = await _await_embedding(self._embedder.embed(query))
        candidates = await self._vector_index.search_owner_candidates(
            owner_user_id=self._actor.owner_user_id,
            query_vector=query_vector,
            allowed_predicates=policy.allowed_predicates,
            limit=policy.max_records,
            calibration=self._calibration,
        )
        if not candidates:
            return self._finish_without_selection()
        candidate_ids = tuple(UUID(str(item["claim_id"])) for item in candidates)
        rows = await self._repository.read_claims(
            actor=self._actor,
            claim_ids=candidate_ids,
        )
        authorization_at = require_utc(
            self._clock(),
            "invalid_response_memory_authorization_time",
        )
        selected = revalidate_candidates(
            self._actor.owner_user_id,
            candidates,
            rows,
            policy=policy,
            predicate_catalog=self._predicate_catalog,
            authorization_at=authorization_at,
        )
        if not selected:
            return self._finish_without_selection()
        rendered = render_memory_context(
            self._actor.owner_user_id,
            selected,
            max_records=policy.max_records,
            max_bytes=32_768,
        )
        content = rendered["content"]
        if not isinstance(content, str) or not content:
            raise ContractViolation("invalid_response_memory_render")
        raw = content.encode("utf-8")
        content_sha256 = _sha256_text(content)
        if rendered["memory_block_sha256"] != content_sha256:
            raise ContractViolation("response_memory_render_hash_mismatch")
        block = PromptReferenceContextBlockV1(
            block_id="governed_memory_successor_v1",
            kind=ContextKind.MEMORY,
            source_contract_version=SUCCESSOR_CONTEXT_CONTRACT,
            source_manifest_sha256=content_sha256,
            request_id_sha256=_sha256_text(self._actor.request_id),
            query_sha256=self._query_sha256,
            content=content,
            content_sha256=content_sha256,
            content_bytes=len(raw),
            estimated_tokens=math.ceil(len(raw) / 4),
            fragments=(
                PromptReferenceFragmentV1(
                    ordinal=0,
                    byte_offset=0,
                    byte_length=len(raw),
                    content_sha256=content_sha256,
                    estimated_tokens=math.ceil(len(raw) / 4),
                ),
            ),
        )
        self._selected_claims = tuple(dict(item) for item in selected)
        self._rendered_context = dict(rendered)
        return GovernedMemoryAssemblyV1(
            successor_memory_context_block=block,
        )

    async def persist_dispatched_answer_binding(
        self,
        *,
        answer_id: UUID,
        prompt_sha256: str,
        outbound_request_bytes: bytes,
    ) -> SuccessorMemoryAnswerProvenanceV1:
        if not self._prepared:
            raise ContractViolation("response_memory_selection_missing")
        if self._terminal:
            raise ContractViolation("response_memory_binding_terminal")
        if self._no_selection_pending:
            try:
                return build_successor_no_memory_selected_provenance_v1(
                    answer_id=answer_id,
                    prompt_sha256=prompt_sha256,
                    outbound_request_bytes=outbound_request_bytes,
                )
            finally:
                self._clear_selected_state()
        if not self._selected_claims:
            raise ContractViolation("response_memory_selection_missing")
        if self._policy is None or self._rendered_context is None:
            raise ContractViolation("response_memory_selection_incomplete")
        answer = require_uuid(answer_id, "invalid_response_memory_answer")
        require_sha256(prompt_sha256, "invalid_response_memory_prompt_sha256")
        if not isinstance(outbound_request_bytes, bytes):
            raise ContractViolation("invalid_outbound_request_bytes")
        try:
            outbound_request = outbound_request_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractViolation("invalid_outbound_request_utf8") from exc
        assert self._query_sha256 is not None
        try:
            prepared = build_answer_binding(
                owner_user_id=self._actor.owner_user_id,
                response_id=answer,
                rendered_context=self._rendered_context,
                selected_claims=self._selected_claims,
                query_sha256=self._query_sha256,
                policy=self._policy,
                renderer_sha256=ANSWER_RENDERER_SHA256,
                prompt_sha256=prompt_sha256,
            )
            escaped = canonical_json_bytes(self._rendered_context["content"])
            start = outbound_request_bytes.find(escaped)
            if start < 0:
                raise ContractViolation("outbound_escaped_memory_segment_mismatch")
            dispatched = mark_answer_binding_dispatched(
                prepared,
                self._rendered_context,
                outbound_request_bytes=outbound_request_bytes,
                escaped_segment_start_utf8=start,
                escaped_segment_end_utf8=start + len(escaped),
            )
            provenance = build_successor_exposed_provenance_v1(dispatched)
            await self._repository.persist_answer_binding(
                actor=self._actor,
                operation_id=self._operation_id,
                thread_id=self._actor.thread_id,
                binding=dispatched,
                memory_block=str(self._rendered_context["content"]),
                outbound_request=outbound_request,
            )
            return provenance
        finally:
            self._clear_selected_state()


__all__ = [
    "EXCLUSIVE_MODE_ENV",
    "EXCLUSIVE_MODE_LEGACY",
    "EXCLUSIVE_MODE_SUCCESSOR",
    "SUCCESSOR_CONTEXT_CONTRACT",
    "InactiveSuccessorMemoryProviderV1",
    "SuccessorGovernedMemoryAssemblyProviderV1",
    "SuccessorResponseActorBinding",
    "SuccessorResponseConfigurationError",
    "SuccessorResponseEmbedder",
    "SuccessorResponseRepository",
    "SuccessorResponseVectorIndex",
    "choose_response_memory_provider",
    "response_mode_from_environment",
]
