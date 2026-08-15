from __future__ import annotations

"""Inactive real-chat adapter for the clean governed-Memory successor.

The adapter is deliberately composed from injected PostgreSQL, Qdrant, and
embedding boundaries.  Importing it and selecting the default ``off`` mode do
not open resources.  Qdrant supplies only bounded candidate identifiers;
PostgreSQL rows are revalidated before any prompt content is constructed.
"""

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
from rag_engine.governed_memory.response_contracts import (
    SuccessorMemoryAssemblyV1,
    SuccessorPromptReferenceContextBlockV1,
    SuccessorPromptReferenceFragmentV1,
    SuccessorResponseActorBinding,
    SuccessorResponseRequestV1,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision


EXCLUSIVE_MODE_SUCCESSOR = ExclusiveMemoryMode.SUCCESSOR_PILOT.value
SUCCESSOR_CONTEXT_CONTRACT = "governed-memory-answer-context-v1"


class SuccessorResponseConfigurationError(RuntimeError):
    """A successor response lane was requested without exact runtime wiring."""


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
        explicit_recall: bool,
    ) -> tuple[dict[str, object], ...]: ...


class SuccessorResponseEmbedder(Protocol):
    def embed(self, text: str) -> Sequence[float] | Awaitable[Sequence[float]]: ...


class SuccessorResponseRelevanceGate(Protocol):
    async def relevant_claim_ids(
        self,
        *,
        owner_user_id: UUID,
        query: str,
        claims: tuple[Mapping[str, object], ...],
    ) -> tuple[UUID, ...]: ...


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

    @property
    def not_applicable_reason(self) -> SuccessorMemoryNotApplicableReason:
        return self._reason

    def discard_selected_state(self) -> None:
        if self._prepared:
            self._terminal = True

    def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        if not isinstance(request, SuccessorResponseRequestV1):
            raise ContractViolation("invalid_response_memory_request_contract")
        if self._prepared:
            raise ContractViolation("response_memory_provider_reused")
        self._prepared = True
        return SuccessorMemoryAssemblyV1()

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
    successor_factory: Callable[[], object],
) -> tuple[object, object | None]:
    """Construct the successor provider; failures never fall back."""

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


def _has_mixed_personal_recall_scope(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            ", what ",
            ", what's ",
            ", which ",
            ", who ",
            ", who's ",
            ", where ",
            ", when ",
            " and what ",
            " and what's ",
            " and which ",
            " and who ",
            " and who's ",
            " and where ",
            " and when ",
            " considering",
            " considered",
            " weighing",
            " undecided",
            " future alternative",
            " future option",
            " possible direction",
        )
    )


_SOURCE_ATTRIBUTION_CLAUSE_MARKERS = (
    " and did that information come from",
    " and did this information come from",
    " and did that answer come from",
    " and did this answer come from",
    " and did you get that from",
    " and did you get this from",
    " and where did that information come from",
    " and where did this information come from",
    " and where did you get that",
    " and where did you get this",
    " and what source did you use",
    " and which source did you use",
)


def _recall_subject_query(normalized: str) -> str:
    """Remove a trailing source-attribution question from a recall query."""

    for marker in _SOURCE_ATTRIBUTION_CLAUSE_MARKERS:
        index = normalized.find(marker)
        if index > 0:
            subject = normalized[:index].rstrip(" ,;:")
            return f"{subject}?"
    return normalized


def _is_explicit_preference_recall(query: str) -> bool:
    if not isinstance(query, str):
        return False
    normalized = " ".join(query.replace("’", "'").casefold().split())
    normalized = _recall_subject_query(normalized)
    if (
        not normalized.endswith("?")
        or len(normalized) > 256
        or " or " in normalized
    ):
        return False
    preference_language = any(
        token in normalized
        for token in (
            " prefer",
            "preferred",
            "favorite",
            "favourite",
            "go-to",
            "go to",
            "usual choice",
            "first choice",
            "typically choose",
            "normally choose",
        )
    )
    if not preference_language or _has_mixed_personal_recall_scope(normalized):
        return False
    if normalized.startswith(("what is my ", "what's my ")):
        return True
    if normalized.startswith("which ") and normalized.endswith(
        (" do i prefer?", " did i prefer?")
    ):
        return True
    return normalized.startswith(("do you remember ", "can you recall ")) and (
        " my " in normalized or " i prefer" in normalized
    )


