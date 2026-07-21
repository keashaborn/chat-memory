from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import UUID

from pydantic import ValidationError

from rag_engine.memory_v1_selection_envelope import (
    ANSWER_BINDING_VERSION,
    BUDGET_POLICY_VERSION,
    CONTRACT_VERSION,
    HARD_MAX_CONTROLS,
    PROMPT_ASSEMBLY_CONTEXT_VERSION,
    PROMPT_ASSEMBLY_INPUT_VERSION,
    REQUEST_VERSION,
    SELECTOR_VERSION,
    TOKEN_ESTIMATOR_VERSION,
    AuthoritativeGovernedMemorySelectorV1,
    ClaimSelectionV1,
    EpistemicStatus,
    EvidenceByStanceV1,
    FinalAnswerMemoryBindingV1,
    InjectedMemoryRecordV1,
    LaneOutcomeCode,
    LifePreferencePolarity,
    LifePreferenceSelectionV1,
    LifePreferenceStability,
    MemoryControlRefV1,
    MemoryLane,
    MemoryLaneLimitV1,
    MemoryLaneSelectionResultV1,
    MemoryPromptAssemblyContextV1,
    MemoryPromptAssemblyInputV1,
    MemoryRecordRefV1,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionContractError,
    MemorySelectionEnvelopeV1,
    MemorySelectionRequestBindingV1,
    MemorySelectionRequestV1,
    ProjectAuthorityLevel,
    ProjectDocumentState,
    ProjectKnowledgeKind,
    ProjectKnowledgeSelectionV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    ReasonCountV1,
    RejectionCode,
    ResponseControlAction,
    ResponsePreferenceControlV1,
    SelectionDirective,
    SelectionOutcomeCode,
    SelectionStatus,
    Sensitivity,
    SourceContractVersionV1,
    SurfacePolicy,
    UseInstruction,
    estimate_memory_record_tokens_v1,
    select_governed_memory_v1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_OWNER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
TRACE = UUID("10000000-0000-4000-8000-000000000001")
ANSWER = UUID("20000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
VECTOR = (0.125, -0.25, 0.5)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest(character: str) -> str:
    return character * 64


def uid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def source(name: str, version: str) -> SourceContractVersionV1:
    return SourceContractVersionV1(name=name, version=version)


CLAIM_SOURCE = source("claim_projection", "memory_projection_v5")
PREFERENCE_SOURCE = source(
    "preference_projection", "memory_preference_projection_v1"
)
PROJECT_SOURCE = source("project_projection", "memory_project_projection_v5")
SOURCE_PINS = (
    CLAIM_SOURCE,
    source("predicate_registry", "memory_predicate_registry_v5_1"),
    PREFERENCE_SOURCE,
    PROJECT_SOURCE,
)


def embedding(
    vector: Sequence[float] = VECTOR,
    *,
    source_value: QueryEmbeddingSource = QueryEmbeddingSource.PRIVATE_LOCAL,
) -> QueryEmbeddingArtifactV1:
    return QueryEmbeddingArtifactV1.from_vector(
        source=source_value,
        model_version="bge-m3@sha256:test",
        vector=vector,
    )


def budget(
    *,
    max_records: int = 8,
    max_tokens: int = 600,
    max_controls: int = 8,
    claim_records: int = 4,
    claim_tokens: int = 500,
    preference_tokens: int = 160,
    project_tokens: int = 600,
) -> MemorySelectionBudgetPolicyV1:
    return MemorySelectionBudgetPolicyV1(
        policy_version=BUDGET_POLICY_VERSION,
        token_estimator_version=TOKEN_ESTIMATOR_VERSION,
        max_records=max_records,
        max_tokens=max_tokens,
        max_controls=max_controls,
        lane_limits=(
            MemoryLaneLimitV1(
                lane=MemoryLane.CLAIM,
                max_records=claim_records,
                max_tokens=claim_tokens,
            ),
            MemoryLaneLimitV1(
                lane=MemoryLane.PREFERENCE,
                max_records=3,
                max_tokens=preference_tokens,
            ),
            MemoryLaneLimitV1(
                lane=MemoryLane.PROJECT_KNOWLEDGE,
                max_records=4,
                max_tokens=project_tokens,
            ),
        ),
    )


def claim(
    rank: int = 1,
    *,
    owner: UUID = OWNER,
    record_number: int = 1,
    token_estimate: int = 1,
    source_contract: SourceContractVersionV1 = CLAIM_SOURCE,
    sensitivity: Sensitivity = Sensitivity.MEDIUM,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    superseded_by: UUID | None = None,
    surface_policy: SurfacePolicy = SurfacePolicy.DIRECT_OR_RELEVANT,
    project_key: str | None = None,
    component_key: str | None = None,
    text: str = "Dahlia was Eric's dog.",
) -> ClaimSelectionV1:
    evidence = uid(100 + record_number)
    return ClaimSelectionV1(
        owner_user_id=owner,
        lane=MemoryLane.CLAIM,
        record_id=uid(record_number),
        revision_id=uid(50 + record_number),
        source_contract=source_contract,
        source_content_sha256=digest("a"),
        rank=rank,
        surface_policy=surface_policy,
        sensitivity=sensitivity,
        use_instruction={
            SurfacePolicy.DIRECT_OR_RELEVANT: (
                UseInstruction.ANSWER_DIRECTLY_ONLY_WHEN_RELEVANT
            ),
            SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY: (
                UseInstruction.USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE
            ),
            SurfacePolicy.EXPLICIT_RECALL_ONLY: (
                UseInstruction.USE_ONLY_FOR_EXPLICIT_RECALL
            ),
            SurfacePolicy.RESTRICTED_EXPLICIT_RECALL_ONLY: (
                UseInstruction.USE_ONLY_FOR_RESTRICTED_EXPLICIT_RECALL
            ),
        }[surface_policy],
        token_estimate=token_estimate,
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_by=superseded_by,
        evidence_refs=(evidence,),
        observation_refs=(uid(200 + record_number),),
        text=text,
        predicate="relationship.has_pet",
        epistemic_status=EpistemicStatus.SUPPORTED,
        evidence_by_stance=EvidenceByStanceV1(supports=(evidence,)),
        project_key=project_key,
        component_key=component_key,
    )


def preference(
    rank: int = 1,
    *,
    owner: UUID = OWNER,
    token_estimate: int = 1,
) -> LifePreferenceSelectionV1:
    return LifePreferenceSelectionV1(
        owner_user_id=owner,
        lane=MemoryLane.PREFERENCE,
        record_id=uid(3),
        revision_id=uid(53),
        source_contract=PREFERENCE_SOURCE,
        source_content_sha256=digest("b"),
        rank=rank,
        surface_policy=SurfacePolicy.MENTION_WHEN_RELEVANT,
        sensitivity=Sensitivity.LOW,
        use_instruction=UseInstruction.USE_ONLY_WHEN_RELEVANT,
        token_estimate=token_estimate,
        valid_from=None,
        valid_to=None,
        superseded_by=None,
        evidence_refs=(uid(103),),
        observation_refs=(),
        preference_key="music:classical",
        preference_class="life",
        preference_domain="music",
        canonical_value_json='{"likes":true}',
        polarity=LifePreferencePolarity.LIKES,
        stability=LifePreferenceStability.STABLE,
    )


def project(
    rank: int = 1,
    *,
    owner: UUID = OWNER,
    component_id: UUID | None = uid(302),
    component_key: str | None = "memory-v1",
    document_state: ProjectDocumentState = ProjectDocumentState.RATIFIED,
    token_estimate: int = 1,
) -> ProjectKnowledgeSelectionV1:
    return ProjectKnowledgeSelectionV1(
        owner_user_id=owner,
        lane=MemoryLane.PROJECT_KNOWLEDGE,
        record_id=uid(5),
        revision_id=uid(55),
        source_contract=PROJECT_SOURCE,
        source_content_sha256=digest("c"),
        rank=rank,
        surface_policy=SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY,
        sensitivity=Sensitivity.MEDIUM,
        use_instruction=UseInstruction.USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE,
        token_estimate=token_estimate,
        valid_from=None,
        valid_to=None,
        superseded_by=None,
        evidence_refs=(uid(105),),
        observation_refs=(uid(205),),
        text="Memory V1 uses Postgres as its authority.",
        project_id=uid(301),
        project_key="verbal-sage",
        component_id=component_id,
        component_key=component_key,
        knowledge_key="architecture:postgres_authority",
        knowledge_kind=ProjectKnowledgeKind.CURRENT_STATE,
        document_state=document_state,
        authority_level=ProjectAuthorityLevel.APPROVED_SPEC,
    )


def control(
    number: int = 7,
    *,
    owner: UUID = OWNER,
    source_contract: SourceContractVersionV1 = PREFERENCE_SOURCE,
    sensitivity: Sensitivity = Sensitivity.LOW,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    superseded_by: UUID | None = None,
    preference_key: str | None = None,
) -> ResponsePreferenceControlV1:
    return ResponsePreferenceControlV1(
        owner_user_id=owner,
        record_id=uid(number),
        revision_id=uid(500 + number),
        source_contract=source_contract,
        source_content_sha256=f"{number % 10}" * 64,
        preference_key=(
            preference_key or f"response:direct_relevance:{number:02d}"
        ),
        action=ResponseControlAction.REQUIRE_DIRECT_RELEVANCE,
        scope_sha256=digest("e"),
        surface_policy=SurfacePolicy.ZERO_TOKEN_CONTROL_ONLY,
        sensitivity=sensitivity,
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_by=superseded_by,
        evidence_refs=(uid(600 + number),),
        content_tokens=0,
    )


def request(
    *,
    owner: UUID = OWNER,
    actor: UUID = OWNER,
    requested_lanes: tuple[MemoryLane, ...] = tuple(MemoryLane),
    directive: SelectionDirective = SelectionDirective.EVALUATE,
    vector: tuple[float, ...] = VECTOR,
    embedding_artifact: QueryEmbeddingArtifactV1 | None = None,
    budget_policy: MemorySelectionBudgetPolicyV1 | None = None,
    source_pins: tuple[SourceContractVersionV1, ...] = SOURCE_PINS,
    trace: UUID = TRACE,
    selected_at: datetime = NOW,
    explicit_recall: bool = True,
    project_key: str | None = "verbal-sage",
    component_key: str | None = "memory-v1",
    max_sensitivity: Sensitivity = Sensitivity.HIGH,
    memory_intent: str = "specific_recall",
    domains: tuple[str, ...] = ("pets", "project"),
    query_text: str = "What do you remember about Dahlia and this project?",
) -> MemorySelectionRequestV1:
    if directive == SelectionDirective.SUPPRESS:
        requested_lanes = ()
        vector = ()
        embedding_artifact = QueryEmbeddingArtifactV1.not_used()
        project_key = None
        component_key = None
    if embedding_artifact is None:
        embedding_artifact = (
            embedding(vector)
            if vector
            else QueryEmbeddingArtifactV1.not_used()
        )
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_v7",
        source_contract_versions=source_pins,
        selection_trace_id=trace,
        authenticated_actor_user_id=actor,
        owner_user_id=owner,
        request_id="request-123",
        thread_id=THREAD,
        query_text=query_text,
        query_vector=vector,
        query_embedding=embedding_artifact,
        memory_intent=memory_intent,
        domains=domains,
        requested_lanes=requested_lanes,
        selection_directive=directive,
        explicit_recall=explicit_recall,
        project_key=project_key,
        component_key=component_key,
        max_sensitivity=max_sensitivity,
        selected_at=selected_at,
        budget_policy=budget_policy or budget(),
    )


def reasons(**values: int) -> tuple[ReasonCountV1, ...]:
    return tuple(
        ReasonCountV1(code=RejectionCode(key), count=count)
        for key, count in sorted(values.items())
        if count
    )


def lane_result(
    lane: MemoryLane,
    *,
    owner: UUID = OWNER,
    records: tuple[Any, ...] | None = None,
    controls: tuple[ResponsePreferenceControlV1, ...] | None = None,
    candidate_count: int | None = None,
    visible_count: int | None = None,
    eligible_count: int | None = None,
    primary: tuple[ReasonCountV1, ...] | None = None,
    reason_values: tuple[ReasonCountV1, ...] = (),
    control_candidate_count: int | None = None,
    control_primary: tuple[ReasonCountV1, ...] | None = None,
    control_reason_values: tuple[ReasonCountV1, ...] = (),
    candidate_marker: str | None = None,
) -> MemoryLaneSelectionResultV1:
    if records is None:
        if lane == MemoryLane.CLAIM:
            records = (claim(owner=owner),)
        elif lane == MemoryLane.PREFERENCE:
            records = (preference(owner=owner),)
        else:
            records = (project(owner=owner),)
    if controls is None:
        controls = (control(owner=owner),) if lane == MemoryLane.PREFERENCE else ()
    if candidate_count is None:
        candidate_count = len(records) + (1 if lane == MemoryLane.CLAIM else 0)
    if visible_count is None:
        visible_count = candidate_count
    if eligible_count is None:
        eligible_count = len(records)
    if primary is None:
        rejected = candidate_count - len(records)
        primary = reasons(semantic_relevance=rejected) if rejected else ()
    if not reason_values:
        reason_values = primary
    if control_candidate_count is None:
        control_candidate_count = len(controls)
    if control_primary is None:
        control_rejected = control_candidate_count - len(controls)
        control_primary = (
            reasons(policy_control=control_rejected) if control_rejected else ()
        )
    if records:
        outcome = LaneOutcomeCode.SELECTED
    elif controls:
        outcome = LaneOutcomeCode.CONTROLS_ONLY
    elif visible_count == 0 and control_candidate_count == 0:
        outcome = LaneOutcomeCode.NO_VISIBLE_MEMORY
    else:
        outcome = LaneOutcomeCode.NO_SELECTION
    return MemoryLaneSelectionResultV1(
        owner_user_id=owner,
        lane=lane,
        records=records,
        controls=controls,
        candidate_count=candidate_count,
        visible_candidate_count=visible_count,
        eligible_count=eligible_count,
        control_candidate_count=control_candidate_count,
        candidate_set_sha256=digest(
            candidate_marker or str(1 + list(MemoryLane).index(lane))
        ),
        primary_rejection_counts=primary,
        reason_counts=reason_values,
        control_primary_rejection_counts=control_primary,
        control_reason_counts=control_reason_values or control_primary,
        outcome_code=outcome,
        owner_scope_verified=True,
        postgres_revalidated=True,
        qdrant_role=(
            "candidate_ids_only" if lane == MemoryLane.CLAIM else "not_used"
        ),
        database_writes=0,
        qdrant_writes=0,
        external_model_calls=0,
    )


class FakeProvider:
    def __init__(self, result: MemoryLaneSelectionResultV1) -> None:
        self.result = result
        self.calls = 0

    async def select(
        self,
        request_value: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1:
        self.calls += 1
        self.last_request = request_value
        self.last_limit = lane_limit
        return self.result


def selector(
    replacements: dict[MemoryLane, MemoryLaneSelectionResultV1] | None = None,
) -> AuthoritativeGovernedMemorySelectorV1:
    replacements = replacements or {}
    return AuthoritativeGovernedMemorySelectorV1(
        {
            lane: FakeProvider(replacements.get(lane, lane_result(lane)))
            for lane in MemoryLane
        }
    )


def single_lane_envelope(
    lane: MemoryLane,
    result: MemoryLaneSelectionResultV1,
    **request_values: Any,
) -> MemorySelectionEnvelopeV1:
    request_values.setdefault("requested_lanes", (lane,))
    if lane != MemoryLane.CLAIM:
        request_values.setdefault("vector", ())
    return asyncio.run(
        select_governed_memory_v1(
            AuthoritativeGovernedMemorySelectorV1(
                {lane: FakeProvider(result)}
            ),
            request(**request_values),
        )
    )


def reason_map(values: Sequence[ReasonCountV1]) -> dict[str, int]:
    return {item.code.value: item.count for item in values}


class MemorySelectionEnvelopeV1Tests(unittest.TestCase):
    def test_all_lanes_return_one_typed_deterministic_envelope(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        self.assertEqual(envelope.status, SelectionStatus.OK)
        self.assertEqual(envelope.outcome_code, SelectionOutcomeCode.SELECTED)
        self.assertEqual([item.lane for item in envelope.records], list(MemoryLane))
        self.assertEqual([item.rank for item in envelope.records], [1, 2, 3])
        expected_tokens = sum(
            estimate_memory_record_tokens_v1(item) for item in envelope.records
        )
        self.assertEqual(envelope.budget.used_tokens, expected_tokens)
        self.assertEqual(envelope.observability.candidate_count, 4)
        self.assertEqual(envelope.observability.selected_count, 3)
        self.assertEqual(envelope.observability.rejected_candidate_count, 1)
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"semantic_relevance": 1},
        )
        self.assertEqual(envelope.observability.selected_control_count, 1)
        parsed = MemorySelectionEnvelopeV1.from_wire_json(
            envelope.canonical_json_bytes()
        )
        self.assertEqual(parsed, envelope)
        replay = asyncio.run(select_governed_memory_v1(selector(), request()))
        self.assertEqual(replay.canonical_json_bytes(), envelope.canonical_json_bytes())

    def test_selector_revalidates_forged_request_before_provider_call(self) -> None:
        providers = {lane: FakeProvider(lane_result(lane)) for lane in MemoryLane}
        forged = request().model_copy(
            update={"authenticated_actor_user_id": OTHER_OWNER}
        )
        with self.assertRaisesRegex(MemorySelectionContractError, "invalid.*request"):
            asyncio.run(
                select_governed_memory_v1(
                    AuthoritativeGovernedMemorySelectorV1(providers), forged
                )
            )
        self.assertTrue(all(provider.calls == 0 for provider in providers.values()))

    def test_selector_revalidates_forged_provider_result(self) -> None:
        forged = lane_result(MemoryLane.CLAIM).model_copy(
            update={"owner_scope_verified": False}
        )
        with self.assertRaisesRegex(MemorySelectionContractError, "invalid claim"):
            single_lane_envelope(MemoryLane.CLAIM, forged)

    def test_wire_rejects_nested_cross_owner_even_with_new_manifest(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        value = envelope.model_dump(mode="json")
        value["records"][0]["owner_user_id"] = str(OTHER_OWNER)
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "cross-owner record"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

        value = envelope.model_dump(mode="json")
        value["controls"][0]["owner_user_id"] = str(OTHER_OWNER)
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "cross-owner control"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

    def test_wire_reapplies_policy_and_budget_with_new_manifest(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        value = envelope.model_dump(mode="json")
        value["records"][0]["sensitivity"] = "restricted"
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "violates selected policy"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

        value = envelope.model_dump(mode="json")
        value["budget"]["max_tokens"] -= 1
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "budget differs"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

    def test_wire_lane_outcomes_must_exactly_match_selection(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        value = envelope.model_dump(mode="json")
        value["observability"]["lane_outcomes"][0]["outcome"] = "no_selection"
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "selected lane"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

        value = envelope.model_dump(mode="json")
        value["observability"]["lane_outcomes"] = value["observability"][
            "lane_outcomes"
        ][:1]
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaisesRegex(ValidationError, "requested lanes"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

    def test_request_binding_changes_for_selection_inputs(self) -> None:
        base = MemorySelectionRequestBindingV1.from_request(request())
        variants = (
            request(query_text="A different query"),
            request(memory_intent="recommendation"),
            request(domains=("project",)),
            request(requested_lanes=(MemoryLane.CLAIM,)),
            request(explicit_recall=False),
            request(component_key=None),
            request(max_sensitivity=Sensitivity.MEDIUM),
            request(selected_at=NOW + timedelta(seconds=1)),
            request(trace=uid(999)),
            request(
                source_pins=(
                    source("claim_projection", "memory_projection_v5_1"),
                    *SOURCE_PINS[1:],
                )
            ),
            request(budget_policy=budget(max_records=7)),
            request(
                embedding_artifact=embedding(
                    VECTOR, source_value=QueryEmbeddingSource.EXTERNAL
                )
            ),
        )
        hashes = {
            MemorySelectionRequestBindingV1.from_request(item).request_binding_sha256
            for item in variants
        }
        self.assertEqual(len(hashes), len(variants))
        self.assertNotIn(base.request_binding_sha256, hashes)

    def test_sensitive_request_wire_round_trips_explicitly(self) -> None:
        original = request()
        wire = original.selector_wire_json_bytes()
        encoded = wire.decode("utf-8")
        self.assertIn('"query_text"', encoded)
        self.assertIn('"query_vector"', encoded)
        self.assertEqual(
            MemorySelectionRequestV1.from_selector_wire_json(wire), original
        )
        self.assertNotIn("query_text", original.model_dump(mode="json"))

    def test_source_versions_and_evidence_stances_are_closed(self) -> None:
        with self.assertRaises(ValidationError):
            SourceContractVersionV1(name="claim_projection", version=" ")
        evidence = uid(777)
        with self.assertRaisesRegex(ValidationError, "multiple stances"):
            EvidenceByStanceV1(
                supports=(evidence,),
                opposes=(evidence,),
            )

    def test_embedding_artifact_is_frozen_and_self_consistent(self) -> None:
        with self.assertRaises(ValidationError):
            QueryEmbeddingArtifactV1(
                artifact_version="memory_query_embedding_artifact_v1",
                source=QueryEmbeddingSource.EXTERNAL,
                model_version="model",
                dimension=3,
                vector_sha256=sha(list(VECTOR)),
                external_call_count=0,
            )
        with self.assertRaises(ValidationError):
            request(
                embedding_artifact=embedding((1.0, 2.0)),
                vector=VECTOR,
            )
        with self.assertRaisesRegex(ValidationError, "requires a frozen"):
            request(vector=(), embedding_artifact=QueryEmbeddingArtifactV1.not_used())
        envelope = asyncio.run(
            select_governed_memory_v1(
                selector(),
                request(
                    embedding_artifact=embedding(
                        VECTOR, source_value=QueryEmbeddingSource.EXTERNAL
                    )
                ),
            )
        )
        self.assertEqual(
            envelope.observability.upstream_external_embedding_calls, 1
        )
        encoded = envelope.canonical_json_bytes().decode("utf-8")
        self.assertNotIn("0.125", encoded)

    def test_suppression_calls_no_provider_and_is_not_an_error(self) -> None:
        providers = {lane: FakeProvider(lane_result(lane)) for lane in MemoryLane}
        envelope = asyncio.run(
            select_governed_memory_v1(
                AuthoritativeGovernedMemorySelectorV1(providers),
                request(directive=SelectionDirective.SUPPRESS),
            )
        )
        self.assertEqual(envelope.status, SelectionStatus.SUPPRESSED)
        self.assertEqual(
            envelope.outcome_code,
            SelectionOutcomeCode.SUPPRESSED_BY_MEMORY_POLICY,
        )
        self.assertEqual(envelope.records, ())
        self.assertEqual(
            envelope.observability.postgres_revalidation, "not_needed"
        )
        self.assertTrue(all(provider.calls == 0 for provider in providers.values()))
        with self.assertRaises(ValidationError):
            request(requested_lanes=())
        raw = request().model_dump(mode="json")
        raw["query_text"] = "ordinary turn"
        raw["query_vector"] = list(VECTOR)
        raw["selection_directive"] = "suppress"
        raw["requested_lanes"] = []
        with self.assertRaisesRegex(ValidationError, "unused query embedding"):
            MemorySelectionRequestV1.from_selector_wire_json(json.dumps(raw))

    def test_facade_reapplies_source_sensitivity_and_temporal_policy(self) -> None:
        cases = (
            (
                claim(
                    source_contract=source(
                        "claim_projection", "unapproved_projection"
                    )
                ),
                RejectionCode.SOURCE_VERSION,
                {},
            ),
            (
                claim(sensitivity=Sensitivity.RESTRICTED),
                RejectionCode.SENSITIVITY,
                {"max_sensitivity": Sensitivity.HIGH},
            ),
            (
                claim(valid_from=NOW + timedelta(seconds=1)),
                RejectionCode.NOT_YET_VALID,
                {},
            ),
            (
                claim(valid_to=NOW),
                RejectionCode.EXPIRED,
                {},
            ),
            (
                claim(superseded_by=uid(999)),
                RejectionCode.SUPERSEDED,
                {},
            ),
        )
        for record, rejection, request_values in cases:
            with self.subTest(rejection=rejection.value):
                envelope = single_lane_envelope(
                    MemoryLane.CLAIM,
                    lane_result(
                        MemoryLane.CLAIM,
                        records=(record,),
                        candidate_count=1,
                        primary=(),
                    ),
                    **request_values,
                )
                self.assertEqual(envelope.records, ())
                self.assertEqual(
                    reason_map(envelope.observability.primary_rejection_counts),
                    {rejection.value: 1},
                )

        accepted = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(claim(valid_from=NOW, sensitivity=Sensitivity.HIGH),),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(len(accepted.records), 1)
        self.assertEqual(
            accepted.observability.postgres_revalidation, "performed"
        )

    def test_explicit_recall_surface_policy_is_reapplied(self) -> None:
        explicit_only = claim(
            surface_policy=SurfacePolicy.EXPLICIT_RECALL_ONLY
        )
        envelope = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(explicit_only,),
                candidate_count=1,
                primary=(),
            ),
            explicit_recall=False,
        )
        self.assertEqual(envelope.records, ())
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"memory_intent": 1},
        )

    def test_exact_project_scope_and_superseded_project_are_rejected(self) -> None:
        mismatched = project(component_id=uid(999), component_key="other-component")
        envelope = single_lane_envelope(
            MemoryLane.PROJECT_KNOWLEDGE,
            lane_result(
                MemoryLane.PROJECT_KNOWLEDGE,
                records=(mismatched,),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"project_scope": 1},
        )

    def test_root_project_scope_is_selected_only_by_root_request(self) -> None:
        root = project(component_id=None, component_key=None)
        result = lane_result(
            MemoryLane.PROJECT_KNOWLEDGE,
            records=(root,),
            candidate_count=1,
            primary=(),
        )
        selected = single_lane_envelope(
            MemoryLane.PROJECT_KNOWLEDGE,
            result,
            component_key=None,
        )
        self.assertEqual(len(selected.records), 1)
        rejected = single_lane_envelope(
            MemoryLane.PROJECT_KNOWLEDGE,
            result,
            component_key="memory-v1",
        )
        self.assertEqual(rejected.records, ())
        self.assertEqual(
            reason_map(rejected.observability.primary_rejection_counts),
            {"project_scope": 1},
        )
        superseded = project(document_state=ProjectDocumentState.SUPERSEDED)
        envelope = single_lane_envelope(
            MemoryLane.PROJECT_KNOWLEDGE,
            lane_result(
                MemoryLane.PROJECT_KNOWLEDGE,
                records=(superseded,),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"superseded": 1},
        )

        scoped_claim = claim(
            surface_policy=SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY,
            project_key="verbal-sage",
            component_key="different-component",
        )
        envelope = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(scoped_claim,),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"project_scope": 1},
        )

    def test_v5_enums_and_root_project_scope_are_compatible(self) -> None:
        root = project(component_id=None, component_key=None)
        self.assertIsNone(root.component_id)
        self.assertIsNone(root.component_key)
        with self.assertRaises(ValidationError):
            project(component_id=None, component_key="memory-v1")
        old_preference = preference().model_dump(mode="json")
        old_preference["polarity"] = "positive"
        with self.assertRaises(ValidationError):
            LifePreferenceSelectionV1.model_validate_json(
                json.dumps(old_preference)
            )
        old_project = project().model_dump(mode="json")
        old_project["document_state"] = "current"
        with self.assertRaises(ValidationError):
            ProjectKnowledgeSelectionV1.model_validate_json(json.dumps(old_project))

    def test_selector_recomputes_tokens_before_all_budgets(self) -> None:
        low = claim(token_estimate=1)
        high = claim(token_estimate=1200)
        first = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(low,),
                candidate_count=1,
                primary=(),
            ),
        )
        second = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(high,),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(first.records[0].token_estimate, second.records[0].token_estimate)
        self.assertEqual(first.envelope_sha256, second.envelope_sha256)

        blocked = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(low,),
                candidate_count=1,
                primary=(),
            ),
            budget_policy=budget(max_tokens=1, claim_tokens=500),
        )
        self.assertEqual(blocked.records, ())
        self.assertEqual(
            reason_map(blocked.observability.primary_rejection_counts),
            {"global_token_budget": 1},
        )

    def test_lane_record_lane_token_and_global_record_budgets(self) -> None:
        two = (
            claim(rank=1, record_number=1),
            claim(rank=2, record_number=2),
        )
        result = lane_result(
            MemoryLane.CLAIM,
            records=two,
            candidate_count=2,
            eligible_count=2,
            primary=(),
        )
        lane_record = single_lane_envelope(
            MemoryLane.CLAIM,
            result,
            budget_policy=budget(claim_records=1),
        )
        self.assertEqual(
            reason_map(lane_record.observability.primary_rejection_counts),
            {"lane_record_budget": 1},
        )
        lane_token = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(two[0],),
                candidate_count=1,
                primary=(),
            ),
            budget_policy=budget(claim_tokens=1),
        )
        self.assertEqual(
            reason_map(lane_token.observability.primary_rejection_counts),
            {"lane_token_budget": 1},
        )
        global_record = single_lane_envelope(
            MemoryLane.CLAIM,
            result,
            budget_policy=budget(max_records=1),
        )
        self.assertEqual(
            reason_map(global_record.observability.primary_rejection_counts),
            {"global_record_budget": 1},
        )

    def test_lane_contract_names_cannot_cross_and_controls_reapply_policy(self) -> None:
        wrong_claim = claim(source_contract=PREFERENCE_SOURCE)
        envelope = single_lane_envelope(
            MemoryLane.CLAIM,
            lane_result(
                MemoryLane.CLAIM,
                records=(wrong_claim,),
                candidate_count=1,
                primary=(),
            ),
        )
        self.assertEqual(
            reason_map(envelope.observability.primary_rejection_counts),
            {"invalid_source_contract": 1},
        )

        control_cases = (
            control(20, source_contract=PROJECT_SOURCE),
            control(21, sensitivity=Sensitivity.RESTRICTED),
            control(22, valid_to=NOW),
            control(23, superseded_by=uid(999)),
        )
        expected = (
            "invalid_source_contract",
            "sensitivity",
            "expired",
            "superseded",
        )
        for item, reason in zip(control_cases, expected):
            with self.subTest(reason=reason):
                result = lane_result(
                    MemoryLane.PREFERENCE,
                    records=(),
                    controls=(item,),
                    candidate_count=0,
                    visible_count=0,
                    eligible_count=0,
                    primary=(),
                    control_candidate_count=1,
                    control_primary=(),
                )
                envelope = single_lane_envelope(
                    MemoryLane.PREFERENCE,
                    result,
                    max_sensitivity=Sensitivity.HIGH,
                )
                self.assertEqual(envelope.controls, ())
                self.assertEqual(
                    reason_map(
                        envelope.observability.control_primary_rejection_counts
                    ),
                    {reason: 1},
                )

    def test_surface_use_and_epistemic_instructions_cannot_conflict(self) -> None:
        value = claim().model_dump(mode="json")
        value["surface_policy"] = "exact_project_scope_only"
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            ClaimSelectionV1.model_validate_json(json.dumps(value))
        value = claim().model_dump(mode="json")
        value["epistemic_status"] = "disputed"
        with self.assertRaisesRegex(ValidationError, "uncertainty instruction"):
            ClaimSelectionV1.model_validate_json(json.dumps(value))

    def test_multiple_revisions_of_one_logical_head_are_rejected(self) -> None:
        first = claim(rank=1)
        second_raw = first.model_dump(mode="json")
        second_raw["revision_id"] = str(uid(998))
        second_raw["rank"] = 2
        second = ClaimSelectionV1.model_validate_json(json.dumps(second_raw))
        with self.assertRaisesRegex(ValidationError, "multiple revisions"):
            lane_result(
                MemoryLane.CLAIM,
                records=(first, second),
                candidate_count=2,
                eligible_count=2,
                primary=(),
            )

    def test_controls_are_capped_sorted_and_deterministic(self) -> None:
        controls = tuple(control(number) for number in range(10, 19))
        result_a = lane_result(
            MemoryLane.PREFERENCE,
            records=(),
            controls=tuple(reversed(controls)),
            candidate_count=0,
            visible_count=0,
            eligible_count=0,
            primary=(),
            control_candidate_count=9,
            control_primary=(),
        )
        result_b = lane_result(
            MemoryLane.PREFERENCE,
            records=(),
            controls=controls,
            candidate_count=0,
            visible_count=0,
            eligible_count=0,
            primary=(),
            control_candidate_count=9,
            control_primary=(),
        )
        first = single_lane_envelope(MemoryLane.PREFERENCE, result_a)
        second = single_lane_envelope(MemoryLane.PREFERENCE, result_b)
        self.assertEqual(len(first.controls), HARD_MAX_CONTROLS)
        self.assertEqual(first.canonical_json_bytes(), second.canonical_json_bytes())
        self.assertEqual(
            reason_map(first.observability.control_primary_rejection_counts),
            {"global_control_budget": 1},
        )

    def test_observability_trace_is_closed_sanitized_and_detached(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        trace = envelope.sanitized_trace()
        encoded = json.dumps(trace, sort_keys=True)
        for forbidden in (
            "Dahlia was Eric",
            "What do you remember",
            "Memory V1 uses Postgres",
            "music:classical",
            str(OWNER),
            "prompt_block",
        ):
            self.assertNotIn(forbidden, encoded)
        trace["primary_rejection_counts"].append(
            {"code": "semantic_relevance", "count": 999}
        )
        fresh = envelope.sanitized_trace()
        self.assertNotEqual(trace, fresh)
        self.assertEqual(
            fresh["primary_rejection_counts"],
            [{"code": "semantic_relevance", "count": 1}],
        )
        with self.assertRaises(ValidationError):
            ReasonCountV1(code="user_secret_reason", count=1)

    def test_wire_is_strict_unknown_fields_and_versions_are_required(self) -> None:
        envelope = asyncio.run(select_governed_memory_v1(selector(), request()))
        value = envelope.model_dump(mode="json")
        value["vantage_id"] = "RESSE"
        with self.assertRaises(ValidationError):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))
        value.pop("vantage_id")
        value.pop("contract_version")
        with self.assertRaisesRegex(ValidationError, "Field required"):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))
        value = envelope.model_dump(mode="json")
        value["records"][0]["rank"] = "1"
        value["envelope_sha256"] = sha(
            {key: item for key, item in value.items() if key != "envelope_sha256"}
        )
        with self.assertRaises(ValidationError):
            MemorySelectionEnvelopeV1.from_wire_json(json.dumps(value))

    def test_canonical_preference_value_cannot_hide_mutable_state(self) -> None:
        item = preference()
        self.assertIsInstance(item.canonical_value_json, str)
        source_value = item.model_dump(mode="json")
        source_value["canonical_value_json"] = '{"b":1, "a":2}'
        with self.assertRaisesRegex(ValidationError, "canonical"):
            LifePreferenceSelectionV1.model_validate_json(json.dumps(source_value))


