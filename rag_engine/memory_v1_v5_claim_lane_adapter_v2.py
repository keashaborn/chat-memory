from __future__ import annotations

from typing import Callable, Mapping

from .memory_v1_entity_scope_v2 import (
    EntityScopeError,
    MemoryClaimSelectorContextV2,
    claim_row_matches_entity_scope_v2,
)
from .memory_v1_governed_lane_adapter_common import GovernedLaneAdapterError
from .memory_v1_selection_envelope import (
    MemoryLaneLimitV1,
    MemoryLaneSelectionResultV1,
    MemorySelectionRequestBindingV1,
    MemorySelectionRequestV1,
    SourceContractVersionV1,
)
from .memory_v1_v5_claim_lane_adapter import (
    CandidateDiscovererV1,
    ClaimRowLoaderV1,
    PredicatePrefixResolverV1,
    V5ClaimLaneAdapterV1,
)


SelectorContextResolverV2 = Callable[
    [MemorySelectionRequestV1], MemoryClaimSelectorContextV2
]


class V5ClaimLaneAdapterV2:
    """Bind the V1 envelope to exact server-resolved entity endpoints."""

    def __init__(
        self,
        *,
        source_contract: SourceContractVersionV1,
        candidate_discoverer: CandidateDiscovererV1,
        row_loader: ClaimRowLoaderV1,
        predicate_prefix_resolver: PredicatePrefixResolverV1,
        selector_context_resolver: SelectorContextResolverV2,
        candidate_limit: int = 24,
        minimum_semantic_score: float = 0.20,
        relative_semantic_ratio: float = 0.40,
    ) -> None:
        self._source_contract = source_contract
        self._candidate_discoverer = candidate_discoverer
        self._row_loader = row_loader
        self._predicate_resolver = predicate_prefix_resolver
        self._context_resolver = selector_context_resolver
        self._candidate_limit = candidate_limit
        self._minimum_semantic_score = minimum_semantic_score
        self._relative_semantic_ratio = relative_semantic_ratio

    async def select(
        self,
        request: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1:
        try:
            request = request.strict_revalidated()
            binding = MemorySelectionRequestBindingV1.from_request(request)
            context = self._context_resolver(request)
            context = MemoryClaimSelectorContextV2.model_validate_json(
                context.model_dump_json()
            )
        except Exception as exc:
            raise GovernedLaneAdapterError(
                "claim V2 adapter received an invalid bound entity scope"
            ) from exc
        if (
            context.owner_user_id != request.owner_user_id
            or context.selection_trace_id != request.selection_trace_id
            or context.request_binding_sha256 != binding.request_binding_sha256
        ):
            raise GovernedLaneAdapterError(
                "claim V2 entity scope differs from the selection request"
            )
        configured_predicates = tuple(
            sorted(
                {
                    str(value).strip().casefold()
                    for value in self._predicate_resolver(request)
                    if str(value).strip()
                }
            )
        )
        if configured_predicates != context.allowed_predicates:
            raise GovernedLaneAdapterError(
                "claim V2 predicate permissions differ from entity-scope rules"
            )

        def row_policy(
            row: Mapping[str, object],
            inner_request: MemorySelectionRequestV1,
        ) -> bool:
            if (
                inner_request.owner_user_id != request.owner_user_id
                or inner_request.selection_trace_id != request.selection_trace_id
            ):
                raise GovernedLaneAdapterError(
                    "claim V2 row policy request binding changed"
                )
            try:
                return claim_row_matches_entity_scope_v2(row, context)
            except EntityScopeError as exc:
                raise GovernedLaneAdapterError(
                    "claim V2 row violated the entity-scope contract"
                ) from exc

        adapter = V5ClaimLaneAdapterV1(
            source_contract=self._source_contract,
            candidate_discoverer=self._candidate_discoverer,
            row_loader=self._row_loader,
            predicate_prefix_resolver=lambda _request: context.allowed_predicates,
            row_policy_evaluator=row_policy,
            candidate_limit=self._candidate_limit,
            minimum_semantic_score=self._minimum_semantic_score,
            relative_semantic_ratio=self._relative_semantic_ratio,
        )
        return await adapter.select(request, lane_limit)


__all__ = ["SelectorContextResolverV2", "V5ClaimLaneAdapterV2"]