def _is_explicit_personal_recall(query: str) -> bool:
    if not isinstance(query, str):
        return False
    normalized = " ".join(query.replace("’", "'").casefold().split())
    normalized = _recall_subject_query(normalized)
    if (
        not normalized.endswith("?")
        or len(normalized) > 256
        or " or " in normalized
    ):
        return False
    if any(
        marker in normalized
        for marker in (
            " should i ",
            " could i ",
            " can i ",
            " would i ",
            " will i ",
        )
    ):
        return False
    if normalized.startswith(
        (
            "what should i ",
            "what could i ",
            "what can i ",
            "what would i ",
            "what will i ",
            "which should i ",
            "which could i ",
            "which can i ",
            "which would i ",
            "which will i ",
            "who should i ",
            "where should i ",
            "when should i ",
        )
    ):
        return False
    if normalized.startswith(("do you remember ", "can you recall ")):
        return any(
            marker in normalized
            for marker in (" my ", " our ", " i ", " we ")
        )
    if normalized.startswith(
        (
            "what is my ",
            "what's my ",
            "who is my ",
            "who's my ",
            "which is my ",
            "where is my ",
            "when is my ",
        )
    ):
        return True
    if not normalized.startswith(("what ", "which ", "who ", "where ", "when ")):
        return False
    return any(
        marker in normalized
        for marker in (
            " do i ",
            " did i ",
            " have i ",
            " was i ",
            " do we ",
            " did we ",
            " have we ",
            " were we ",
        )
    )


def _explicit_preference_policy(policy: RetrievalPolicy) -> RetrievalPolicy:
    if "preference.personal" not in policy.allowed_predicates:
        raise ContractViolation("preference_recall_predicate_unavailable")
    return RetrievalPolicy(
        explicit_recall=True,
        allowed_predicates=("preference.personal",),
        domains=policy.domains,
        intents=policy.intents,
        max_records=1,
        policy_revision=policy.policy_revision,
    )


