from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import unittest
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from rag_engine.memory_v1_entity_scope_v2 import (
    EntityScopeModeV2,
    MemoryClaimEntityScopeV2,
    MemoryClaimSelectorContextV2,
    ObjectScopeKindV2,
    PredicateEntityScopeRuleV2,
)
from rag_engine.memory_v1_governed_lane_adapter_common import GovernedLaneAdapterError
from rag_engine.memory_v1_selection_envelope import (
    BUDGET_POLICY_VERSION,
    TOKEN_ESTIMATOR_VERSION,
    MemoryLane,
    MemoryLaneLimitV1,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestBindingV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    RejectionCode,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
    SurfacePolicy,
    UseInstruction,
)
from rag_engine.memory_v1_v5_claim_lane_adapter_v2 import V5ClaimLaneAdapterV2


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
TRACE = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
CLAIM_SOURCE = SourceContractVersionV1(
    name="claim_projection", version="memory_projection_v5"
)


def uid(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


SELF = uid(701)
PET = uid(702)


def lane_limit() -> MemoryLaneLimitV1:
    return MemoryLaneLimitV1(
        lane=MemoryLane.CLAIM,
        max_records=4,
        max_tokens=600,
    )


def claim_request() -> MemorySelectionRequestV1:
    vector = (0.125, -0.25, 0.5)
    limit = lane_limit()
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_test_v1",
        source_contract_versions=(CLAIM_SOURCE,),
        selection_trace_id=TRACE,
        authenticated_actor_user_id=OWNER,
        owner_user_id=OWNER,
        request_id="adapter-v2-test",
        thread_id=THREAD,
        query_text="What do you know about my pets?",
        query_vector=vector,
        query_embedding=QueryEmbeddingArtifactV1.from_vector(
            source=QueryEmbeddingSource.PRIVATE_LOCAL,
            model_version="bge-m3@test",
            vector=vector,
        ),
        memory_intent="pet_recall",
        domains=("personal",),
        requested_lanes=(MemoryLane.CLAIM,),
        selection_directive=SelectionDirective.EVALUATE,
        explicit_recall=True,
        project_key=None,
        component_key=None,
        max_sensitivity=Sensitivity.HIGH,
        selected_at=NOW,
        budget_policy=MemorySelectionBudgetPolicyV1(
            policy_version=BUDGET_POLICY_VERSION,
            token_estimator_version=TOKEN_ESTIMATOR_VERSION,
            max_records=4,
            max_tokens=600,
            max_controls=4,
            lane_limits=(limit,),
        ),
    )