class PromptAssemblyBoundaryV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = asyncio.run(
            select_governed_memory_v1(selector(), request())
        )
        self.context = MemoryPromptAssemblyContextV1.from_envelope(
            envelope=self.envelope,
            authenticated_actor_user_id=OWNER,
            renderer_version="response_policy_renderer_v1",
        )

    def test_exact_typed_context_binds_the_envelope(self) -> None:
        item = MemoryPromptAssemblyInputV1.create(
            context=self.context,
            envelope=self.envelope,
        )
        self.assertEqual(item.contract_version, PROMPT_ASSEMBLY_INPUT_VERSION)
        self.assertEqual(
            item.context.contract_version, PROMPT_ASSEMBLY_CONTEXT_VERSION
        )

    def test_context_mismatches_fail_closed(self) -> None:
        mutations = (
            {"authenticated_actor_user_id": OTHER_OWNER},
            {"owner_user_id": OTHER_OWNER},
            {"selection_trace_id": uid(999)},
            {"request_id_sha256": digest("9")},
            {"query_sha256": digest("8")},
            {"request_binding_sha256": digest("7")},
            {"envelope_sha256": digest("6")},
            {"max_memory_prompt_tokens": self.envelope.budget.max_tokens + 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                forged = self.context.model_copy(update=mutation)
                with self.assertRaises(ValidationError):
                    MemoryPromptAssemblyInputV1.create(
                        context=forged,
                        envelope=self.envelope,
                    )


class FinalAnswerMemoryBindingV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = asyncio.run(
            select_governed_memory_v1(selector(), request())
        )
        self.refs = tuple(
            MemoryRecordRefV1.from_record(item) for item in self.envelope.records
        )
        self.context = MemoryPromptAssemblyContextV1.from_envelope(
            envelope=self.envelope,
            authenticated_actor_user_id=OWNER,
            renderer_version="response_policy_renderer_v1",
        )
        self.assembly_input = MemoryPromptAssemblyInputV1.create(
            context=self.context,
            envelope=self.envelope,
        )

    def test_binding_uses_renderer_actual_tokens_and_fragment_hash(self) -> None:
        injected = (
            InjectedMemoryRecordV1(
                record=self.refs[0],
                actual_prompt_tokens=17,
                rendered_fragment_sha256=digest("f"),
            ),
            InjectedMemoryRecordV1(
                record=self.refs[1],
                actual_prompt_tokens=9,
                rendered_fragment_sha256=digest("9"),
            ),
        )
        binding = FinalAnswerMemoryBindingV1.create(
            assembly_input=self.assembly_input,
            owner_user_id=OWNER,
            answer_id=ANSWER,
            injected=injected,
            answer_model_exposed=self.refs[:1],
            created_at=NOW,
        )
        self.assertEqual(binding.selected_count, 3)
        self.assertEqual(binding.injected_count, 2)
        self.assertEqual(binding.exposed_count, 1)
        self.assertEqual(binding.actual_prompt_memory_tokens, 26)
        self.assertEqual(binding.outcome, "exposed")
        self.assertEqual(
            binding.assembly_context_sha256, self.context.context_sha256
        )
        self.assertEqual(
            binding.assembly_input_sha256,
            self.assembly_input.assembly_input_sha256,
        )
        self.assertEqual(
            binding.selection_set_sha256,
            self.envelope.observability.selection_set_sha256,
        )
        parsed = FinalAnswerMemoryBindingV1.from_wire_json(
            binding.canonical_json_bytes()
        )
        self.assertEqual(parsed, binding)

    def test_binding_enforces_the_exact_assembly_token_cap(self) -> None:
        context = MemoryPromptAssemblyContextV1.from_envelope(
            envelope=self.envelope,
            authenticated_actor_user_id=OWNER,
            renderer_version="response_policy_renderer_v1",
            max_memory_prompt_tokens=10,
        )
        assembly_input = MemoryPromptAssemblyInputV1.create(
            context=context,
            envelope=self.envelope,
        )
        with self.assertRaisesRegex(MemorySelectionContractError, "assembly budget"):
            FinalAnswerMemoryBindingV1.create(
                assembly_input=assembly_input,
                owner_user_id=OWNER,
                answer_id=ANSWER,
                injected=(
                    InjectedMemoryRecordV1(
                        record=self.refs[0],
                        actual_prompt_tokens=11,
                        rendered_fragment_sha256=digest("f"),
                    ),
                ),
                created_at=NOW,
            )

    def test_binding_preserves_exact_envelope_control_order_and_hash(self) -> None:
        controls = (
            control(30, preference_key="response:z-last"),
            control(31, preference_key="response:a-first"),
        )
        result = lane_result(
            MemoryLane.PREFERENCE,
            records=(),
            controls=controls,
            candidate_count=0,
            visible_count=0,
            eligible_count=0,
            primary=(),
            control_candidate_count=2,
            control_primary=(),
        )
        envelope = single_lane_envelope(MemoryLane.PREFERENCE, result)
        self.assertEqual(envelope.controls[0].record_id, uid(31))
        context = MemoryPromptAssemblyContextV1.from_envelope(
            envelope=envelope,
            authenticated_actor_user_id=OWNER,
            renderer_version="response_policy_renderer_v1",
        )
        assembly_input = MemoryPromptAssemblyInputV1.create(
            context=context,
            envelope=envelope,
        )
        binding = FinalAnswerMemoryBindingV1.create(
            assembly_input=assembly_input,
            owner_user_id=OWNER,
            answer_id=ANSWER,
            created_at=NOW,
        )
        self.assertEqual(
            binding.selected_control_set_sha256,
            envelope.observability.control_set_sha256,
        )
        self.assertEqual(binding.control_items[0].control.record_id, uid(31))

    def test_binding_rejects_invalid_stage_subsets_and_owner(self) -> None:
        with self.assertRaisesRegex(MemorySelectionContractError, "exposed"):
            FinalAnswerMemoryBindingV1.create(
                assembly_input=self.assembly_input,
                owner_user_id=OWNER,
                answer_id=ANSWER,
                answer_model_exposed=self.refs[:1],
                created_at=NOW,
            )
        with self.assertRaisesRegex(MemorySelectionContractError, "owner"):
            FinalAnswerMemoryBindingV1.create(
                assembly_input=self.assembly_input,
                owner_user_id=OTHER_OWNER,
                answer_id=ANSWER,
                created_at=NOW,
            )
        unknown = self.refs[0].model_copy(update={"record_id": uid(999)})
        with self.assertRaisesRegex(MemorySelectionContractError, "injected"):
            FinalAnswerMemoryBindingV1.create(
                assembly_input=self.assembly_input,
                owner_user_id=OWNER,
                answer_id=ANSWER,
                injected=(
                    InjectedMemoryRecordV1(
                        record=unknown,
                        actual_prompt_tokens=1,
                        rendered_fragment_sha256=digest("f"),
                    ),
                ),
                created_at=NOW,
            )

    def test_v1_binding_has_no_unverifiable_attribution_or_semantic_control_key(self) -> None:
        schema = json.dumps(
            FinalAnswerMemoryBindingV1.model_json_schema(), sort_keys=True
        )
        self.assertNotIn("model_attributed", schema)
        self.assertNotIn("attribution_reported", schema)
        self.assertNotIn("preference_key", schema)

    def test_binding_outcome_is_semantically_derived(self) -> None:
        binding = FinalAnswerMemoryBindingV1.create(
            assembly_input=self.assembly_input,
            owner_user_id=OWNER,
            answer_id=ANSWER,
            created_at=NOW,
        )
        value = binding.model_dump(mode="json")
        value["outcome"] = "exposed"
        value["binding_manifest_sha256"] = sha(
            {
                key: item
                for key, item in value.items()
                if key != "binding_manifest_sha256"
            }
        )
        with self.assertRaisesRegex(ValidationError, "outcome"):
            FinalAnswerMemoryBindingV1.from_wire_json(json.dumps(value))

    def test_empty_envelope_still_binds_generated_answer(self) -> None:
        results = {
            lane: lane_result(
                lane,
                records=(),
                controls=(),
                candidate_count=0,
                visible_count=0,
                eligible_count=0,
                primary=(),
                control_candidate_count=0,
                control_primary=(),
            )
            for lane in MemoryLane
        }
        envelope = asyncio.run(
            select_governed_memory_v1(selector(results), request())
        )
        context = MemoryPromptAssemblyContextV1.from_envelope(
            envelope=envelope,
            authenticated_actor_user_id=OWNER,
            renderer_version="response_policy_renderer_v1",
        )
        assembly_input = MemoryPromptAssemblyInputV1.create(
            context=context,
            envelope=envelope,
        )
        binding = FinalAnswerMemoryBindingV1.create(
            assembly_input=assembly_input,
            owner_user_id=OWNER,
            answer_id=ANSWER,
            created_at=NOW,
        )
        self.assertEqual(binding.outcome, "no_memory_selected")
        self.assertEqual(binding.selected_count, 0)

    def test_binding_manifest_detects_tampering(self) -> None:
        binding = FinalAnswerMemoryBindingV1.create(
            assembly_input=self.assembly_input,
            owner_user_id=OWNER,
            answer_id=ANSWER,
            created_at=NOW,
        )
        value = binding.model_dump(mode="json")
        value["answer_id"] = str(uid(987))
        with self.assertRaisesRegex(ValidationError, "manifest hash mismatch"):
            FinalAnswerMemoryBindingV1.from_wire_json(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