def _explicit_personal_policy(policy: RetrievalPolicy) -> RetrievalPolicy:
    return RetrievalPolicy(
        explicit_recall=True,
        allowed_predicates=policy.allowed_predicates,
        domains=policy.domains,
        intents=policy.intents,
        max_records=policy.max_records,
        policy_revision=policy.policy_revision,
    )


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
        relevance_gate: SuccessorResponseRelevanceGate,
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
        if (
            repository is None
            or vector_index is None
            or embedder is None
            or relevance_gate is None
        ):
            raise ContractViolation("response_memory_adapter_required")
        if not isinstance(predicate_catalog, Mapping):
            raise ContractViolation("invalid_response_memory_predicate_catalog")
        if not isinstance(calibration, CalibrationDecision):
            raise ContractViolation("invalid_retrieval_calibration")
        self._actor = actor
        self._repository = repository
        self._vector_index = vector_index
        self._embedder = embedder
        self._relevance_gate = relevance_gate
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

    def _finish_without_selection(self) -> SuccessorMemoryAssemblyV1:
        self._no_selection_pending = True
        self._policy = None
        self._selected_claims = ()
        self._rendered_context = None
        self._query_sha256 = None
        return SuccessorMemoryAssemblyV1()

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

    def _verified_request(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorResponseRequestV1:
        if not isinstance(request, SuccessorResponseRequestV1):
            raise ContractViolation("invalid_response_memory_request_contract")
        verified = SuccessorResponseRequestV1(
            authenticated_actor_user_id=request.authenticated_actor_user_id,
            thread_id=request.thread_id,
            request_id=request.request_id,
            current_message=request.current_message,
            conversation_snapshot_sha256=request.conversation_snapshot_sha256,
            trusted_policy_signals_sha256=request.trusted_policy_signals_sha256,
            contract_version=request.contract_version,
        )
        if (
            verified.authenticated_actor_user_id != self._actor.owner_user_id
            or verified.request_id != self._actor.request_id
            or verified.thread_id != self._actor.thread_id
        ):
            raise ContractViolation("response_memory_request_binding_mismatch")
        return verified

    async def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        try:
            return await self._prepare_once(request=request)
        except Exception:
            self._clear_selected_state()
            raise

    async def _prepare_once(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        if self._prepared:
            raise ContractViolation("response_memory_provider_reused")
        self._prepared = True
        verified_request = self._verified_request(request=request)
        query = verified_request.current_message
        self._query_sha256 = _sha256_text(query)
        if not self._actor.eligible:
            raise ContractViolation("ineligible_successor_response_provider")
        if not self._calibration.retrieval_enabled:
            raise ContractViolation("successor_response_calibration_disabled")

        policy = self._policy_factory(self._predicate_catalog)
        if not isinstance(policy, RetrievalPolicy):
            raise ContractViolation("invalid_response_memory_retrieval_policy")
        if _is_explicit_preference_recall(query):
            policy = _explicit_preference_policy(policy)
        elif _is_explicit_personal_recall(query):
            policy = _explicit_personal_policy(policy)
        self._policy = policy
        query_vector = await _await_embedding(self._embedder.embed(query))
        candidates = await self._vector_index.search_owner_candidates(
            owner_user_id=self._actor.owner_user_id,
            query_vector=query_vector,
            allowed_predicates=policy.allowed_predicates,
            limit=policy.max_records,
            calibration=self._calibration,
            explicit_recall=policy.explicit_recall,
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
        try:
            relevant_claim_ids = await self._relevance_gate.relevant_claim_ids(
                owner_user_id=self._actor.owner_user_id,
                query=query,
                claims=tuple(selected),
            )
            if (
                not isinstance(relevant_claim_ids, tuple)
                or len(set(relevant_claim_ids)) != len(relevant_claim_ids)
            ):
                return self._finish_without_selection()
            selected_ids = {
                UUID(str(item["claim_id"])) for item in selected
            }
            if any(
                not isinstance(claim_id, UUID)
                or claim_id not in selected_ids
                for claim_id in relevant_claim_ids
            ):
                return self._finish_without_selection()
        except Exception:
            return self._finish_without_selection()
        relevant_set = set(relevant_claim_ids)
        selected = tuple(
            item
            for item in selected
            if UUID(str(item["claim_id"])) in relevant_set
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
        block = SuccessorPromptReferenceContextBlockV1(
            block_id="governed_memory_successor_v1",
            kind="memory",
            source_contract_version=SUCCESSOR_CONTEXT_CONTRACT,
            source_manifest_sha256=content_sha256,
            request_id_sha256=_sha256_text(self._actor.request_id),
            query_sha256=self._query_sha256,
            content=content,
            content_sha256=content_sha256,
            content_bytes=len(raw),
            estimated_tokens=math.ceil(len(raw) / 4),
            fragments=(
                SuccessorPromptReferenceFragmentV1(
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
        return SuccessorMemoryAssemblyV1(
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
    "EXCLUSIVE_MODE_SUCCESSOR",
    "SUCCESSOR_CONTEXT_CONTRACT",
    "InactiveSuccessorMemoryProviderV1",
    "SuccessorGovernedMemoryAssemblyProviderV1",
    "SuccessorResponseActorBinding",
    "SuccessorResponseConfigurationError",
    "SuccessorResponseEmbedder",
    "SuccessorResponseRepository",
    "SuccessorResponseRelevanceGate",
    "SuccessorResponseVectorIndex",
    "choose_response_memory_provider",
    "response_mode_from_environment",
]