def claim_discovery(claim_id: UUID) -> dict[str, Any]:
    hits = [{"rank": 1, "claim_id": str(claim_id), "semantic_score": 0.91}]
    vector_bytes = struct.pack("!3d", 0.125, -0.25, 0.5)
    material = {
        "version": "memory_v1_v5_shadow_candidate_v1",
        "owner_user_id": str(OWNER),
        "collection": "memory_v1_test_claims",
        "query_vector_dimension": 3,
        "query_vector_sha256": hashlib.sha256(vector_bytes).hexdigest(),
        "candidate_limit": 24,
        "candidate_hits": hits,
    }
    return {
        **material,
        "candidate_count": 1,
        "candidate_set_sha256": hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "database_writes": 0,
        "qdrant_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


def enriched_claim_row(claim_id: UUID) -> dict[str, Any]:
    return {
        "owner_user_id": OWNER,
        "claim_id": claim_id,
        "revision_id": uid(51),
        "canonical_key": "v5:relationship:pet",
        "canonical_text": "Dahlia was Eric's dog.",
        "predicate": "relationship.has_pet",
        "status": "supported",
        "sensitivity": "medium",
        "importance": 0.8,
        "salience": 0.7,
        "valid_from": None,
        "valid_to": None,
        "superseded_by": None,
        "metadata": {"memory_contract": "memory_projection_v5"},
        "retrieval_policy": {"surface_policy": "direct_or_relevant"},
        "projection_review_decision": "authorized",
        "projection_apply_outcome": "applied",
        "evidence_by_stance": {
            "context": [],
            "opposes": [],
            "qualifies": [],
            "supports": [str(uid(101))],
        },
        "observation_ids": [str(uid(201))],
        "project_key": None,
        "component_key": None,
        "source_content_sha256": "a" * 64,
    }


def selector_context(
    selection_request: MemorySelectionRequestV1,
    *,
    subject: UUID,
    object_entity: UUID | None,
    predicate: str,
    request_binding_sha256: str | None = None,
) -> MemoryClaimSelectorContextV2:
    binding = MemorySelectionRequestBindingV1.from_request(selection_request)
    scope = MemoryClaimEntityScopeV2.create(
        owner_user_id=selection_request.owner_user_id,
        selection_trace_id=selection_request.selection_trace_id,
        mode=EntityScopeModeV2.PET_PROFILE,
        resolution_policy_version="memory_entity_resolution_policy_v2",
        request_binding_sha256=(
            request_binding_sha256 or binding.request_binding_sha256
        ),
        query_entity_hint_sha256s=("a" * 64,),
        predicate_rules=(
            PredicateEntityScopeRuleV2(
                predicate=predicate,
                subject_entity_ids=(subject,),
                object_scope=(
                    ObjectScopeKindV2.ALLOWLISTED_ENTITY
                    if object_entity is not None
                    else ObjectScopeKindV2.LITERAL_ONLY
                ),
                object_entity_ids=(
                    (object_entity,) if object_entity is not None else ()
                ),
            ),
        ),
    )
    return MemoryClaimSelectorContextV2.create(entity_scope=scope)


def adapter(
    row: Mapping[str, object],
    context_resolver,
    *,
    predicate: str,
) -> V5ClaimLaneAdapterV2:
    claim_id = uid(1)

    async def load(
        owner: UUID, claim_ids: Sequence[UUID]
    ) -> Mapping[str, object]:
        return {
            "owner_user_id": owner,
            "claim_ids": list(claim_ids),
            "records": [dict(row)],
            "controls": {
                "effective_role_brains_app": True,
                "transaction_read_only": True,
                "restricted_read_contract": True,
            },
            "database_writes": 0,
        }

    return V5ClaimLaneAdapterV2(
        source_contract=CLAIM_SOURCE,
        candidate_discoverer=lambda owner, vector, limit: claim_discovery(claim_id),
        row_loader=load,
        predicate_prefix_resolver=lambda _: (predicate,),
        selector_context_resolver=context_resolver,
    )


def pet_relationship_row() -> dict[str, object]:
    row = enriched_claim_row(uid(1))
    row.update(
        {
            "subject_entity_id": SELF,
            "subject_entity_type": "self",
            "object_entity_id": PET,
            "object_entity_type": "animal",
        }
    )
    return row


def reported_stance_row() -> dict[str, object]:
    row = enriched_claim_row(uid(1))
    row.update(
        {
            "canonical_key": "v5:stance:reported:public_opinion_evidence",
            "canonical_text": (
                "Eric reports that public opinion is not the same as evidence."
            ),
            "predicate": "stance.reported",
            "retrieval_policy": {
                "surface_policy": "relevant_recall_or_explicit_recall"
            },
            "subject_entity_id": SELF,
            "subject_entity_type": "self",
            "object_entity_id": None,
            "object_entity_type": None,
        }
    )
    return row


class ClaimLaneAdapterV2Test(unittest.TestCase):
    def claim_request(self) -> MemorySelectionRequestV1:
        return claim_request()

    def test_exact_pet_edge_is_selected(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=PET,
            predicate="relationship.has_pet",
        )
        result = asyncio.run(
            adapter(
                pet_relationship_row(),
                lambda _: value,
                predicate="relationship.has_pet",
            ).select(selection_request, lane_limit())
        )
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].predicate, "relationship.has_pet")

    def test_reported_stance_alias_maps_to_frozen_direct_relevance_policy(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=None,
            predicate="stance.reported",
        )
        result = asyncio.run(
            adapter(
                reported_stance_row(),
                lambda _: value,
                predicate="stance.reported",
            ).select(selection_request, lane_limit())
        )
        self.assertEqual(len(result.records), 1)
        self.assertEqual(
            result.records[0].surface_policy,
            SurfacePolicy.MENTION_WHEN_DIRECTLY_RELEVANT,
        )
        self.assertEqual(
            result.records[0].use_instruction,
            UseInstruction.MENTION_ONLY_WHEN_DIRECTLY_RELEVANT,
        )

    def test_unknown_stored_surface_alias_fails_closed(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=None,
            predicate="stance.reported",
        )
        row = reported_stance_row()
        row["retrieval_policy"] = {"surface_policy": "unreviewed_surface"}
        result = asyncio.run(
            adapter(
                row,
                lambda _: value,
                predicate="stance.reported",
            ).select(selection_request, lane_limit())
        )
        self.assertEqual(result.records, ())
        counts = {item.code: item.count for item in result.reason_counts}
        self.assertEqual(counts[RejectionCode.SURFACE_POLICY], 1)

    def test_wrong_subject_is_rejected_as_entity_scope(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=PET,
            object_entity=PET,
            predicate="relationship.has_pet",
        )
        result = asyncio.run(
            adapter(
                pet_relationship_row(),
                lambda _: value,
                predicate="relationship.has_pet",
            ).select(selection_request, lane_limit())
        )
        self.assertEqual(result.records, ())
        counts = {item.code: item.count for item in result.reason_counts}
        self.assertEqual(counts[RejectionCode.ENTITY_SCOPE], 1)

    def test_cross_owner_row_fails_closed(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=PET,
            predicate="relationship.has_pet",
        )
        row = pet_relationship_row()
        row["owner_user_id"] = OTHER
        with self.assertRaisesRegex(GovernedLaneAdapterError, "entity-scope contract"):
            asyncio.run(
                adapter(
                    row,
                    lambda _: value,
                    predicate="relationship.has_pet",
                ).select(selection_request, lane_limit())
            )

    def test_scope_must_bind_exact_request(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=PET,
            predicate="relationship.has_pet",
            request_binding_sha256="f" * 64,
        )
        with self.assertRaisesRegex(GovernedLaneAdapterError, "differs"):
            asyncio.run(
                adapter(
                    pet_relationship_row(),
                    lambda _: value,
                    predicate="relationship.has_pet",
                ).select(selection_request, lane_limit())
            )

    def test_predicates_must_match_scope_rules(self) -> None:
        selection_request = self.claim_request()
        value = selector_context(
            selection_request,
            subject=SELF,
            object_entity=None,
            predicate="identity.name",
        )
        with self.assertRaisesRegex(GovernedLaneAdapterError, "permissions differ"):
            asyncio.run(
                adapter(
                    pet_relationship_row(),
                    lambda _: value,
                    predicate="relationship.has_pet",
                ).select(selection_request, lane_limit())
            )


if __name__ == "__main__":
    unittest.main()
