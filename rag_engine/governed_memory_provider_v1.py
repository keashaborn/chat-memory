from __future__ import annotations

"""Live governed Memory V1 adapter for RESSE response composition."""

import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from rag_engine.memory_prompt_renderer_v1 import (
    MEMORY_PROMPT_RENDERER_VERSION,
    MemoryControlApplicationDecisionV1,
    apply_memory_control_decision_v1,
    render_governed_memory_v1,
)
from rag_engine.memory_v1_governed_postgres_loaders_v1 import (
    load_governed_preference_snapshot_v1,
    load_governed_v5_claim_rows_v1,
)
from rag_engine.memory_v1_intent import VERSION as INTENT_VERSION
from rag_engine.memory_v1_intent import classify_memory_intent
from rag_engine.memory_v1_preference_lane_adapter import GovernedPreferenceLaneAdapterV1
from rag_engine.memory_v1_projection import ClaimVectorIndex
from rag_engine.memory_v1_selection_envelope import (
    AuthoritativeGovernedMemorySelectorV1,
    MemoryLane,
    MemoryPromptAssemblyContextV1,
    MemoryPromptAssemblyInputV1,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
    select_governed_memory_v1,
)
from rag_engine.memory_v1_v5_claim_lane_adapter import V5ClaimLaneAdapterV1
from rag_engine.memory_v1_v5_shadow_candidate import discover_v5_shadow_candidates
from rag_engine.openai_client import embed_text
from rag_engine.qdrant_compat import make_qdrant_client
from rag_engine.response_composition_root_v0_2 import GovernedMemoryAssemblyV1
from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1


CLAIM_SOURCE = SourceContractVersionV1(
    name="claim_projection", version="memory_projection_v5"
)
PREFERENCE_SOURCE = SourceContractVersionV1(
    name="preference_projection", version="memory_preference_projection_v1"
)


class LiveGovernedMemoryAssemblyProviderV1:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
    ) -> GovernedMemoryAssemblyV1:
        query = conversation_snapshot.messages[-1].content
        intent = classify_memory_intent(query, request_classification="GENERAL")
        claim_context = dict(intent.get("claim_context") or {})
        allowed_predicates = tuple(
            sorted(
                {
                    str(value).strip().casefold()
                    for value in claim_context.get("allowed_predicates", [])
                    if str(value).strip()
                }
            )
        )

        requested: list[MemoryLane] = []
        if intent.get("routes", {}).get("governed_claims") and allowed_predicates:
            requested.append(MemoryLane.CLAIM)
        if intent.get("memory_intent") in {"preference_recall", "recommendation"}:
            requested.append(MemoryLane.PREFERENCE)
        if not requested:
            return GovernedMemoryAssemblyV1()

        vector: tuple[float, ...] = ()
        embedding = QueryEmbeddingArtifactV1.not_used()
        if MemoryLane.CLAIM in requested:
            model = os.getenv("EMBED_MODEL", "text-embedding-3-large")
            vector = tuple(await asyncio.to_thread(embed_text, query, model=model))
            embedding = QueryEmbeddingArtifactV1.from_vector(
                source=QueryEmbeddingSource.EXTERNAL,
                model_version=model,
                vector=vector,
            )

        providers: dict[MemoryLane, object] = {}
        qdrant = None
        try:
            if MemoryLane.CLAIM in requested:
                qdrant_url = (os.getenv("QDRANT_URL") or "").strip()
                if not qdrant_url:
                    raise RuntimeError("QDRANT_URL is required for governed claim selection")
                qdrant = make_qdrant_client(url=qdrant_url, timeout=15.0)
                index = ClaimVectorIndex(
                    qdrant,
                    collection_name=os.getenv("MEMORY_V1_COLLECTION", "memory_claim_v1"),
                    vector_size=len(vector),
                )
                providers[MemoryLane.CLAIM] = V5ClaimLaneAdapterV1(
                    source_contract=CLAIM_SOURCE,
                    candidate_discoverer=lambda owner, values, limit: (
                        discover_v5_shadow_candidates(index, owner, values, limit=limit)
                    ),
                    row_loader=lambda owner, claim_ids: load_governed_v5_claim_rows_v1(
                        self._conn, owner, claim_ids
                    ),
                    predicate_prefix_resolver=lambda _request: allowed_predicates,
                )
            if MemoryLane.PREFERENCE in requested:
                providers[MemoryLane.PREFERENCE] = GovernedPreferenceLaneAdapterV1(
                    source_contract=PREFERENCE_SOURCE,
                    snapshot_loader=lambda owner: load_governed_preference_snapshot_v1(
                        self._conn, owner
                    ),
                )

            request = MemorySelectionRequestV1.create(
                intent_adapter_version=INTENT_VERSION,
                source_contract_versions=tuple(
                    source
                    for lane, source in (
                        (MemoryLane.CLAIM, CLAIM_SOURCE),
                        (MemoryLane.PREFERENCE, PREFERENCE_SOURCE),
                    )
                    if lane in requested
                ),
                selection_trace_id=uuid4(),
                authenticated_actor_user_id=authenticated_actor_user_id,
                owner_user_id=authenticated_actor_user_id,
                request_id=conversation_snapshot.current_request_id,
                thread_id=conversation_snapshot.thread_id,
                query_text=query,
                query_vector=vector,
                query_embedding=embedding,
                memory_intent=str(intent.get("memory_intent") or "none"),
                domains=tuple(sorted(set(intent.get("domains") or ()))),
                requested_lanes=tuple(requested),
                selection_directive=SelectionDirective.EVALUATE,
                explicit_recall=bool(claim_context.get("explicit_recall")),
                project_key=None,
                component_key=None,
                max_sensitivity=(
                    Sensitivity.HIGH
                    if bool(claim_context.get("explicit_recall"))
                    else Sensitivity.MEDIUM
                ),
                selected_at=datetime.now(timezone.utc),
                budget_policy=MemorySelectionBudgetPolicyV1.standard(),
            )
            envelope = await select_governed_memory_v1(
                AuthoritativeGovernedMemorySelectorV1(providers), request
            )
            context = MemoryPromptAssemblyContextV1.from_envelope(
                envelope=envelope,
                authenticated_actor_user_id=authenticated_actor_user_id,
                renderer_version=MEMORY_PROMPT_RENDERER_VERSION,
            )
            memory_input = MemoryPromptAssemblyInputV1.create(
                context=context,
                envelope=envelope,
            )
            rendered = render_governed_memory_v1(memory_input=memory_input)
            decision = MemoryControlApplicationDecisionV1.create(
                render_result=rendered,
                direct_relevance_confirmed=bool(intent.get("direct_relevance")),
            )
            application = apply_memory_control_decision_v1(
                render_result=rendered,
                decision=decision,
            )
            return GovernedMemoryAssemblyV1(
                memory_input=memory_input,
                memory_application=application,
            )
        finally:
            if qdrant is not None:
                qdrant.close()


__all__ = ["LiveGovernedMemoryAssemblyProviderV1"]
