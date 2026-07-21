from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

import pytest

from rag_engine.memory_v1_governed_lane_adapter_common import (
    GovernedLaneAdapterError,
    GovernedLaneSchemaGap,
)
from rag_engine.memory_v1_preference_lane_adapter import (
    GovernedPreferenceLaneAdapterV1,
)
from rag_engine.memory_v1_selection_envelope import (
    BUDGET_POLICY_VERSION,
    TOKEN_ESTIMATOR_VERSION,
    MemoryLane,
    MemoryLaneLimitV1,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    RejectionCode,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
)
from rag_engine.memory_v1_v5_claim_lane_adapter import V5ClaimLaneAdapterV1
from rag_engine.memory_v1_v5_shadow_candidate import VERSION as CANDIDATE_VERSION
from rag_engine.memory_v1_v5_project_lane_adapter import V5ProjectLaneAdapterV1


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
TRACE = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)

CLAIM_SOURCE = SourceContractVersionV1(
    name="claim_projection", version="memory_projection_v5"
)
PREFERENCE_SOURCE = SourceContractVersionV1(
    name="preference_projection", version="memory_preference_projection_v1"
)
PROJECT_SOURCE = SourceContractVersionV1(
    name="project_projection", version="memory_project_projection_v5"
)


def uid(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def lane_limit(lane: MemoryLane, *, records: int = 4, tokens: int = 600) -> MemoryLaneLimitV1:
    return MemoryLaneLimitV1(lane=lane, max_records=records, max_tokens=tokens)


def request(
    lane: MemoryLane,
    source: SourceContractVersionV1,
    *,
    memory_intent: str,
    domains: tuple[str, ...] = (),
    explicit_recall: bool = True,
) -> MemorySelectionRequestV1:
    vector = (0.125, -0.25, 0.5) if lane == MemoryLane.CLAIM else ()
    embedding = (
        QueryEmbeddingArtifactV1.from_vector(
            source=QueryEmbeddingSource.PRIVATE_LOCAL,
            model_version="bge-m3@test",
            vector=vector,
        )
        if vector
        else QueryEmbeddingArtifactV1.not_used()
    )
    limit = lane_limit(lane)
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_test_v1",
        source_contract_versions=(source,),
        selection_trace_id=TRACE,
        authenticated_actor_user_id=OWNER,
        owner_user_id=OWNER,
        request_id="adapter-test",
        thread_id=THREAD,
        query_text="What is relevant now?",
        query_vector=vector,
        query_embedding=embedding,
        memory_intent=memory_intent,
        domains=domains,
        requested_lanes=(lane,),
        selection_directive=SelectionDirective.EVALUATE,
        explicit_recall=explicit_recall,
        project_key=("verbal-sage" if lane == MemoryLane.PROJECT_KNOWLEDGE else None),
        component_key=("memory-v1" if lane == MemoryLane.PROJECT_KNOWLEDGE else None),
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
        "version": CANDIDATE_VERSION,
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


def current_claim_row(claim_id: UUID) -> dict[str, Any]:
    """Exact field shape currently returned by read_v5_shadow_claims."""

    return {
        "owner_user_id": OWNER,
        "claim_id": claim_id,
        "canonical_key": "v5:relationship:pet",
        "canonical_text": "Dahlia was Eric's dog.",
        "predicate": "relationship.has_pet",
        "status": "supported",
        "sensitivity": "medium",
        "importance": 0.8,
        "salience": 0.7,
        "valid_from": None,
        "valid_to": None,
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
    }


def enriched_claim_row(claim_id: UUID) -> dict[str, Any]:
    row = current_claim_row(claim_id)
    row.update(
        {
            "revision_id": uid(51),
            "superseded_by": None,
            "component_key": None,
            "source_content_sha256": "a" * 64,
        }
    )
    return row


def claim_adapter(
    rows: Sequence[Mapping[str, Any]],
    *,
    discovery: Mapping[str, Any] | None = None,
    predicate_permissions: tuple[str, ...] = ("relationship.",),
) -> V5ClaimLaneAdapterV1:
    claim_id = uid(1)

    def discover(
        owner: UUID, vector: tuple[float, ...], *, limit: int
    ) -> Mapping[str, Any]:
        assert owner == OWNER
        assert vector
        assert limit == 24
        return discovery or claim_discovery(claim_id)

    async def load(
        owner: UUID, claim_ids: Sequence[UUID]
    ) -> Mapping[str, Any]:
        assert owner == OWNER
        assert tuple(claim_ids) == (claim_id,)
        return {
            "owner_user_id": owner,
            "claim_ids": list(claim_ids),
            "records": list(rows),
            "controls": {
                "effective_role_brains_app": True,
                "transaction_read_only": True,
                "restricted_read_contract": True,
            },
            "database_writes": 0,
        }

    return V5ClaimLaneAdapterV1(
        source_contract=CLAIM_SOURCE,
        candidate_discoverer=discover,
        row_loader=load,
        predicate_prefix_resolver=lambda _: predicate_permissions,
    )


def current_project_row() -> dict[str, Any]:
    """Exact field shape currently returned by read_v5_shadow_project_knowledge."""

    return {
        "owner_user_id": OWNER,
        "thread_id": THREAD,
        "project_id": uid(301),
        "project_key": "verbal-sage",
        "component_id": uid(302),
        "component_key": "memory-v1",
        "knowledge_id": uid(5),
        "knowledge_kind": "current_state",
        "knowledge_key": "architecture:postgres_authority",
        "canonical_text": "Memory V1 uses Postgres as its authority.",
        "status": "active",
        "revision_id": uid(55),
        "revision_number": 1,
        "document_state": "ratified",
        "authority_level": "approved_spec",
        "surface_policy": "exact_project_scope_only",
        "content_sha256": "c" * 64,
        "effective_precision": None,
        "effective_instant_at": None,
        "effective_calendar_range": None,
        "effective_instant_range": None,
        "projection_review_decision": "authorized",
        "projection_apply_outcome": "applied",
        "evidence_ids": [str(uid(105))],
        "observation_ids": [str(uid(205))],
    }


def enriched_project_row() -> dict[str, Any]:
    row = current_project_row()
    row.update(
        {
            "sensitivity": "medium",
            "valid_from": None,
            "valid_to": None,
            "superseded_by": None,
            "source_rank": 1,
        }
    )
    return row


def project_adapter(rows: Sequence[Mapping[str, Any]]) -> V5ProjectLaneAdapterV1:
    async def load(
        owner: UUID, thread_id: UUID, *, limit: int
    ) -> Mapping[str, Any]:
        assert owner == OWNER
        assert thread_id == THREAD
        assert limit == 8
        return {
            "owner_user_id": owner,
            "thread_id": thread_id,
            "project_key": "verbal-sage",
            "component_key": "memory-v1",
            "records": list(rows),
            "controls": {
                "effective_role_brains_app": True,
                "transaction_read_only": True,
                "restricted_read_contract": True,
            },
            "database_writes": 0,
        }

    return V5ProjectLaneAdapterV1(
        source_contract=PROJECT_SOURCE,
        row_loader=load,
    )


def preference_row(number: int = 3, *, preference_class: str = "life") -> dict[str, Any]:
    preference_id = uid(number)
    revision_id = uid(50 + number)
    review_id = uid(500 + number)
    return {
        "preference_id": preference_id,
        "status": "active",
        "preference_class": preference_class,
        "preference_domain": "music",
        "preference_key": (
            "music:classical"
            if preference_class == "life"
            else "response:direct_relevance:person"
        ),
        "value": {"likes": True},
        "polarity": "likes" if preference_class == "life" else "not_applicable",
        "scope": (
            {"context": "music_recommendation"}
            if preference_class == "life"
            else {"people": ["Alex"]}
        ),
        "stability": "stable",
        "surface_policy": (
            "mention_when_relevant"
            if preference_class == "life"
            else "never_surface_as_content"
        ),
        "sensitivity": "low",
        "current_revision_id": revision_id,
        "head_revision_number": 1,
        "head_content_sha256": f"{number}" * 64,
        "head_accepted_review_id": review_id,
        "revision_id": revision_id,
        "revision_number": 1,
        "content_sha256": f"{number}" * 64,
        "accepted_review_id": review_id,
        "evidence_ids": [str(uid(100 + number))],
        "evidence_link_count": 1,
        "active_evidence_count": 1,
    }


def preference_batch(*rows: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "owner_user_id": str(OWNER),
        "preferences": list(rows),
        "controls": {
            "effective_role_brains_app": True,
            "transaction_read_only": True,
            "forced_rls": True,
        },
        "database_writes": 0,
    }


def preference_adapter(batch: Mapping[str, Any]) -> GovernedPreferenceLaneAdapterV1:
    async def load(owner: UUID) -> Mapping[str, Any]:
        assert owner == OWNER
        return batch

    return GovernedPreferenceLaneAdapterV1(
        source_contract=PREFERENCE_SOURCE,
        snapshot_loader=load,
    )


def test_claim_adapter_reports_exact_current_read_schema_gap() -> None:
    adapter = claim_adapter((current_claim_row(uid(1)),))
    with pytest.raises(GovernedLaneSchemaGap) as captured:
        asyncio.run(
            adapter.select(
                request(
                    MemoryLane.CLAIM,
                    CLAIM_SOURCE,
                    memory_intent="specific_recall",
                ),
                lane_limit(MemoryLane.CLAIM),
            )
        )
    assert captured.value.source == "memory.read_v5_shadow_claims"
    assert captured.value.missing_fields == (
        "component_key",
        "revision_id",
        "source_content_sha256",
        "superseded_by",
    )


def test_enriched_claim_row_becomes_typed_selection() -> None:
    adapter = claim_adapter((enriched_claim_row(uid(1)),))
    result = asyncio.run(
        adapter.select(
            request(
                MemoryLane.CLAIM,
                CLAIM_SOURCE,
                memory_intent="specific_recall",
            ),
            lane_limit(MemoryLane.CLAIM),
        )
    )
    assert result.candidate_count == 1
    assert result.visible_candidate_count == 1
    assert result.eligible_count == 1
    assert len(result.records) == 1
    record = result.records[0]
    assert record.source_content_sha256 == "a" * 64
    assert record.revision_id == uid(51)
    assert record.evidence_refs == (uid(101),)
    assert record.observation_refs == (uid(201),)


def test_claim_loader_cross_owner_fails_closed_without_prose() -> None:
    row = enriched_claim_row(uid(1))
    row["owner_user_id"] = OTHER
    adapter = claim_adapter((row,))
    with pytest.raises(GovernedLaneAdapterError, match="cross-owner") as captured:
        asyncio.run(
            adapter.select(
                request(
                    MemoryLane.CLAIM,
                    CLAIM_SOURCE,
                    memory_intent="specific_recall",
                ),
                lane_limit(MemoryLane.CLAIM),
            )
        )
    assert "Dahlia" not in str(captured.value)


def test_exact_predicate_permission_does_not_grant_a_string_prefix() -> None:
    row = enriched_claim_row(uid(1))
    row["predicate"] = "relationship.has_pet_unreviewed"
    result = asyncio.run(
        claim_adapter(
            (row,),
            predicate_permissions=("relationship.has_pet",),
        ).select(
            request(
                MemoryLane.CLAIM,
                CLAIM_SOURCE,
                memory_intent="specific_recall",
            ),
            lane_limit(MemoryLane.CLAIM),
        )
    )
    assert result.records == ()
    assert [(item.code, item.count) for item in result.primary_rejection_counts] == [
        (RejectionCode.PREDICATE_PERMISSION, 1)
    ]


def test_claim_candidate_manifest_tampering_fails_before_postgres_reload() -> None:
    discovery = claim_discovery(uid(1))
    discovery["candidate_set_sha256"] = "f" * 64
    with pytest.raises(GovernedLaneAdapterError, match="manifest does not reconcile"):
        asyncio.run(
            claim_adapter(
                (enriched_claim_row(uid(1)),),
                discovery=discovery,
            ).select(
                request(
                    MemoryLane.CLAIM,
                    CLAIM_SOURCE,
                    memory_intent="specific_recall",
                ),
                lane_limit(MemoryLane.CLAIM),
            )
        )


def test_project_adapter_reports_exact_current_read_schema_gap() -> None:
    adapter = project_adapter((current_project_row(),))
    with pytest.raises(GovernedLaneSchemaGap) as captured:
        asyncio.run(
            adapter.select(
                request(
                    MemoryLane.PROJECT_KNOWLEDGE,
                    PROJECT_SOURCE,
                    memory_intent="project_status",
                ),
                lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
            )
        )
    assert captured.value.source == "memory.read_v5_shadow_project_knowledge"
    assert captured.value.missing_fields == (
        "sensitivity",
        "source_rank",
        "superseded_by",
        "valid_from",
        "valid_to",
    )


def test_enriched_project_row_becomes_typed_selection() -> None:
    result = asyncio.run(
        project_adapter((enriched_project_row(),)).select(
            request(
                MemoryLane.PROJECT_KNOWLEDGE,
                PROJECT_SOURCE,
                memory_intent="project_status",
            ),
            lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
        )
    )
    assert result.candidate_count == 1
    assert result.eligible_count == 1
    assert len(result.records) == 1
    record = result.records[0]
    assert record.project_key == "verbal-sage"
    assert record.component_key == "memory-v1"
    assert record.source_content_sha256 == "c" * 64
    assert record.evidence_refs == (uid(105),)
    assert record.observation_refs == (uid(205),)


def test_project_temporal_fields_fail_until_normalization_contract_is_frozen() -> None:
    row = enriched_project_row()
    row["effective_instant_at"] = NOW
    with pytest.raises(
        GovernedLaneAdapterError,
        match="temporal normalization contract is not frozen",
    ):
        asyncio.run(
            project_adapter((row,)).select(
                request(
                    MemoryLane.PROJECT_KNOWLEDGE,
                    PROJECT_SOURCE,
                    memory_intent="project_status",
                ),
                lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
            )
        )


def test_project_loader_cannot_exceed_its_governed_scan_limit() -> None:
    rows = tuple(enriched_project_row() for _ in range(9))
    with pytest.raises(GovernedLaneAdapterError, match="governed scan limit"):
        asyncio.run(
            project_adapter(rows).select(
                request(
                    MemoryLane.PROJECT_KNOWLEDGE,
                    PROJECT_SOURCE,
                    memory_intent="project_status",
                ),
                lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
            )
        )


def test_project_revision_number_must_be_a_positive_integer() -> None:
    for invalid in (False, 0, -1):
        row = enriched_project_row()
        row["revision_number"] = invalid
        with pytest.raises(GovernedLaneAdapterError, match="revision number"):
            asyncio.run(
                project_adapter((row,)).select(
                    request(
                        MemoryLane.PROJECT_KNOWLEDGE,
                        PROJECT_SOURCE,
                        memory_intent="project_status",
                    ),
                    lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
                )
            )


def test_governed_life_preference_selects_but_response_control_stays_closed() -> None:
    batch = preference_batch(
        preference_row(3, preference_class="life"),
        preference_row(7, preference_class="response"),
    )
    result = asyncio.run(
        preference_adapter(batch).select(
            request(
                MemoryLane.PREFERENCE,
                PREFERENCE_SOURCE,
                memory_intent="recommendation",
                domains=("music",),
                explicit_recall=False,
            ),
            lane_limit(MemoryLane.PREFERENCE),
        )
    )
    assert result.candidate_count == 1
    assert result.visible_candidate_count == 1
    assert result.eligible_count == 1
    assert len(result.records) == 1
    assert result.records[0].canonical_value_json == '{"likes":true}'
    assert result.control_candidate_count == 1
    assert result.controls == ()
    assert [
        (item.code, item.count) for item in result.control_primary_rejection_counts
    ] == [(RejectionCode.POLICY_CONTROL, 1)]


def test_preference_adapter_rejects_legacy_prompt_block() -> None:
    batch = preference_batch(preference_row())
    batch["prompt_block"] = "do not accept this"
    with pytest.raises(GovernedLaneAdapterError, match="forbidden legacy fields"):
        asyncio.run(
            preference_adapter(batch).select(
                request(
                    MemoryLane.PREFERENCE,
                    PREFERENCE_SOURCE,
                    memory_intent="recommendation",
                    domains=("music",),
                ),
                lane_limit(MemoryLane.PREFERENCE),
            )
        )


def test_preference_snapshot_cannot_exceed_governed_scan_limit() -> None:
    rows = tuple(preference_row(3) for _ in range(201))
    with pytest.raises(GovernedLaneAdapterError, match="governed scan limit"):
        asyncio.run(
            preference_adapter(preference_batch(*rows)).select(
                request(
                    MemoryLane.PREFERENCE,
                    PREFERENCE_SOURCE,
                    memory_intent="recommendation",
                    domains=("music",),
                ),
                lane_limit(MemoryLane.PREFERENCE),
            )
        )


def test_source_pin_mismatch_is_closed_rejection_not_fallback() -> None:
    wrong_source = SourceContractVersionV1(
        name="claim_projection", version="memory_projection_v4"
    )
    result = asyncio.run(
        claim_adapter((enriched_claim_row(uid(1)),)).select(
            request(
                MemoryLane.CLAIM,
                wrong_source,
                memory_intent="specific_recall",
            ),
            lane_limit(MemoryLane.CLAIM),
        )
    )
    assert result.records == ()
    assert [(item.code, item.count) for item in result.primary_rejection_counts] == [
        (RejectionCode.SOURCE_VERSION, 1)
    ]


def test_input_rows_are_not_mutated() -> None:
    row = enriched_project_row()
    before = deepcopy(row)
    asyncio.run(
        project_adapter((row,)).select(
            request(
                MemoryLane.PROJECT_KNOWLEDGE,
                PROJECT_SOURCE,
                memory_intent="project_status",
            ),
            lane_limit(MemoryLane.PROJECT_KNOWLEDGE),
        )
    )
    assert row == before
