from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Mapping, Protocol, Sequence, TypeVar, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONTRACT_VERSION = "memory_selection_envelope_v1"
REQUEST_VERSION = "memory_selection_request_v1"
REQUEST_BINDING_VERSION = "memory_selection_request_binding_v1"
SELECTOR_VERSION = "memory_v1_authoritative_selector_v1"
BUDGET_POLICY_VERSION = "memory_selection_budget_v1"
TOKEN_ESTIMATOR_VERSION = "memory_prompt_token_estimator_v1"
REJECTION_REGISTRY_VERSION = "memory_selection_rejection_registry_v1"
EMBEDDING_ARTIFACT_VERSION = "memory_query_embedding_artifact_v1"
PROMPT_ASSEMBLY_CONTEXT_VERSION = "memory_prompt_assembly_context_v1"
PROMPT_ASSEMBLY_INPUT_VERSION = "memory_prompt_assembly_input_v1"
ANSWER_BINDING_VERSION = "final_answer_memory_binding_v1"

HARD_MAX_RECORDS = 8
HARD_MAX_TOKENS = 1200
HARD_MAX_CONTROLS = 8
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,239}$")
PROJECT_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,159}$")


class MemorySelectionContractError(RuntimeError):
    """Fail-closed error at a trusted Memory V1 integration boundary."""


class MemoryLane(str, Enum):
    CLAIM = "claim"
    PREFERENCE = "preference"
    PROJECT_KNOWLEDGE = "project_knowledge"


LANE_ORDER = {
    MemoryLane.CLAIM: 0,
    MemoryLane.PREFERENCE: 1,
    MemoryLane.PROJECT_KNOWLEDGE: 2,
}


class Sensitivity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    RESTRICTED = "restricted"


SENSITIVITY_RANK = {
    Sensitivity.LOW: 0,
    Sensitivity.MEDIUM: 1,
    Sensitivity.HIGH: 2,
    Sensitivity.RESTRICTED: 3,
}


class SelectionDirective(str, Enum):
    EVALUATE = "evaluate"
    SUPPRESS = "suppress"


class QueryEmbeddingSource(str, Enum):
    NOT_USED = "not_used"
    CACHED = "cached"
    PRIVATE_LOCAL = "private_local"
    EXTERNAL = "external"


class SurfacePolicy(str, Enum):
    DIRECT_OR_RELEVANT = "direct_or_relevant"
    RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL = (
        "relevant_recommendation_or_explicit_recall"
    )
    EXACT_PROJECT_SCOPE_ONLY = "exact_project_scope_only"
    MENTION_WHEN_DIRECTLY_RELEVANT = "mention_when_directly_relevant"
    EXPLICIT_PERSON_OR_RELATIONSHIP_CONTEXT_ONLY = (
        "explicit_person_or_relationship_context_only"
    )
    RESTRICTED_EXPLICIT_RECALL_ONLY = "restricted_explicit_recall_only"
    EXPLICIT_RECALL_ONLY = "explicit_recall_only"
    MENTION_WHEN_RELEVANT = "mention_when_relevant"
    ZERO_TOKEN_CONTROL_ONLY = "zero_token_control_only"
    NEVER_SURFACE_AS_CONTENT = "never_surface_as_content"
    NORMALIZATION_ONLY = "normalization_only"
    NEVER = "never"


CONTENT_SURFACE_POLICIES = frozenset(
    {
        SurfacePolicy.DIRECT_OR_RELEVANT,
        SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL,
        SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY,
        SurfacePolicy.MENTION_WHEN_DIRECTLY_RELEVANT,
        SurfacePolicy.EXPLICIT_PERSON_OR_RELATIONSHIP_CONTEXT_ONLY,
        SurfacePolicy.RESTRICTED_EXPLICIT_RECALL_ONLY,
        SurfacePolicy.EXPLICIT_RECALL_ONLY,
        SurfacePolicy.MENTION_WHEN_RELEVANT,
    }
)

CONTROL_SURFACE_POLICIES = frozenset(
    {
        SurfacePolicy.ZERO_TOKEN_CONTROL_ONLY,
        SurfacePolicy.NEVER_SURFACE_AS_CONTENT,
    }
)


class UseInstruction(str, Enum):
    ANSWER_DIRECTLY_ONLY_WHEN_RELEVANT = "answer_directly_only_when_relevant"
    STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE = (
        "state_uncertainty_and_material_counterevidence"
    )
    USE_ONLY_FOR_RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL = (
        "use_only_for_relevant_recommendation_or_explicit_recall"
    )
    USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE = "use_only_inside_exact_project_scope"
    USE_ONLY_WHEN_RELEVANT = "use_only_when_relevant"
    MENTION_ONLY_WHEN_DIRECTLY_RELEVANT = "mention_only_when_directly_relevant"
    USE_ONLY_FOR_EXPLICIT_PERSON_OR_RELATIONSHIP_CONTEXT = (
        "use_only_for_explicit_person_or_relationship_context"
    )
    USE_ONLY_FOR_RESTRICTED_EXPLICIT_RECALL = (
        "use_only_for_restricted_explicit_recall"
    )
    USE_ONLY_FOR_EXPLICIT_RECALL = "use_only_for_explicit_recall"


SURFACE_USE_COMPATIBILITY = {
    SurfacePolicy.DIRECT_OR_RELEVANT: frozenset(
        {
            UseInstruction.ANSWER_DIRECTLY_ONLY_WHEN_RELEVANT,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL: frozenset(
        {
            UseInstruction.USE_ONLY_FOR_RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY: frozenset(
        {
            UseInstruction.USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.MENTION_WHEN_DIRECTLY_RELEVANT: frozenset(
        {
            UseInstruction.MENTION_ONLY_WHEN_DIRECTLY_RELEVANT,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.EXPLICIT_PERSON_OR_RELATIONSHIP_CONTEXT_ONLY: frozenset(
        {
            UseInstruction.USE_ONLY_FOR_EXPLICIT_PERSON_OR_RELATIONSHIP_CONTEXT,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.RESTRICTED_EXPLICIT_RECALL_ONLY: frozenset(
        {
            UseInstruction.USE_ONLY_FOR_RESTRICTED_EXPLICIT_RECALL,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.EXPLICIT_RECALL_ONLY: frozenset(
        {
            UseInstruction.USE_ONLY_FOR_EXPLICIT_RECALL,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
    SurfacePolicy.MENTION_WHEN_RELEVANT: frozenset(
        {
            UseInstruction.USE_ONLY_WHEN_RELEVANT,
            UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE,
        }
    ),
}


LANE_SOURCE_CONTRACT_NAMES = {
    MemoryLane.CLAIM: frozenset({"claim_projection"}),
    MemoryLane.PREFERENCE: frozenset({"preference_projection"}),
    MemoryLane.PROJECT_KNOWLEDGE: frozenset({"project_projection"}),
}
CONTROL_SOURCE_CONTRACT_NAMES = frozenset({"preference_projection"})


class ResponseControlAction(str, Enum):
    REQUIRE_DIRECT_RELEVANCE = "require_direct_relevance"


class RejectionCode(str, Enum):
    NOT_VISIBLE = "not_visible"
    INVALID_SOURCE_CONTRACT = "invalid_source_contract"
    PROJECTION_NOT_AUTHORIZED = "projection_not_authorized"
    PROJECTION_NOT_APPLIED = "projection_not_applied"
    SEMANTIC_RELEVANCE = "semantic_relevance"
    EPISTEMIC_STATUS = "epistemic_status"
    NO_ACTIVE_EVIDENCE = "no_active_evidence"
    NO_SUPPORTING_EVIDENCE = "no_supporting_evidence"
    NO_OBSERVATION_PROVENANCE = "no_observation_provenance"
    SENSITIVITY = "sensitivity"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    PREDICATE_PERMISSION = "predicate_permission"
    ENTITY_SCOPE = "entity_scope"
    SURFACE_POLICY = "surface_policy"
    PROJECT_SCOPE = "project_scope"
    SOURCE_VERSION = "source_version"
    MEMORY_INTENT = "memory_intent"
    INACTIVE = "inactive"
    NO_DIRECT_RELEVANCE = "no_direct_relevance"
    POLICY_CONTROL = "policy_control"
    LANE_RECORD_BUDGET = "lane_record_budget"
    LANE_TOKEN_BUDGET = "lane_token_budget"
    GLOBAL_RECORD_BUDGET = "global_record_budget"
    GLOBAL_TOKEN_BUDGET = "global_token_budget"
    GLOBAL_CONTROL_BUDGET = "global_control_budget"


class LaneOutcomeCode(str, Enum):
    SELECTED = "selected"
    CONTROLS_ONLY = "controls_only"
    NO_VISIBLE_MEMORY = "no_visible_memory"
    NO_SELECTION = "no_selection"


class SelectionOutcomeCode(str, Enum):
    SELECTED = "selected"
    CONTROLS_ONLY = "controls_only"
    NO_VISIBLE_MEMORY = "no_visible_memory"
    NO_SELECTION = "no_selection"
    SUPPRESSED_BY_MEMORY_POLICY = "suppressed_by_memory_policy"


class SelectionStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    SUPPRESSED = "suppressed"


class EpistemicStatus(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    DISPUTED = "disputed"


class LifePreferencePolarity(str, Enum):
    LIKES = "likes"
    DISLIKES = "dislikes"
    PREFERS = "prefers"
    AVOIDS = "avoids"


class LifePreferenceStability(str, Enum):
    TENTATIVE = "tentative"
    CONTEXTUAL = "contextual"
    STABLE = "stable"


class ProjectKnowledgeKind(str, Enum):
    CONSTRAINT = "constraint"
    CURRENT_STATE = "current_state"
    PROPOSED_FEATURE = "proposed_feature"
    REQUIREMENT = "requirement"


class ProjectDocumentState(str, Enum):
    UNVERIFIED = "unverified"
    WORKING = "working"
    PROPOSED = "proposed"
    RATIFIED = "ratified"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"


class ProjectAuthorityLevel(str, Enum):
    USER_REPORTED = "user_reported"
    USER_RATIFIED = "user_ratified"
    APPROVED_SPEC = "approved_spec"
    SYSTEM_OBSERVED = "system_observed"
    EXTERNAL_REFERENCE = "external_reference"


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _validate_sha256(value: str, field: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _validate_utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be normalized to UTC")
    return value.astimezone(timezone.utc)


def _validate_sorted_unique_uuids(
    values: tuple[UUID, ...], field: str
) -> tuple[UUID, ...]:
    canonical = tuple(sorted(set(values), key=str))
    if values != canonical:
        raise ValueError(f"{field} must be sorted and unique")
    return values


TModel = TypeVar("TModel", bound=BaseModel)


def _wire_revalidate(model_type: type[TModel], value: TModel) -> TModel:
    """Reparse JSON so model_copy/model_construct cannot bypass validators."""

    return model_type.model_validate_json(_canonical_json_bytes(value))


class SourceContractVersionV1(StrictFrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,119}$")
    version: str = Field(pattern=VERSION_RE.pattern, max_length=120)


def _validate_source_versions(
    value: tuple[SourceContractVersionV1, ...],
) -> tuple[SourceContractVersionV1, ...]:
    if not 1 <= len(value) <= 24:
        raise ValueError("source_contract_versions must contain 1 to 24 entries")
    names = [item.name for item in value]
    if len(names) != len(set(names)):
        raise ValueError("source_contract_versions contains duplicate names")
    if names != sorted(names):
        raise ValueError("source_contract_versions must be sorted by name")
    return value


class ReasonCountV1(StrictFrozenModel):
    code: RejectionCode
    count: int = Field(ge=1, le=100000)


def _validate_reason_tuple(
    value: tuple[ReasonCountV1, ...], field: str
) -> tuple[ReasonCountV1, ...]:
    codes = [item.code.value for item in value]
    if len(codes) != len(set(codes)):
        raise ValueError(f"{field} contains duplicate codes")
    if codes != sorted(codes):
        raise ValueError(f"{field} must be sorted by code")
    return value


def _reason_tuple(counter: Mapping[RejectionCode, int]) -> tuple[ReasonCountV1, ...]:
    return tuple(
        ReasonCountV1(code=code, count=count)
        for code, count in sorted(counter.items(), key=lambda item: item[0].value)
        if count
    )


def _reason_total(value: Sequence[ReasonCountV1]) -> int:
    return sum(item.count for item in value)


class QueryEmbeddingArtifactV1(StrictFrozenModel):
    artifact_version: Literal[EMBEDDING_ARTIFACT_VERSION]
    source: QueryEmbeddingSource
    model_version: str = Field(pattern=VERSION_RE.pattern, max_length=160)
    dimension: int = Field(ge=0, le=65536)
    vector_sha256: str
    external_call_count: int = Field(ge=0, le=1)

    @field_validator("vector_sha256")
    @classmethod
    def vector_hash(cls, value: str) -> str:
        return _validate_sha256(value, "vector_sha256")

    @model_validator(mode="after")
    def source_consistency(self) -> "QueryEmbeddingArtifactV1":
        if self.source == QueryEmbeddingSource.NOT_USED:
            if (
                self.model_version != "not_used"
                or self.dimension != 0
                or self.vector_sha256 != _sha256([])
                or self.external_call_count != 0
            ):
                raise ValueError("not_used embedding artifact is inconsistent")
        elif self.dimension <= 0 or self.model_version == "not_used":
            raise ValueError("used embedding artifact requires model and dimension")
        elif self.source == QueryEmbeddingSource.EXTERNAL:
            if self.external_call_count != 1:
                raise ValueError("external embedding requires one upstream call")
        elif self.external_call_count != 0:
            raise ValueError("nonexternal embedding cannot report an external call")
        return self

    @classmethod
    def not_used(cls) -> "QueryEmbeddingArtifactV1":
        return cls(
            artifact_version=EMBEDDING_ARTIFACT_VERSION,
            source=QueryEmbeddingSource.NOT_USED,
            model_version="not_used",
            dimension=0,
            vector_sha256=_sha256([]),
            external_call_count=0,
        )

    @classmethod
    def from_vector(
        cls,
        *,
        source: QueryEmbeddingSource,
        model_version: str,
        vector: Sequence[float],
    ) -> "QueryEmbeddingArtifactV1":
        values = tuple(float(item) for item in vector)
        return cls(
            artifact_version=EMBEDDING_ARTIFACT_VERSION,
            source=source,
            model_version=model_version,
            dimension=len(values),
            vector_sha256=_sha256(list(values)),
            external_call_count=1 if source == QueryEmbeddingSource.EXTERNAL else 0,
        )


class MemorySelectionScoresV1(StrictFrozenModel):
    semantic_relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    aggregate_salience: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency: float | None = Field(default=None, ge=0.0, le=1.0)
    recency: float | None = Field(default=None, ge=0.0, le=1.0)
    emotional_significance: float | None = Field(default=None, ge=0.0, le=1.0)
    goal_relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    future_utility: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_history: float | None = Field(default=None, ge=0.0, le=1.0)
    contradiction_pressure: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("*")
    @classmethod
    def finite_scores(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("selection scores must be finite")
        return value


class EvidenceByStanceV1(StrictFrozenModel):
    context: tuple[UUID, ...] = ()
    opposes: tuple[UUID, ...] = ()
    qualifies: tuple[UUID, ...] = ()
    supports: tuple[UUID, ...] = ()

    @field_validator("context", "opposes", "qualifies", "supports")
    @classmethod
    def sorted_unique(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _validate_sorted_unique_uuids(value, info.field_name)

    def all_evidence(self) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                set(self.context + self.opposes + self.qualifies + self.supports),
                key=str,
            )
        )

    @model_validator(mode="after")
    def one_stance_per_evidence(self) -> "EvidenceByStanceV1":
        combined = self.context + self.opposes + self.qualifies + self.supports
        if len(combined) != len(set(combined)):
            raise ValueError("an evidence reference cannot have multiple stances")
        return self


class SelectedMemoryRecordBaseV1(StrictFrozenModel):
    owner_user_id: UUID
    lane: MemoryLane
    record_id: UUID
    revision_id: UUID | None
    source_contract: SourceContractVersionV1
    source_content_sha256: str
    rank: int = Field(ge=1, le=HARD_MAX_RECORDS)
    surface_policy: SurfacePolicy
    sensitivity: Sensitivity
    use_instruction: UseInstruction
    token_estimate: int = Field(ge=1, le=HARD_MAX_TOKENS)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_by: UUID | None = None
    evidence_refs: tuple[UUID, ...]
    observation_refs: tuple[UUID, ...]
    scores: MemorySelectionScoresV1 = MemorySelectionScoresV1()

    @field_validator("source_content_sha256")
    @classmethod
    def source_hash(cls, value: str) -> str:
        return _validate_sha256(value, "source_content_sha256")

    @field_validator("evidence_refs", "observation_refs")
    @classmethod
    def sorted_unique_refs(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _validate_sorted_unique_uuids(value, info.field_name)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def utc_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _validate_utc(value, info.field_name)

    @model_validator(mode="after")
    def common_invariants(self) -> "SelectedMemoryRecordBaseV1":
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        if self.surface_policy not in CONTENT_SURFACE_POLICIES:
            raise ValueError("content record requires a content surface policy")
        if self.use_instruction not in SURFACE_USE_COMPATIBILITY[self.surface_policy]:
            raise ValueError("surface policy and use instruction are incompatible")
        return self


class ClaimSelectionV1(SelectedMemoryRecordBaseV1):
    lane: Literal[MemoryLane.CLAIM]
    text: str = Field(min_length=1, max_length=4000)
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,127}$")
    epistemic_status: EpistemicStatus
    evidence_by_stance: EvidenceByStanceV1
    project_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)
    component_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)

    @model_validator(mode="after")
    def claim_provenance_and_scope(self) -> "ClaimSelectionV1":
        if not self.evidence_refs or not self.observation_refs:
            raise ValueError("claim selection requires evidence and observations")
        if self.evidence_refs != self.evidence_by_stance.all_evidence():
            raise ValueError("claim evidence_refs must equal evidence_by_stance")
        if (
            self.epistemic_status == EpistemicStatus.SUPPORTED
            and not self.evidence_by_stance.supports
        ):
            raise ValueError("supported claim requires supporting evidence")
        if self.epistemic_status in {
            EpistemicStatus.UNCERTAIN,
            EpistemicStatus.DISPUTED,
        } and (
            self.use_instruction
            != UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE
        ):
            raise ValueError("uncertain or disputed claim requires uncertainty instruction")
        if (
            self.epistemic_status == EpistemicStatus.SUPPORTED
            and self.use_instruction
            == UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE
        ):
            raise ValueError("supported claim cannot use uncertainty instruction")
        if self.component_key and not self.project_key:
            raise ValueError("claim component_key requires project_key")
        if (
            self.surface_policy == SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY
            and not self.project_key
        ):
            raise ValueError("exact-project claim requires project scope")
        return self


class LifePreferenceSelectionV1(SelectedMemoryRecordBaseV1):
    lane: Literal[MemoryLane.PREFERENCE]
    preference_key: str = Field(pattern=KEY_RE.pattern)
    preference_class: Literal["life"]
    preference_domain: str = Field(pattern=KEY_RE.pattern)
    canonical_value_json: str = Field(min_length=1, max_length=4000)
    polarity: LifePreferencePolarity
    stability: LifePreferenceStability

    @field_validator("canonical_value_json")
    @classmethod
    def canonical_json_value(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical_value_json must be valid JSON") from exc
        canonical = _canonical_json_bytes(parsed).decode("utf-8")
        if value != canonical:
            raise ValueError("canonical_value_json must use canonical JSON")
        if len(value.encode("utf-8")) > 4000:
            raise ValueError("canonical_value_json exceeds 4000 bytes")
        return value

    @model_validator(mode="after")
    def preference_provenance(self) -> "LifePreferenceSelectionV1":
        if not self.evidence_refs:
            raise ValueError("life preference selection requires evidence")
        return self


class ProjectKnowledgeSelectionV1(SelectedMemoryRecordBaseV1):
    lane: Literal[MemoryLane.PROJECT_KNOWLEDGE]
    text: str = Field(min_length=1, max_length=4000)
    project_id: UUID
    project_key: str = Field(pattern=PROJECT_KEY_RE.pattern)
    component_id: UUID | None
    component_key: str | None = Field(pattern=PROJECT_KEY_RE.pattern)
    knowledge_key: str = Field(pattern=KEY_RE.pattern)
    knowledge_kind: ProjectKnowledgeKind
    document_state: ProjectDocumentState
    authority_level: ProjectAuthorityLevel

    @model_validator(mode="after")
    def project_provenance_and_scope(self) -> "ProjectKnowledgeSelectionV1":
        if not self.evidence_refs or not self.observation_refs:
            raise ValueError("project selection requires evidence and observations")
        if (self.component_id is None) != (self.component_key is None):
            raise ValueError("project component_id and component_key must both be null or set")
        if self.surface_policy != SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY:
            raise ValueError("project knowledge requires exact project scope policy")
        return self


SelectedMemoryRecordV1 = Annotated[
    Union[ClaimSelectionV1, LifePreferenceSelectionV1, ProjectKnowledgeSelectionV1],
    Field(discriminator="lane"),
]


class ResponsePreferenceControlV1(StrictFrozenModel):
    owner_user_id: UUID
    record_id: UUID
    revision_id: UUID
    source_contract: SourceContractVersionV1
    source_content_sha256: str
    preference_key: str = Field(pattern=KEY_RE.pattern)
    action: ResponseControlAction
    scope_sha256: str
    surface_policy: SurfacePolicy
    sensitivity: Sensitivity
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_by: UUID | None = None
    evidence_refs: tuple[UUID, ...]
    content_tokens: Literal[0] = 0

    @field_validator("source_content_sha256", "scope_sha256")
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("evidence_refs")
    @classmethod
    def sorted_unique_refs(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if not value:
            raise ValueError("response control requires evidence")
        return _validate_sorted_unique_uuids(value, "evidence_refs")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def utc_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _validate_utc(value, info.field_name)

    @model_validator(mode="after")
    def control_invariants(self) -> "ResponsePreferenceControlV1":
        if self.surface_policy not in CONTROL_SURFACE_POLICIES:
            raise ValueError("response control requires a control-only surface policy")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class MemoryLaneLimitV1(StrictFrozenModel):
    lane: MemoryLane
    max_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    max_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)


class MemorySelectionBudgetPolicyV1(StrictFrozenModel):
    policy_version: Literal[BUDGET_POLICY_VERSION]
    token_estimator_version: Literal[TOKEN_ESTIMATOR_VERSION]
    max_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    max_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    max_controls: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    lane_limits: tuple[MemoryLaneLimitV1, ...]

    @model_validator(mode="after")
    def unique_lanes(self) -> "MemorySelectionBudgetPolicyV1":
        lanes = [item.lane for item in self.lane_limits]
        if len(lanes) != len(set(lanes)):
            raise ValueError("lane_limits contains duplicate lanes")
        if lanes != sorted(lanes, key=LANE_ORDER.get):
            raise ValueError("lane_limits must use canonical lane order")
        return self

    @classmethod
    def standard(cls) -> "MemorySelectionBudgetPolicyV1":
        return cls(
            policy_version=BUDGET_POLICY_VERSION,
            token_estimator_version=TOKEN_ESTIMATOR_VERSION,
            max_records=8,
            max_tokens=600,
            max_controls=8,
            lane_limits=(
                MemoryLaneLimitV1(
                    lane=MemoryLane.CLAIM, max_records=4, max_tokens=500
                ),
                MemoryLaneLimitV1(
                    lane=MemoryLane.PREFERENCE, max_records=3, max_tokens=160
                ),
                MemoryLaneLimitV1(
                    lane=MemoryLane.PROJECT_KNOWLEDGE,
                    max_records=4,
                    max_tokens=600,
                ),
            ),
        )


class MemoryLaneBudgetUsageV1(StrictFrozenModel):
    lane: MemoryLane
    max_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    max_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    used_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    used_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)

    @model_validator(mode="after")
    def within_limit(self) -> "MemoryLaneBudgetUsageV1":
        if self.used_records > self.max_records or self.used_tokens > self.max_tokens:
            raise ValueError("lane usage exceeds lane limit")
        return self


class MemorySelectionBudgetV1(StrictFrozenModel):
    policy_version: Literal[BUDGET_POLICY_VERSION]
    token_estimator_version: Literal[TOKEN_ESTIMATOR_VERSION]
    max_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    max_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    max_controls: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    used_records: int = Field(ge=0, le=HARD_MAX_RECORDS)
    used_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    used_controls: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    lane_usage: tuple[MemoryLaneBudgetUsageV1, ...]

    @model_validator(mode="after")
    def reconcile(self) -> "MemorySelectionBudgetV1":
        if self.used_records > self.max_records or self.used_tokens > self.max_tokens:
            raise ValueError("selection usage exceeds global budget")
        if self.used_controls > self.max_controls:
            raise ValueError("selection controls exceed global budget")
        lanes = [item.lane for item in self.lane_usage]
        if len(lanes) != len(set(lanes)):
            raise ValueError("lane_usage contains duplicate lanes")
        if lanes != sorted(lanes, key=LANE_ORDER.get):
            raise ValueError("lane_usage must use canonical lane order")
        if self.used_records != sum(item.used_records for item in self.lane_usage):
            raise ValueError("global used_records does not reconcile with lanes")
        if self.used_tokens != sum(item.used_tokens for item in self.lane_usage):
            raise ValueError("global used_tokens does not reconcile with lanes")
        return self


class MemorySelectionRequestV1(StrictFrozenModel):
    contract_version: Literal[REQUEST_VERSION]
    selector_version: Literal[SELECTOR_VERSION]
    intent_adapter_version: str = Field(pattern=VERSION_RE.pattern, max_length=120)
    source_contract_versions: tuple[SourceContractVersionV1, ...]
    selection_trace_id: UUID
    authenticated_actor_user_id: UUID
    owner_user_id: UUID
    request_id: str = Field(min_length=1, max_length=200)
    thread_id: UUID | None
    query_text: str = Field(min_length=1, max_length=16000, exclude=True, repr=False)
    query_vector: tuple[float, ...] = Field(exclude=True, repr=False)
    query_embedding: QueryEmbeddingArtifactV1
    memory_intent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    domains: tuple[str, ...]
    requested_lanes: tuple[MemoryLane, ...]
    selection_directive: SelectionDirective
    explicit_recall: bool
    project_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)
    component_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)
    max_sensitivity: Sensitivity
    selected_at: datetime
    budget_policy: MemorySelectionBudgetPolicyV1

    @field_validator("source_contract_versions")
    @classmethod
    def source_versions(
        cls, value: tuple[SourceContractVersionV1, ...]
    ) -> tuple[SourceContractVersionV1, ...]:
        return _validate_source_versions(value)

    @field_validator("query_vector")
    @classmethod
    def finite_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if len(value) > 65536 or any(not math.isfinite(item) for item in value):
            raise ValueError("query_vector must be finite and bounded")
        return value

    @field_validator("domains")
    @classmethod
    def normalized_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 16 or value != tuple(sorted(set(value))):
            raise ValueError("domains must be sorted, unique, and bounded")
        if any(not KEY_RE.fullmatch(domain) for domain in value):
            raise ValueError("domains contains an invalid value")
        return value

    @field_validator("requested_lanes")
    @classmethod
    def normalized_lanes(cls, value: tuple[MemoryLane, ...]) -> tuple[MemoryLane, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested_lanes must be unique")
        if value != tuple(sorted(value, key=LANE_ORDER.get)):
            raise ValueError("requested_lanes must use canonical lane order")
        return value

    @field_validator("selected_at")
    @classmethod
    def selected_at_utc(cls, value: datetime) -> datetime:
        return _validate_utc(value, "selected_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def owner_scope_embedding_and_directive(self) -> "MemorySelectionRequestV1":
        if self.authenticated_actor_user_id != self.owner_user_id:
            raise ValueError("authenticated actor must equal memory owner")
        if self.component_key and not self.project_key:
            raise ValueError("component_key requires project_key")
        if self.selection_directive == SelectionDirective.EVALUATE:
            if not self.requested_lanes:
                raise ValueError("evaluate requires at least one requested lane")
        else:
            if self.requested_lanes:
                raise ValueError("suppress requires an empty requested_lanes tuple")
            if self.query_vector or self.query_embedding.source != QueryEmbeddingSource.NOT_USED:
                raise ValueError("suppress requires an unused query embedding")
        limit_lanes = {item.lane for item in self.budget_policy.lane_limits}
        if not set(self.requested_lanes).issubset(limit_lanes):
            raise ValueError("every requested lane requires a lane budget")
        if len(self.query_vector) != self.query_embedding.dimension:
            raise ValueError("query vector dimension does not match frozen artifact")
        if _sha256(list(self.query_vector)) != self.query_embedding.vector_sha256:
            raise ValueError("query vector hash does not match frozen artifact")
        if (
            MemoryLane.CLAIM in self.requested_lanes
            and self.selection_directive == SelectionDirective.EVALUATE
            and not self.query_vector
        ):
            raise ValueError("claim selection requires a frozen query embedding")
        return self

    @property
    def query_sha256(self) -> str:
        return _text_sha256(self.query_text)

    @classmethod
    def create(cls, **values: Any) -> "MemorySelectionRequestV1":
        return cls(
            contract_version=REQUEST_VERSION,
            selector_version=SELECTOR_VERSION,
            **values,
        )

    def selector_wire_json_bytes(self) -> bytes:
        raw = self.model_dump(mode="json")
        raw["query_text"] = self.query_text
        raw["query_vector"] = list(self.query_vector)
        return _canonical_json_bytes(raw)

    @classmethod
    def from_selector_wire_json(
        cls, value: str | bytes
    ) -> "MemorySelectionRequestV1":
        """Explicit sensitive wire parser; input contains query text and vector."""

        return cls.model_validate_json(value)

    def strict_revalidated(self) -> "MemorySelectionRequestV1":
        return type(self).from_selector_wire_json(self.selector_wire_json_bytes())


class _MemorySelectionRequestBindingPayloadV1(StrictFrozenModel):
    contract_version: Literal[REQUEST_BINDING_VERSION]
    request_contract_version: Literal[REQUEST_VERSION]
    selector_version: Literal[SELECTOR_VERSION]
    intent_adapter_version: str = Field(pattern=VERSION_RE.pattern, max_length=120)
    source_contract_versions: tuple[SourceContractVersionV1, ...]
    selection_trace_id: UUID
    authenticated_actor_user_id: UUID
    owner_user_id: UUID
    request_id_sha256: str
    thread_id: UUID | None
    thread_id_sha256: str
    query_sha256: str
    query_embedding: QueryEmbeddingArtifactV1
    memory_intent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    domains: tuple[str, ...]
    requested_lanes: tuple[MemoryLane, ...]
    selection_directive: SelectionDirective
    explicit_recall: bool
    project_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)
    component_key: str | None = Field(default=None, pattern=PROJECT_KEY_RE.pattern)
    max_sensitivity: Sensitivity
    selected_at: datetime
    budget_policy: MemorySelectionBudgetPolicyV1

    @field_validator("request_id_sha256", "thread_id_sha256", "query_sha256")
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("source_contract_versions")
    @classmethod
    def source_versions(
        cls, value: tuple[SourceContractVersionV1, ...]
    ) -> tuple[SourceContractVersionV1, ...]:
        return _validate_source_versions(value)

    @field_validator("domains")
    @classmethod
    def normalized_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return MemorySelectionRequestV1.normalized_domains(value)

    @field_validator("requested_lanes")
    @classmethod
    def normalized_lanes(cls, value: tuple[MemoryLane, ...]) -> tuple[MemoryLane, ...]:
        return MemorySelectionRequestV1.normalized_lanes(value)

    @field_validator("selected_at")
    @classmethod
    def selected_at_utc(cls, value: datetime) -> datetime:
        return _validate_utc(value, "selected_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def consistent(self) -> "_MemorySelectionRequestBindingPayloadV1":
        if self.authenticated_actor_user_id != self.owner_user_id:
            raise ValueError("request binding actor must equal owner")
        if self.thread_id_sha256 != _text_sha256(self.thread_id):
            raise ValueError("request binding thread hash mismatch")
        if self.component_key and not self.project_key:
            raise ValueError("request binding component requires project")
        if self.selection_directive == SelectionDirective.EVALUATE:
            if not self.requested_lanes:
                raise ValueError("evaluate binding requires requested lanes")
        else:
            if self.requested_lanes:
                raise ValueError("suppressed binding cannot request lanes")
            if self.query_embedding.source != QueryEmbeddingSource.NOT_USED:
                raise ValueError("suppressed binding requires unused embedding")
        limit_lanes = {item.lane for item in self.budget_policy.lane_limits}
        if not set(self.requested_lanes).issubset(limit_lanes):
            raise ValueError("every bound requested lane requires a lane budget")
        if (
            MemoryLane.CLAIM in self.requested_lanes
            and (
                self.query_embedding.source == QueryEmbeddingSource.NOT_USED
                or self.query_embedding.dimension <= 0
            )
        ):
            raise ValueError("bound claim selection requires an embedding artifact")
        return self


class MemorySelectionRequestBindingV1(_MemorySelectionRequestBindingPayloadV1):
    request_binding_sha256: str

    @field_validator("request_binding_sha256")
    @classmethod
    def binding_hash(cls, value: str) -> str:
        return _validate_sha256(value, "request_binding_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemorySelectionRequestBindingV1":
        payload = self.model_dump(mode="json", exclude={"request_binding_sha256"})
        if self.request_binding_sha256 != _sha256(payload):
            raise ValueError("request binding hash mismatch")
        return self

    @classmethod
    def from_request(
        cls, request: MemorySelectionRequestV1
    ) -> "MemorySelectionRequestBindingV1":
        request = request.strict_revalidated()
        payload = _MemorySelectionRequestBindingPayloadV1(
            contract_version=REQUEST_BINDING_VERSION,
            request_contract_version=request.contract_version,
            selector_version=request.selector_version,
            intent_adapter_version=request.intent_adapter_version,
            source_contract_versions=request.source_contract_versions,
            selection_trace_id=request.selection_trace_id,
            authenticated_actor_user_id=request.authenticated_actor_user_id,
            owner_user_id=request.owner_user_id,
            request_id_sha256=_text_sha256(request.request_id),
            thread_id=request.thread_id,
            thread_id_sha256=_text_sha256(request.thread_id),
            query_sha256=request.query_sha256,
            query_embedding=request.query_embedding,
            memory_intent=request.memory_intent,
            domains=request.domains,
            requested_lanes=request.requested_lanes,
            selection_directive=request.selection_directive,
            explicit_recall=request.explicit_recall,
            project_key=request.project_key,
            component_key=request.component_key,
            max_sensitivity=request.max_sensitivity,
            selected_at=request.selected_at,
            budget_policy=request.budget_policy,
        )
        serialized = payload.model_dump(mode="json")
        return cls(
            **payload.model_dump(),
            request_binding_sha256=_sha256(serialized),
        )


class MemoryLaneSelectionResultV1(StrictFrozenModel):
    owner_user_id: UUID
    lane: MemoryLane
    records: tuple[SelectedMemoryRecordV1, ...] = ()
    controls: tuple[ResponsePreferenceControlV1, ...] = ()
    candidate_count: int = Field(ge=0, le=100000)
    visible_candidate_count: int = Field(ge=0, le=100000)
    eligible_count: int = Field(ge=0, le=100000)
    control_candidate_count: int = Field(ge=0, le=100000)
    candidate_set_sha256: str
    primary_rejection_counts: tuple[ReasonCountV1, ...]
    reason_counts: tuple[ReasonCountV1, ...]
    control_primary_rejection_counts: tuple[ReasonCountV1, ...]
    control_reason_counts: tuple[ReasonCountV1, ...]
    outcome_code: LaneOutcomeCode
    owner_scope_verified: Literal[True]
    postgres_revalidated: Literal[True]
    qdrant_role: Literal["candidate_ids_only", "not_used"]
    database_writes: Literal[0]
    qdrant_writes: Literal[0]
    external_model_calls: Literal[0]

    @field_validator("candidate_set_sha256")
    @classmethod
    def candidate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "candidate_set_sha256")

    @field_validator(
        "primary_rejection_counts",
        "reason_counts",
        "control_primary_rejection_counts",
        "control_reason_counts",
    )
    @classmethod
    def rejection_counts(
        cls, value: tuple[ReasonCountV1, ...], info: Any
    ) -> tuple[ReasonCountV1, ...]:
        return _validate_reason_tuple(value, info.field_name)

    @model_validator(mode="after")
    def reconcile(self) -> "MemoryLaneSelectionResultV1":
        if not (
            len(self.records)
            <= self.eligible_count
            <= self.visible_candidate_count
            <= self.candidate_count
        ):
            raise ValueError("lane candidate counts do not reconcile")
        if _reason_total(self.primary_rejection_counts) != self.candidate_count - len(
            self.records
        ):
            raise ValueError("primary rejection counts must reconcile exactly")
        if self.control_candidate_count < len(self.controls):
            raise ValueError("control candidate count is below selected controls")
        if _reason_total(
            self.control_primary_rejection_counts
        ) != self.control_candidate_count - len(self.controls):
            raise ValueError("control primary rejection counts must reconcile exactly")
        if self.controls and self.lane != MemoryLane.PREFERENCE:
            raise ValueError("response controls belong only to the preference lane")
        for record in self.records:
            if record.owner_user_id != self.owner_user_id:
                raise ValueError("lane selector returned a cross-owner record")
            if record.lane != self.lane:
                raise ValueError("lane selector returned a record from another lane")
        for control in self.controls:
            if control.owner_user_id != self.owner_user_id:
                raise ValueError("lane selector returned a cross-owner control")
        record_heads = [(record.lane, record.record_id) for record in self.records]
        if len(record_heads) != len(set(record_heads)):
            raise ValueError("lane selector returned multiple revisions of one record")
        control_heads = [control.record_id for control in self.controls]
        if len(control_heads) != len(set(control_heads)):
            raise ValueError("lane selector returned multiple revisions of one control")
        ranks = [record.rank for record in self.records]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("lane record ranks must be contiguous")
        if self.records:
            expected = LaneOutcomeCode.SELECTED
        elif self.controls:
            expected = LaneOutcomeCode.CONTROLS_ONLY
        elif self.visible_candidate_count == 0 and self.control_candidate_count == 0:
            expected = LaneOutcomeCode.NO_VISIBLE_MEMORY
        else:
            expected = LaneOutcomeCode.NO_SELECTION
        if self.outcome_code != expected:
            raise ValueError("lane outcome does not match lane result")
        return self


class MemoryLaneOutcomeV1(StrictFrozenModel):
    lane: MemoryLane
    outcome: LaneOutcomeCode


class MemorySelectionObservabilityV1(StrictFrozenModel):
    rejection_registry_version: Literal[REJECTION_REGISTRY_VERSION]
    candidate_count: int = Field(ge=0, le=100000)
    visible_candidate_count: int = Field(ge=0, le=100000)
    eligible_count: int = Field(ge=0, le=100000)
    selected_count: int = Field(ge=0, le=HARD_MAX_RECORDS)
    rejected_candidate_count: int = Field(ge=0, le=100000)
    control_candidate_count: int = Field(ge=0, le=100000)
    selected_control_count: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    rejected_control_count: int = Field(ge=0, le=100000)
    candidate_set_sha256: str
    selection_set_sha256: str
    control_set_sha256: str
    primary_rejection_counts: tuple[ReasonCountV1, ...]
    reason_counts: tuple[ReasonCountV1, ...]
    control_primary_rejection_counts: tuple[ReasonCountV1, ...]
    control_reason_counts: tuple[ReasonCountV1, ...]
    lane_outcomes: tuple[MemoryLaneOutcomeV1, ...]
    query_embedding_artifact_sha256: str
    upstream_external_embedding_calls: int = Field(ge=0, le=1)
    owner_scope_verified: Literal[True]
    postgres_revalidation: Literal["performed", "not_needed"]
    qdrant_role: Literal["candidate_ids_only", "not_used"]
    database_writes: Literal[0]
    qdrant_writes: Literal[0]
    selector_external_model_calls: Literal[0]

    @field_validator(
        "candidate_set_sha256",
        "selection_set_sha256",
        "control_set_sha256",
        "query_embedding_artifact_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator(
        "primary_rejection_counts",
        "reason_counts",
        "control_primary_rejection_counts",
        "control_reason_counts",
    )
    @classmethod
    def rejection_counts(
        cls, value: tuple[ReasonCountV1, ...], info: Any
    ) -> tuple[ReasonCountV1, ...]:
        return _validate_reason_tuple(value, info.field_name)

    @model_validator(mode="after")
    def reconcile(self) -> "MemorySelectionObservabilityV1":
        if not (
            self.selected_count
            <= self.eligible_count
            <= self.visible_candidate_count
            <= self.candidate_count
        ):
            raise ValueError("envelope candidate counts do not reconcile")
        if self.rejected_candidate_count != self.candidate_count - self.selected_count:
            raise ValueError("rejected_candidate_count does not reconcile")
        if _reason_total(self.primary_rejection_counts) != self.rejected_candidate_count:
            raise ValueError("primary rejection counts must reconcile exactly")
        if self.rejected_control_count != (
            self.control_candidate_count - self.selected_control_count
        ):
            raise ValueError("rejected_control_count does not reconcile")
        if _reason_total(
            self.control_primary_rejection_counts
        ) != self.rejected_control_count:
            raise ValueError("control primary rejection counts must reconcile exactly")
        lanes = [item.lane for item in self.lane_outcomes]
        if len(lanes) != len(set(lanes)) or lanes != sorted(
            lanes, key=LANE_ORDER.get
        ):
            raise ValueError("lane outcomes must be sorted and unique")
        expected_revalidation = "performed" if self.lane_outcomes else "not_needed"
        if self.postgres_revalidation != expected_revalidation:
            raise ValueError("Postgres revalidation state does not match selector work")
        return self


def _record_ref_dict(record: SelectedMemoryRecordV1) -> dict[str, Any]:
    return {
        "owner_user_id": str(record.owner_user_id),
        "lane": record.lane.value,
        "record_id": str(record.record_id),
        "revision_id": str(record.revision_id) if record.revision_id else None,
        "source_content_sha256": record.source_content_sha256,
        "rank": record.rank,
    }


def _selection_set_sha256(records: Sequence[SelectedMemoryRecordV1]) -> str:
    return _sha256([_record_ref_dict(record) for record in records])


def _control_ref_dict(control: ResponsePreferenceControlV1) -> dict[str, Any]:
    return {
        "owner_user_id": str(control.owner_user_id),
        "record_id": str(control.record_id),
        "revision_id": str(control.revision_id),
        "source_content_sha256": control.source_content_sha256,
        "control_sha256": _sha256(control),
    }


def _control_selection_set_sha256(
    controls: Sequence[ResponsePreferenceControlV1],
) -> str:
    return _sha256([_control_ref_dict(control) for control in controls])


def estimate_memory_record_tokens_v1(record: SelectedMemoryRecordV1) -> int:
    """Deterministic upper-interface estimate; renderer reports actual tokens later."""

    common = {
        "lane": record.lane.value,
        "surface_policy": record.surface_policy.value,
        "use_instruction": record.use_instruction.value,
    }
    if isinstance(record, ClaimSelectionV1):
        payload = {
            **common,
            "text": record.text,
            "predicate": record.predicate,
            "epistemic_status": record.epistemic_status.value,
        }
    elif isinstance(record, LifePreferenceSelectionV1):
        payload = {
            **common,
            "preference_key": record.preference_key,
            "preference_domain": record.preference_domain,
            "value": record.canonical_value_json,
            "polarity": record.polarity.value,
            "stability": record.stability.value,
        }
    elif isinstance(record, ProjectKnowledgeSelectionV1):
        payload = {
            **common,
            "text": record.text,
            "project_key": record.project_key,
            "component_key": record.component_key,
            "knowledge_key": record.knowledge_key,
            "knowledge_kind": record.knowledge_kind.value,
            "document_state": record.document_state.value,
            "authority_level": record.authority_level.value,
        }
    else:  # pragma: no cover - discriminated union is closed
        raise MemorySelectionContractError("unsupported record type")
    return max(1, min(HARD_MAX_TOKENS, math.ceil(len(_canonical_json_bytes(payload)) / 4)))


class _MemorySelectionEnvelopePayloadV1(StrictFrozenModel):
    contract_version: Literal[CONTRACT_VERSION]
    selector_version: Literal[SELECTOR_VERSION]
    request_binding: MemorySelectionRequestBindingV1
    selection_trace_id: UUID
    owner_user_id: UUID
    selected_at: datetime
    status: SelectionStatus
    outcome_code: SelectionOutcomeCode
    records: tuple[SelectedMemoryRecordV1, ...]
    controls: tuple[ResponsePreferenceControlV1, ...]
    budget: MemorySelectionBudgetV1
    observability: MemorySelectionObservabilityV1

    @field_validator("selected_at")
    @classmethod
    def selected_at_utc(cls, value: datetime) -> datetime:
        return _validate_utc(value, "selected_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def reconcile(self) -> "_MemorySelectionEnvelopePayloadV1":
        binding = self.request_binding
        if self.selector_version != binding.selector_version:
            raise ValueError("selector version differs from request binding")
        if self.selection_trace_id != binding.selection_trace_id:
            raise ValueError("selection trace differs from request binding")
        if self.owner_user_id != binding.owner_user_id:
            raise ValueError("envelope owner differs from request binding")
        if self.selected_at != binding.selected_at:
            raise ValueError("selection time differs from request binding")
        if any(record.owner_user_id != self.owner_user_id for record in self.records):
            raise ValueError("envelope contains a cross-owner record")
        if any(control.owner_user_id != self.owner_user_id for control in self.controls):
            raise ValueError("envelope contains a cross-owner control")
        if any(
            record.lane not in binding.requested_lanes for record in self.records
        ):
            raise ValueError("envelope contains a record from an unrequested lane")
        if self.controls and MemoryLane.PREFERENCE not in binding.requested_lanes:
            raise ValueError("envelope controls require the preference lane")
        ranks = [record.rank for record in self.records]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("envelope record ranks must be contiguous")
        record_ids = [
            (record.lane, record.record_id)
            for record in self.records
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("envelope contains duplicate record handles")
        control_ids = [
            control.record_id for control in self.controls
        ]
        if len(control_ids) != len(set(control_ids)):
            raise ValueError("envelope contains duplicate controls")
        pins = {
            (item.name, item.version)
            for item in binding.source_contract_versions
        }
        if any(
            (item.source_contract.name, item.source_contract.version) not in pins
            for item in (*self.records, *self.controls)
        ):
            raise ValueError("envelope contains an unpinned source contract")
        for record in self.records:
            rejection = _record_policy_rejection(record, binding)
            if rejection is not None:
                raise ValueError(
                    f"envelope record violates selected policy: {rejection.value}"
                )
            if record.token_estimate != estimate_memory_record_tokens_v1(record):
                raise ValueError("envelope record token estimate is not authoritative")
        for control in self.controls:
            rejection = _control_policy_rejection(control, binding)
            if rejection is not None:
                raise ValueError(
                    f"envelope control violates selected policy: {rejection.value}"
                )
        canonical_controls = tuple(
            sorted(
                self.controls,
                key=lambda item: (
                    item.preference_key,
                    str(item.record_id),
                    str(item.revision_id),
                    item.source_content_sha256,
                    item.scope_sha256,
                ),
            )
        )
        if self.controls != canonical_controls:
            raise ValueError("envelope controls are not in canonical order")
        requested_budget = binding.budget_policy
        if (
            self.budget.policy_version != requested_budget.policy_version
            or self.budget.token_estimator_version
            != requested_budget.token_estimator_version
            or self.budget.max_records != requested_budget.max_records
            or self.budget.max_tokens != requested_budget.max_tokens
            or self.budget.max_controls != requested_budget.max_controls
        ):
            raise ValueError("envelope budget differs from request binding")
        if self.budget.used_records != len(self.records):
            raise ValueError("budget used_records does not match records")
        if self.budget.used_tokens != sum(
            record.token_estimate for record in self.records
        ):
            raise ValueError("budget used_tokens does not match records")
        if self.budget.used_controls != len(self.controls):
            raise ValueError("budget used_controls does not match controls")
        lane_usage = {item.lane: item for item in self.budget.lane_usage}
        for lane in binding.requested_lanes:
            lane_records = [record for record in self.records if record.lane == lane]
            usage = lane_usage.get(lane)
            if usage is None:
                raise ValueError("requested lane is absent from lane usage")
            requested_usage = next(
                item for item in requested_budget.lane_limits if item.lane == lane
            )
            if (
                usage.max_records != requested_usage.max_records
                or usage.max_tokens != requested_usage.max_tokens
            ):
                raise ValueError("lane budget differs from request binding")
            if usage.used_records != len(lane_records) or usage.used_tokens != sum(
                record.token_estimate for record in lane_records
            ):
                raise ValueError("lane usage does not match selected records")
        if set(lane_usage) != set(binding.requested_lanes):
            raise ValueError("lane usage differs from requested lanes")
        if self.observability.selected_count != len(self.records):
            raise ValueError("observability selected_count does not match records")
        if self.observability.selected_control_count != len(self.controls):
            raise ValueError("observability selected_control_count does not match controls")
        outcome_by_lane = {
            item.lane: item.outcome for item in self.observability.lane_outcomes
        }
        if set(outcome_by_lane) != set(binding.requested_lanes):
            raise ValueError("lane outcomes differ from requested lanes")
        for lane in binding.requested_lanes:
            has_records = any(record.lane == lane for record in self.records)
            has_controls = lane == MemoryLane.PREFERENCE and bool(self.controls)
            lane_outcome = outcome_by_lane[lane]
            if has_records and lane_outcome != LaneOutcomeCode.SELECTED:
                raise ValueError("selected lane must report selected outcome")
            if not has_records and has_controls and lane_outcome != LaneOutcomeCode.CONTROLS_ONLY:
                raise ValueError("control-only lane must report controls-only outcome")
            if not has_records and not has_controls and lane_outcome not in {
                LaneOutcomeCode.NO_VISIBLE_MEMORY,
                LaneOutcomeCode.NO_SELECTION,
            }:
                raise ValueError("empty lane reports an impossible outcome")
        if self.observability.selection_set_sha256 != _selection_set_sha256(
            self.records
        ):
            raise ValueError("selection set hash mismatch")
        if self.observability.control_set_sha256 != _control_selection_set_sha256(
            self.controls
        ):
            raise ValueError("control set hash mismatch")
        if self.observability.query_embedding_artifact_sha256 != _sha256(
            binding.query_embedding
        ):
            raise ValueError("query embedding artifact hash mismatch")
        if self.observability.upstream_external_embedding_calls != (
            binding.query_embedding.external_call_count
        ):
            raise ValueError("embedding call count mismatch")
        if binding.selection_directive == SelectionDirective.SUPPRESS:
            expected_status = SelectionStatus.SUPPRESSED
            expected_outcome = SelectionOutcomeCode.SUPPRESSED_BY_MEMORY_POLICY
            if (
                self.records
                or self.controls
                or self.observability.candidate_count
                or self.observability.control_candidate_count
            ):
                raise ValueError("suppressed envelope must be zero-selection")
        elif self.records:
            expected_status = SelectionStatus.OK
            expected_outcome = SelectionOutcomeCode.SELECTED
        elif self.controls:
            expected_status = SelectionStatus.OK
            expected_outcome = SelectionOutcomeCode.CONTROLS_ONLY
        elif (
            self.observability.visible_candidate_count == 0
            and self.observability.control_candidate_count == 0
        ):
            expected_status = SelectionStatus.EMPTY
            expected_outcome = SelectionOutcomeCode.NO_VISIBLE_MEMORY
        else:
            expected_status = SelectionStatus.EMPTY
            expected_outcome = SelectionOutcomeCode.NO_SELECTION
        if self.status != expected_status or self.outcome_code != expected_outcome:
            raise ValueError("envelope status/outcome does not match selection")
        return self


class MemorySelectionEnvelopeV1(_MemorySelectionEnvelopePayloadV1):
    envelope_sha256: str

    @field_validator("envelope_sha256")
    @classmethod
    def envelope_hash(cls, value: str) -> str:
        return _validate_sha256(value, "envelope_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemorySelectionEnvelopeV1":
        payload = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if self.envelope_sha256 != _sha256(payload):
            raise ValueError("envelope manifest hash mismatch")
        return self

    @classmethod
    def create(cls, **values: Any) -> "MemorySelectionEnvelopeV1":
        payload = _MemorySelectionEnvelopePayloadV1(
            contract_version=CONTRACT_VERSION,
            selector_version=SELECTOR_VERSION,
            **values,
        )
        serialized = payload.model_dump(mode="json")
        return cls(
            **payload.model_dump(),
            envelope_sha256=_sha256(serialized),
        )

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "MemorySelectionEnvelopeV1":
        """The supported strict wire parser."""

        return cls.model_validate_json(value)

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    def sanitized_trace(self) -> dict[str, Any]:
        obs = self.observability
        return {
            "contract_version": self.contract_version,
            "selector_version": self.selector_version,
            "selection_trace_id": str(self.selection_trace_id),
            "owner_user_id_sha256": _text_sha256(self.owner_user_id),
            "request_id_sha256": self.request_binding.request_id_sha256,
            "thread_id_sha256": self.request_binding.thread_id_sha256,
            "query_sha256": self.request_binding.query_sha256,
            "request_binding_sha256": self.request_binding.request_binding_sha256,
            "envelope_sha256": self.envelope_sha256,
            "status": self.status.value,
            "outcome_code": self.outcome_code.value,
            "candidate_count": obs.candidate_count,
            "visible_candidate_count": obs.visible_candidate_count,
            "eligible_count": obs.eligible_count,
            "selected_count": obs.selected_count,
            "rejected_candidate_count": obs.rejected_candidate_count,
            "control_candidate_count": obs.control_candidate_count,
            "selected_control_count": obs.selected_control_count,
            "rejected_control_count": obs.rejected_control_count,
            "token_budget": self.budget.max_tokens,
            "token_estimate": self.budget.used_tokens,
            "token_estimator_version": self.budget.token_estimator_version,
            "primary_rejection_counts": [
                {"code": item.code.value, "count": item.count}
                for item in obs.primary_rejection_counts
            ],
            "reason_counts": [
                {"code": item.code.value, "count": item.count}
                for item in obs.reason_counts
            ],
            "control_primary_rejection_counts": [
                {"code": item.code.value, "count": item.count}
                for item in obs.control_primary_rejection_counts
            ],
            "control_reason_counts": [
                {"code": item.code.value, "count": item.count}
                for item in obs.control_reason_counts
            ],
            "lane_outcomes": [
                {"lane": item.lane.value, "outcome": item.outcome.value}
                for item in obs.lane_outcomes
            ],
            "candidate_set_sha256": obs.candidate_set_sha256,
            "selection_set_sha256": obs.selection_set_sha256,
            "control_set_sha256": obs.control_set_sha256,
            "query_embedding_artifact_sha256": (
                obs.query_embedding_artifact_sha256
            ),
            "upstream_external_embedding_calls": (
                obs.upstream_external_embedding_calls
            ),
            "postgres_revalidation": obs.postgres_revalidation,
            "database_writes": obs.database_writes,
            "qdrant_writes": obs.qdrant_writes,
            "selector_external_model_calls": obs.selector_external_model_calls,
        }


class GovernedMemoryLaneProviderV1(Protocol):
    async def select(
        self,
        request: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1: ...


def _record_policy_rejection(
    record: SelectedMemoryRecordV1,
    request: MemorySelectionRequestV1,
) -> RejectionCode | None:
    pins = {
        (item.name, item.version) for item in request.source_contract_versions
    }
    if record.source_contract.name not in LANE_SOURCE_CONTRACT_NAMES[record.lane]:
        return RejectionCode.INVALID_SOURCE_CONTRACT
    if (record.source_contract.name, record.source_contract.version) not in pins:
        return RejectionCode.SOURCE_VERSION
    if SENSITIVITY_RANK[record.sensitivity] > SENSITIVITY_RANK[request.max_sensitivity]:
        return RejectionCode.SENSITIVITY
    if record.valid_from and record.valid_from > request.selected_at:
        return RejectionCode.NOT_YET_VALID
    if record.valid_to and record.valid_to <= request.selected_at:
        return RejectionCode.EXPIRED
    if record.superseded_by is not None:
        return RejectionCode.SUPERSEDED
    if (
        record.surface_policy
        in {
            SurfacePolicy.EXPLICIT_RECALL_ONLY,
            SurfacePolicy.RESTRICTED_EXPLICIT_RECALL_ONLY,
        }
        and not request.explicit_recall
    ):
        return RejectionCode.MEMORY_INTENT
    if record.surface_policy not in CONTENT_SURFACE_POLICIES:
        return RejectionCode.SURFACE_POLICY
    if isinstance(record, ProjectKnowledgeSelectionV1):
        if record.document_state == ProjectDocumentState.SUPERSEDED:
            return RejectionCode.SUPERSEDED
        if record.project_key != request.project_key:
            return RejectionCode.PROJECT_SCOPE
        if record.component_key != request.component_key:
            return RejectionCode.PROJECT_SCOPE
    elif record.surface_policy == SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY:
        if not isinstance(record, ClaimSelectionV1):
            return RejectionCode.PROJECT_SCOPE
        if record.project_key != request.project_key:
            return RejectionCode.PROJECT_SCOPE
        if record.component_key != request.component_key:
            return RejectionCode.PROJECT_SCOPE
    return None


def _control_policy_rejection(
    control: ResponsePreferenceControlV1,
    request: MemorySelectionRequestV1,
) -> RejectionCode | None:
    pins = {
        (item.name, item.version) for item in request.source_contract_versions
    }
    if control.source_contract.name not in CONTROL_SOURCE_CONTRACT_NAMES:
        return RejectionCode.INVALID_SOURCE_CONTRACT
    if (control.source_contract.name, control.source_contract.version) not in pins:
        return RejectionCode.SOURCE_VERSION
    if SENSITIVITY_RANK[control.sensitivity] > SENSITIVITY_RANK[
        request.max_sensitivity
    ]:
        return RejectionCode.SENSITIVITY
    if control.valid_from and control.valid_from > request.selected_at:
        return RejectionCode.NOT_YET_VALID
    if control.valid_to and control.valid_to <= request.selected_at:
        return RejectionCode.EXPIRED
    if control.superseded_by is not None:
        return RejectionCode.SUPERSEDED
    if control.surface_policy not in CONTROL_SURFACE_POLICIES:
        return RejectionCode.SURFACE_POLICY
    return None


def _rebuild_record(
    record: SelectedMemoryRecordV1, *, rank: int, token_estimate: int
) -> SelectedMemoryRecordV1:
    raw = record.model_dump(mode="json")
    raw["rank"] = rank
    raw["token_estimate"] = token_estimate
    return type(record).model_validate_json(_canonical_json_bytes(raw))


class AuthoritativeGovernedMemorySelectorV1:
    """Memory-owned composition root; lane providers never reach prompt assembly."""

    def __init__(
        self,
        providers: Mapping[MemoryLane, GovernedMemoryLaneProviderV1],
    ) -> None:
        self._providers = dict(providers)
        if set(self._providers) - set(MemoryLane):
            raise MemorySelectionContractError("unknown lane provider")

    async def select(
        self,
        request: MemorySelectionRequestV1,
    ) -> MemorySelectionEnvelopeV1:
        try:
            request = request.strict_revalidated()
        except Exception as exc:
            raise MemorySelectionContractError("invalid memory selection request") from exc
        request_binding = MemorySelectionRequestBindingV1.from_request(request)
        limit_by_lane = {
            item.lane: item for item in request.budget_policy.lane_limits
        }

        if request.selection_directive == SelectionDirective.SUPPRESS:
            budget = MemorySelectionBudgetV1(
                policy_version=request.budget_policy.policy_version,
                token_estimator_version=request.budget_policy.token_estimator_version,
                max_records=request.budget_policy.max_records,
                max_tokens=request.budget_policy.max_tokens,
                max_controls=request.budget_policy.max_controls,
                used_records=0,
                used_tokens=0,
                used_controls=0,
                lane_usage=(),
            )
            observability = MemorySelectionObservabilityV1(
                rejection_registry_version=REJECTION_REGISTRY_VERSION,
                candidate_count=0,
                visible_candidate_count=0,
                eligible_count=0,
                selected_count=0,
                rejected_candidate_count=0,
                control_candidate_count=0,
                selected_control_count=0,
                rejected_control_count=0,
                candidate_set_sha256=_sha256([]),
                selection_set_sha256=_selection_set_sha256(()),
                control_set_sha256=_control_selection_set_sha256(()),
                primary_rejection_counts=(),
                reason_counts=(),
                control_primary_rejection_counts=(),
                control_reason_counts=(),
                lane_outcomes=(),
                query_embedding_artifact_sha256=_sha256(request.query_embedding),
                upstream_external_embedding_calls=(
                    request.query_embedding.external_call_count
                ),
                owner_scope_verified=True,
                postgres_revalidation="not_needed",
                qdrant_role="not_used",
                database_writes=0,
                qdrant_writes=0,
                selector_external_model_calls=0,
            )
            return MemorySelectionEnvelopeV1.create(
                request_binding=request_binding,
                selection_trace_id=request.selection_trace_id,
                owner_user_id=request.owner_user_id,
                selected_at=request.selected_at,
                status=SelectionStatus.SUPPRESSED,
                outcome_code=SelectionOutcomeCode.SUPPRESSED_BY_MEMORY_POLICY,
                records=(),
                controls=(),
                budget=budget,
                observability=observability,
            )

        results: list[MemoryLaneSelectionResultV1] = []
        for lane in request.requested_lanes:
            provider = self._providers.get(lane)
            if provider is None:
                raise MemorySelectionContractError(
                    f"missing provider for lane {lane.value}"
                )
            raw_result = await provider.select(request, limit_by_lane[lane])
            try:
                result = _wire_revalidate(MemoryLaneSelectionResultV1, raw_result)
            except Exception as exc:
                raise MemorySelectionContractError(
                    f"invalid {lane.value} provider result"
                ) from exc
            if result.owner_user_id != request.owner_user_id:
                raise MemorySelectionContractError("lane provider returned another owner")
            if result.lane != lane:
                raise MemorySelectionContractError("lane provider identity mismatch")
            results.append(result)

        primary: Counter[RejectionCode] = Counter()
        reasons: Counter[RejectionCode] = Counter()
        control_primary: Counter[RejectionCode] = Counter()
        control_reasons: Counter[RejectionCode] = Counter()
        provider_records: list[SelectedMemoryRecordV1] = []
        provider_controls: list[ResponsePreferenceControlV1] = []
        candidate_hashes: list[dict[str, str]] = []
        candidate_count = visible_count = eligible_count = control_candidates = 0
        qdrant_used = False
        result_by_lane: dict[MemoryLane, MemoryLaneSelectionResultV1] = {}
        for result in results:
            result_by_lane[result.lane] = result
            primary.update({item.code: item.count for item in result.primary_rejection_counts})
            reasons.update({item.code: item.count for item in result.reason_counts})
            control_primary.update(
                {item.code: item.count for item in result.control_primary_rejection_counts}
            )
            control_reasons.update(
                {item.code: item.count for item in result.control_reason_counts}
            )
            candidate_count += result.candidate_count
            visible_count += result.visible_candidate_count
            eligible_count += result.eligible_count
            control_candidates += result.control_candidate_count
            qdrant_used = qdrant_used or result.qdrant_role == "candidate_ids_only"
            candidate_hashes.append(
                {
                    "lane": result.lane.value,
                    "candidate_set_sha256": result.candidate_set_sha256,
                }
            )
            provider_records.extend(result.records)
            provider_controls.extend(result.controls)

        provider_records.sort(
            key=lambda item: (
                LANE_ORDER[item.lane],
                item.rank,
                str(item.record_id),
                str(item.revision_id or ""),
                item.source_content_sha256,
            )
        )
        identities: set[tuple[MemoryLane, UUID]] = set()
        selected: list[SelectedMemoryRecordV1] = []
        used_tokens = 0
        lane_counts: Counter[MemoryLane] = Counter()
        lane_tokens: Counter[MemoryLane] = Counter()
        for record in provider_records:
            if record.owner_user_id != request.owner_user_id:
                raise MemorySelectionContractError("provider returned a cross-owner record")
            identity = (record.lane, record.record_id)
            if identity in identities:
                raise MemorySelectionContractError("duplicate governed record across lanes")
            identities.add(identity)
            rejection = _record_policy_rejection(record, request)
            if rejection is not None:
                primary[rejection] += 1
                reasons[rejection] += 1
                continue
            estimate = estimate_memory_record_tokens_v1(record)
            lane_limit = limit_by_lane[record.lane]
            if lane_counts[record.lane] >= lane_limit.max_records:
                rejection = RejectionCode.LANE_RECORD_BUDGET
            elif lane_tokens[record.lane] + estimate > lane_limit.max_tokens:
                rejection = RejectionCode.LANE_TOKEN_BUDGET
            elif len(selected) >= request.budget_policy.max_records:
                rejection = RejectionCode.GLOBAL_RECORD_BUDGET
            elif used_tokens + estimate > request.budget_policy.max_tokens:
                rejection = RejectionCode.GLOBAL_TOKEN_BUDGET
            else:
                rejection = None
            if rejection is not None:
                primary[rejection] += 1
                reasons[rejection] += 1
                continue
            rebuilt = _rebuild_record(
                record, rank=len(selected) + 1, token_estimate=estimate
            )
            selected.append(rebuilt)
            lane_counts[record.lane] += 1
            lane_tokens[record.lane] += estimate
            used_tokens += estimate

        provider_controls.sort(
            key=lambda item: (
                item.preference_key,
                str(item.record_id),
                str(item.revision_id),
                item.source_content_sha256,
                item.scope_sha256,
            )
        )
        selected_controls: list[ResponsePreferenceControlV1] = []
        control_ids: set[UUID] = set()
        for control in provider_controls:
            if control.owner_user_id != request.owner_user_id:
                raise MemorySelectionContractError("provider returned a cross-owner control")
            identity = control.record_id
            if identity in control_ids:
                raise MemorySelectionContractError("duplicate response control")
            control_ids.add(identity)
            rejection = _control_policy_rejection(control, request)
            if rejection is None and (
                len(selected_controls) >= request.budget_policy.max_controls
            ):
                rejection = RejectionCode.GLOBAL_CONTROL_BUDGET
            if rejection is not None:
                control_primary[rejection] += 1
                control_reasons[rejection] += 1
                continue
            selected_controls.append(control)

        lane_usage = tuple(
            MemoryLaneBudgetUsageV1(
                lane=lane,
                max_records=limit_by_lane[lane].max_records,
                max_tokens=limit_by_lane[lane].max_tokens,
                used_records=lane_counts[lane],
                used_tokens=lane_tokens[lane],
            )
            for lane in request.requested_lanes
        )
        budget = MemorySelectionBudgetV1(
            policy_version=request.budget_policy.policy_version,
            token_estimator_version=request.budget_policy.token_estimator_version,
            max_records=request.budget_policy.max_records,
            max_tokens=request.budget_policy.max_tokens,
            max_controls=request.budget_policy.max_controls,
            used_records=len(selected),
            used_tokens=used_tokens,
            used_controls=len(selected_controls),
            lane_usage=lane_usage,
        )
        lane_outcomes: list[MemoryLaneOutcomeV1] = []
        for lane in request.requested_lanes:
            lane_selected = any(item.lane == lane for item in selected)
            lane_controls = lane == MemoryLane.PREFERENCE and bool(selected_controls)
            lane_result = result_by_lane[lane]
            if lane_selected:
                outcome = LaneOutcomeCode.SELECTED
            elif lane_controls:
                outcome = LaneOutcomeCode.CONTROLS_ONLY
            elif (
                lane_result.visible_candidate_count == 0
                and lane_result.control_candidate_count == 0
            ):
                outcome = LaneOutcomeCode.NO_VISIBLE_MEMORY
            else:
                outcome = LaneOutcomeCode.NO_SELECTION
            lane_outcomes.append(MemoryLaneOutcomeV1(lane=lane, outcome=outcome))
        observability = MemorySelectionObservabilityV1(
            rejection_registry_version=REJECTION_REGISTRY_VERSION,
            candidate_count=candidate_count,
            visible_candidate_count=visible_count,
            eligible_count=eligible_count,
            selected_count=len(selected),
            rejected_candidate_count=candidate_count - len(selected),
            control_candidate_count=control_candidates,
            selected_control_count=len(selected_controls),
            rejected_control_count=control_candidates - len(selected_controls),
            candidate_set_sha256=_sha256(candidate_hashes),
            selection_set_sha256=_selection_set_sha256(selected),
            control_set_sha256=_control_selection_set_sha256(selected_controls),
            primary_rejection_counts=_reason_tuple(primary),
            reason_counts=_reason_tuple(reasons),
            control_primary_rejection_counts=_reason_tuple(control_primary),
            control_reason_counts=_reason_tuple(control_reasons),
            lane_outcomes=tuple(lane_outcomes),
            query_embedding_artifact_sha256=_sha256(request.query_embedding),
            upstream_external_embedding_calls=(
                request.query_embedding.external_call_count
            ),
            owner_scope_verified=True,
            postgres_revalidation="performed",
            qdrant_role="candidate_ids_only" if qdrant_used else "not_used",
            database_writes=0,
            qdrant_writes=0,
            selector_external_model_calls=0,
        )
        if selected:
            status = SelectionStatus.OK
            outcome = SelectionOutcomeCode.SELECTED
        elif selected_controls:
            status = SelectionStatus.OK
            outcome = SelectionOutcomeCode.CONTROLS_ONLY
        elif visible_count == 0 and control_candidates == 0:
            status = SelectionStatus.EMPTY
            outcome = SelectionOutcomeCode.NO_VISIBLE_MEMORY
        else:
            status = SelectionStatus.EMPTY
            outcome = SelectionOutcomeCode.NO_SELECTION
        return MemorySelectionEnvelopeV1.create(
            request_binding=request_binding,
            selection_trace_id=request.selection_trace_id,
            owner_user_id=request.owner_user_id,
            selected_at=request.selected_at,
            status=status,
            outcome_code=outcome,
            records=tuple(selected),
            controls=tuple(selected_controls),
            budget=budget,
            observability=observability,
        )


async def select_governed_memory_v1(
    selector: AuthoritativeGovernedMemorySelectorV1,
    request: MemorySelectionRequestV1,
) -> MemorySelectionEnvelopeV1:
    """The sole governed Memory V1 selector-to-runtime entry point."""

    return await selector.select(request)


class _MemoryPromptAssemblyContextPayloadV1(StrictFrozenModel):
    contract_version: Literal[PROMPT_ASSEMBLY_CONTEXT_VERSION]
    supported_envelope_contract_version: Literal[CONTRACT_VERSION]
    supported_selector_version: Literal[SELECTOR_VERSION]
    renderer_version: str = Field(pattern=VERSION_RE.pattern, max_length=120)
    authenticated_actor_user_id: UUID
    owner_user_id: UUID
    selection_trace_id: UUID
    request_id_sha256: str
    thread_id: UUID | None
    thread_id_sha256: str
    query_sha256: str
    request_binding_sha256: str
    envelope_sha256: str
    max_memory_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)

    @field_validator(
        "request_id_sha256",
        "thread_id_sha256",
        "query_sha256",
        "request_binding_sha256",
        "envelope_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @model_validator(mode="after")
    def owner_and_thread(self) -> "_MemoryPromptAssemblyContextPayloadV1":
        if self.authenticated_actor_user_id != self.owner_user_id:
            raise ValueError("assembly actor must equal owner")
        if self.thread_id_sha256 != _text_sha256(self.thread_id):
            raise ValueError("assembly thread hash mismatch")
        return self


class MemoryPromptAssemblyContextV1(_MemoryPromptAssemblyContextPayloadV1):
    context_sha256: str

    @field_validator("context_sha256")
    @classmethod
    def context_hash(cls, value: str) -> str:
        return _validate_sha256(value, "context_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemoryPromptAssemblyContextV1":
        payload = self.model_dump(mode="json", exclude={"context_sha256"})
        if self.context_sha256 != _sha256(payload):
            raise ValueError("assembly context hash mismatch")
        return self

    @classmethod
    def from_envelope(
        cls,
        *,
        envelope: MemorySelectionEnvelopeV1,
        authenticated_actor_user_id: UUID,
        renderer_version: str,
        max_memory_prompt_tokens: int | None = None,
    ) -> "MemoryPromptAssemblyContextV1":
        envelope = MemorySelectionEnvelopeV1.from_wire_json(
            envelope.canonical_json_bytes()
        )
        binding = envelope.request_binding
        payload = _MemoryPromptAssemblyContextPayloadV1(
            contract_version=PROMPT_ASSEMBLY_CONTEXT_VERSION,
            supported_envelope_contract_version=CONTRACT_VERSION,
            supported_selector_version=SELECTOR_VERSION,
            renderer_version=renderer_version,
            authenticated_actor_user_id=authenticated_actor_user_id,
            owner_user_id=envelope.owner_user_id,
            selection_trace_id=envelope.selection_trace_id,
            request_id_sha256=binding.request_id_sha256,
            thread_id=binding.thread_id,
            thread_id_sha256=binding.thread_id_sha256,
            query_sha256=binding.query_sha256,
            request_binding_sha256=binding.request_binding_sha256,
            envelope_sha256=envelope.envelope_sha256,
            max_memory_prompt_tokens=(
                envelope.budget.max_tokens
                if max_memory_prompt_tokens is None
                else max_memory_prompt_tokens
            ),
        )
        return cls(
            **payload.model_dump(),
            context_sha256=_sha256(payload.model_dump(mode="json")),
        )


class _MemoryPromptAssemblyInputPayloadV1(StrictFrozenModel):
    contract_version: Literal[PROMPT_ASSEMBLY_INPUT_VERSION]
    context: MemoryPromptAssemblyContextV1
    envelope: MemorySelectionEnvelopeV1

    @model_validator(mode="after")
    def bind_context_to_envelope(self) -> "_MemoryPromptAssemblyInputPayloadV1":
        context = self.context
        envelope = self.envelope
        request = envelope.request_binding
        if context.authenticated_actor_user_id != envelope.owner_user_id:
            raise ValueError("assembly context actor differs from envelope owner")
        if context.owner_user_id != envelope.owner_user_id:
            raise ValueError("assembly context owner differs from envelope owner")
        checks = (
            (context.selection_trace_id, envelope.selection_trace_id, "selection trace"),
            (context.request_id_sha256, request.request_id_sha256, "request hash"),
            (context.thread_id, request.thread_id, "thread id"),
            (context.thread_id_sha256, request.thread_id_sha256, "thread hash"),
            (context.query_sha256, request.query_sha256, "query hash"),
            (
                context.request_binding_sha256,
                request.request_binding_sha256,
                "request binding hash",
            ),
            (context.envelope_sha256, envelope.envelope_sha256, "envelope hash"),
        )
        for actual, expected, label in checks:
            if actual != expected:
                raise ValueError(f"assembly {label} mismatch")
        if context.max_memory_prompt_tokens > envelope.budget.max_tokens:
            raise ValueError("assembly token cap exceeds selected envelope budget")
        return self


class MemoryPromptAssemblyInputV1(_MemoryPromptAssemblyInputPayloadV1):
    assembly_input_sha256: str

    @field_validator("assembly_input_sha256")
    @classmethod
    def input_hash(cls, value: str) -> str:
        return _validate_sha256(value, "assembly_input_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemoryPromptAssemblyInputV1":
        payload = self.model_dump(mode="json", exclude={"assembly_input_sha256"})
        if self.assembly_input_sha256 != _sha256(payload):
            raise ValueError("assembly input hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        context: MemoryPromptAssemblyContextV1,
        envelope: MemorySelectionEnvelopeV1,
    ) -> "MemoryPromptAssemblyInputV1":
        payload = _MemoryPromptAssemblyInputPayloadV1(
            contract_version=PROMPT_ASSEMBLY_INPUT_VERSION,
            context=context,
            envelope=envelope,
        )
        return cls(
            **payload.model_dump(),
            assembly_input_sha256=_sha256(payload.model_dump(mode="json")),
        )

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "MemoryPromptAssemblyInputV1":
        return cls.model_validate_json(value)

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)


class MemoryRecordRefV1(StrictFrozenModel):
    owner_user_id: UUID
    lane: MemoryLane
    record_id: UUID
    revision_id: UUID | None
    source_content_sha256: str
    rank: int = Field(ge=1, le=HARD_MAX_RECORDS)

    @field_validator("source_content_sha256")
    @classmethod
    def content_hash(cls, value: str) -> str:
        return _validate_sha256(value, "source_content_sha256")

    @classmethod
    def from_record(cls, record: SelectedMemoryRecordV1) -> "MemoryRecordRefV1":
        return cls(
            owner_user_id=record.owner_user_id,
            lane=record.lane,
            record_id=record.record_id,
            revision_id=record.revision_id,
            source_content_sha256=record.source_content_sha256,
            rank=record.rank,
        )


class InjectedMemoryRecordV1(StrictFrozenModel):
    record: MemoryRecordRefV1
    actual_prompt_tokens: int = Field(ge=1, le=HARD_MAX_TOKENS)
    rendered_fragment_sha256: str

    @field_validator("rendered_fragment_sha256")
    @classmethod
    def fragment_hash(cls, value: str) -> str:
        return _validate_sha256(value, "rendered_fragment_sha256")


class MemoryAnswerBindingItemV1(StrictFrozenModel):
    record: MemoryRecordRefV1
    injected: bool
    answer_model_exposed: bool
    actual_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    rendered_fragment_sha256: str | None

    @field_validator("rendered_fragment_sha256")
    @classmethod
    def optional_fragment_hash(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_sha256(value, "rendered_fragment_sha256")
        return value

    @model_validator(mode="after")
    def ordered_stages(self) -> "MemoryAnswerBindingItemV1":
        if self.answer_model_exposed and not self.injected:
            raise ValueError("exposed record must be injected")
        if self.injected:
            if self.actual_prompt_tokens <= 0 or self.rendered_fragment_sha256 is None:
                raise ValueError("injected record requires actual tokens and fragment hash")
        elif self.actual_prompt_tokens or self.rendered_fragment_sha256 is not None:
            raise ValueError("noninjected record cannot report prompt material")
        return self


class MemoryControlRefV1(StrictFrozenModel):
    owner_user_id: UUID
    record_id: UUID
    revision_id: UUID
    source_content_sha256: str
    control_sha256: str

    @field_validator("source_content_sha256", "control_sha256")
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @classmethod
    def from_control(cls, control: ResponsePreferenceControlV1) -> "MemoryControlRefV1":
        return cls(
            owner_user_id=control.owner_user_id,
            record_id=control.record_id,
            revision_id=control.revision_id,
            source_content_sha256=control.source_content_sha256,
            control_sha256=_sha256(control),
        )


class MemoryAnswerControlBindingItemV1(StrictFrozenModel):
    control: MemoryControlRefV1
    applied: bool


def _ref_set_sha256(items: Sequence[MemoryRecordRefV1]) -> str:
    return _sha256([item.model_dump(mode="json") for item in items])


def _control_set_sha256(items: Sequence[MemoryControlRefV1]) -> str:
    return _sha256([item.model_dump(mode="json") for item in items])


class _FinalAnswerMemoryBindingPayloadV1(StrictFrozenModel):
    contract_version: Literal[ANSWER_BINDING_VERSION]
    envelope_contract_version: Literal[CONTRACT_VERSION]
    owner_user_id: UUID
    answer_id: UUID
    selection_trace_id: UUID
    envelope_sha256: str
    request_binding_sha256: str
    assembly_context_sha256: str
    assembly_input_sha256: str
    renderer_version: str = Field(pattern=VERSION_RE.pattern, max_length=120)
    max_memory_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    request_id_sha256: str
    thread_id_sha256: str
    selection_set_sha256: str
    injected_set_sha256: str
    exposed_set_sha256: str
    selected_control_set_sha256: str
    applied_control_set_sha256: str
    selected_count: int = Field(ge=0, le=HARD_MAX_RECORDS)
    injected_count: int = Field(ge=0, le=HARD_MAX_RECORDS)
    exposed_count: int = Field(ge=0, le=HARD_MAX_RECORDS)
    selected_control_count: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    applied_control_count: int = Field(ge=0, le=HARD_MAX_CONTROLS)
    actual_prompt_memory_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    outcome: Literal[
        "no_memory_selected",
        "selected_not_injected",
        "injected_not_exposed",
        "exposed",
    ]
    items: tuple[MemoryAnswerBindingItemV1, ...]
    control_items: tuple[MemoryAnswerControlBindingItemV1, ...]
    created_at: datetime

    @field_validator(
        "envelope_sha256",
        "request_binding_sha256",
        "assembly_context_sha256",
        "assembly_input_sha256",
        "request_id_sha256",
        "thread_id_sha256",
        "selection_set_sha256",
        "injected_set_sha256",
        "exposed_set_sha256",
        "selected_control_set_sha256",
        "applied_control_set_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def created_at_utc(cls, value: datetime) -> datetime:
        return _validate_utc(value, "created_at")  # type: ignore[return-value]

    @model_validator(mode="after")
    def reconcile(self) -> "_FinalAnswerMemoryBindingPayloadV1":
        refs = [item.record for item in self.items]
        injected = [item.record for item in self.items if item.injected]
        exposed = [item.record for item in self.items if item.answer_model_exposed]
        selected_controls = [item.control for item in self.control_items]
        applied_controls = [item.control for item in self.control_items if item.applied]
        record_ids = [
            (item.owner_user_id, item.lane, item.record_id)
            for item in refs
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("answer binding contains duplicate records")
        control_ids = [
            (item.owner_user_id, item.record_id)
            for item in selected_controls
        ]
        if len(control_ids) != len(set(control_ids)):
            raise ValueError("answer binding contains duplicate controls")
        if any(item.owner_user_id != self.owner_user_id for item in refs):
            raise ValueError("answer binding contains a cross-owner record")
        if any(item.owner_user_id != self.owner_user_id for item in selected_controls):
            raise ValueError("answer binding contains a cross-owner control")
        if [item.rank for item in refs] != list(range(1, len(refs) + 1)):
            raise ValueError("answer binding records must retain contiguous ranks")
        expected_counts = (
            len(refs),
            len(injected),
            len(exposed),
            len(selected_controls),
            len(applied_controls),
        )
        if expected_counts != (
            self.selected_count,
            self.injected_count,
            self.exposed_count,
            self.selected_control_count,
            self.applied_control_count,
        ):
            raise ValueError("answer binding counts do not reconcile")
        if self.actual_prompt_memory_tokens != sum(
            item.actual_prompt_tokens for item in self.items
        ):
            raise ValueError("answer binding actual token count does not reconcile")
        if self.actual_prompt_memory_tokens > self.max_memory_prompt_tokens:
            raise ValueError("answer binding exceeds assembly token cap")
        expected_hashes = (
            _ref_set_sha256(refs),
            _ref_set_sha256(injected),
            _ref_set_sha256(exposed),
            _control_set_sha256(selected_controls),
            _control_set_sha256(applied_controls),
        )
        if expected_hashes != (
            self.selection_set_sha256,
            self.injected_set_sha256,
            self.exposed_set_sha256,
            self.selected_control_set_sha256,
            self.applied_control_set_sha256,
        ):
            raise ValueError("answer binding set hashes do not reconcile")
        if not refs and not selected_controls:
            expected_outcome = "no_memory_selected"
        elif exposed:
            expected_outcome = "exposed"
        elif injected:
            expected_outcome = "injected_not_exposed"
        else:
            expected_outcome = "selected_not_injected"
        if self.outcome != expected_outcome:
            raise ValueError("answer binding outcome does not match stages")
        return self


class FinalAnswerMemoryBindingV1(_FinalAnswerMemoryBindingPayloadV1):
    binding_manifest_sha256: str

    @field_validator("binding_manifest_sha256")
    @classmethod
    def binding_hash(cls, value: str) -> str:
        return _validate_sha256(value, "binding_manifest_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "FinalAnswerMemoryBindingV1":
        payload = self.model_dump(mode="json", exclude={"binding_manifest_sha256"})
        if self.binding_manifest_sha256 != _sha256(payload):
            raise ValueError("answer binding manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        assembly_input: MemoryPromptAssemblyInputV1,
        owner_user_id: UUID,
        answer_id: UUID,
        injected: Sequence[InjectedMemoryRecordV1] = (),
        answer_model_exposed: Sequence[MemoryRecordRefV1] = (),
        applied_controls: Sequence[MemoryControlRefV1] = (),
        created_at: datetime,
    ) -> "FinalAnswerMemoryBindingV1":
        assembly_input = MemoryPromptAssemblyInputV1.from_wire_json(
            assembly_input.canonical_json_bytes()
        )
        envelope = assembly_input.envelope
        context = assembly_input.context
        if owner_user_id != envelope.owner_user_id:
            raise MemorySelectionContractError("binding owner does not match envelope")
        all_refs = tuple(MemoryRecordRefV1.from_record(item) for item in envelope.records)
        all_controls = tuple(
            MemoryControlRefV1.from_control(item) for item in envelope.controls
        )
        if _control_set_sha256(all_controls) != (
            envelope.observability.control_set_sha256
        ):
            raise MemorySelectionContractError(
                "binding controls differ from selected envelope controls"
            )

        def record_key(item: MemoryRecordRefV1) -> tuple[Any, ...]:
            return (
                item.owner_user_id,
                item.lane,
                item.record_id,
                item.revision_id,
                item.source_content_sha256,
                item.rank,
            )

        def control_key(item: MemoryControlRefV1) -> tuple[Any, ...]:
            return (
                item.owner_user_id,
                item.record_id,
                item.revision_id,
                item.source_content_sha256,
                item.control_sha256,
            )

        selected = {record_key(item): item for item in all_refs}
        selected_controls = {control_key(item): item for item in all_controls}
        injected_by_key: dict[tuple[Any, ...], InjectedMemoryRecordV1] = {}
        for item in injected:
            key = record_key(item.record)
            if key in injected_by_key or key not in selected:
                raise MemorySelectionContractError(
                    "injected records must be unique selected records"
                )
            injected_by_key[key] = item
        exposed_keys = {record_key(item) for item in answer_model_exposed}
        if len(exposed_keys) != len(answer_model_exposed) or not exposed_keys.issubset(
            injected_by_key
        ):
            raise MemorySelectionContractError(
                "exposed records must be unique injected records"
            )
        applied_control_keys = {control_key(item) for item in applied_controls}
        if len(applied_control_keys) != len(applied_controls) or not (
            applied_control_keys.issubset(selected_controls)
        ):
            raise MemorySelectionContractError(
                "applied controls must be unique selected controls"
            )
        items = tuple(
            MemoryAnswerBindingItemV1(
                record=ref,
                injected=(key := record_key(ref)) in injected_by_key,
                answer_model_exposed=key in exposed_keys,
                actual_prompt_tokens=(
                    injected_by_key[key].actual_prompt_tokens
                    if key in injected_by_key
                    else 0
                ),
                rendered_fragment_sha256=(
                    injected_by_key[key].rendered_fragment_sha256
                    if key in injected_by_key
                    else None
                ),
            )
            for ref in all_refs
        )
        actual_tokens = sum(item.actual_prompt_tokens for item in items)
        if actual_tokens > context.max_memory_prompt_tokens:
            raise MemorySelectionContractError(
                "actual rendered memory tokens exceed assembly budget"
            )
        control_items = tuple(
            MemoryAnswerControlBindingItemV1(
                control=ref,
                applied=control_key(ref) in applied_control_keys,
            )
            for ref in all_controls
        )
        injected_refs = tuple(item.record for item in items if item.injected)
        exposed_refs = tuple(item.record for item in items if item.answer_model_exposed)
        applied_control_refs = tuple(
            item.control for item in control_items if item.applied
        )
        if not all_refs and not all_controls:
            outcome = "no_memory_selected"
        elif exposed_refs:
            outcome = "exposed"
        elif injected_refs:
            outcome = "injected_not_exposed"
        else:
            outcome = "selected_not_injected"
        payload = _FinalAnswerMemoryBindingPayloadV1(
            contract_version=ANSWER_BINDING_VERSION,
            envelope_contract_version=CONTRACT_VERSION,
            owner_user_id=owner_user_id,
            answer_id=answer_id,
            selection_trace_id=envelope.selection_trace_id,
            envelope_sha256=envelope.envelope_sha256,
            request_binding_sha256=(
                envelope.request_binding.request_binding_sha256
            ),
            assembly_context_sha256=context.context_sha256,
            assembly_input_sha256=assembly_input.assembly_input_sha256,
            renderer_version=context.renderer_version,
            max_memory_prompt_tokens=context.max_memory_prompt_tokens,
            request_id_sha256=envelope.request_binding.request_id_sha256,
            thread_id_sha256=envelope.request_binding.thread_id_sha256,
            selection_set_sha256=_ref_set_sha256(all_refs),
            injected_set_sha256=_ref_set_sha256(injected_refs),
            exposed_set_sha256=_ref_set_sha256(exposed_refs),
            selected_control_set_sha256=_control_set_sha256(all_controls),
            applied_control_set_sha256=_control_set_sha256(applied_control_refs),
            selected_count=len(all_refs),
            injected_count=len(injected_refs),
            exposed_count=len(exposed_refs),
            selected_control_count=len(all_controls),
            applied_control_count=len(applied_control_refs),
            actual_prompt_memory_tokens=actual_tokens,
            outcome=outcome,
            items=items,
            control_items=control_items,
            created_at=created_at,
        )
        return cls(
            **payload.model_dump(),
            binding_manifest_sha256=_sha256(payload.model_dump(mode="json")),
        )

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "FinalAnswerMemoryBindingV1":
        return cls.model_validate_json(value)

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)


__all__ = [
    "ANSWER_BINDING_VERSION",
    "BUDGET_POLICY_VERSION",
    "CONTRACT_VERSION",
    "EMBEDDING_ARTIFACT_VERSION",
    "HARD_MAX_CONTROLS",
    "HARD_MAX_RECORDS",
    "HARD_MAX_TOKENS",
    "PROMPT_ASSEMBLY_CONTEXT_VERSION",
    "PROMPT_ASSEMBLY_INPUT_VERSION",
    "REJECTION_REGISTRY_VERSION",
    "REQUEST_BINDING_VERSION",
    "REQUEST_VERSION",
    "SELECTOR_VERSION",
    "TOKEN_ESTIMATOR_VERSION",
    "AuthoritativeGovernedMemorySelectorV1",
    "ClaimSelectionV1",
    "EpistemicStatus",
    "EvidenceByStanceV1",
    "FinalAnswerMemoryBindingV1",
    "GovernedMemoryLaneProviderV1",
    "InjectedMemoryRecordV1",
    "LaneOutcomeCode",
    "LifePreferencePolarity",
    "LifePreferenceSelectionV1",
    "LifePreferenceStability",
    "MemoryControlRefV1",
    "MemoryLane",
    "MemoryLaneLimitV1",
    "MemoryLaneOutcomeV1",
    "MemoryLaneSelectionResultV1",
    "MemoryPromptAssemblyContextV1",
    "MemoryPromptAssemblyInputV1",
    "MemoryRecordRefV1",
    "MemorySelectionBudgetPolicyV1",
    "MemorySelectionContractError",
    "MemorySelectionEnvelopeV1",
    "MemorySelectionRequestBindingV1",
    "MemorySelectionRequestV1",
    "ProjectAuthorityLevel",
    "ProjectDocumentState",
    "ProjectKnowledgeKind",
    "ProjectKnowledgeSelectionV1",
    "QueryEmbeddingArtifactV1",
    "QueryEmbeddingSource",
    "ReasonCountV1",
    "RejectionCode",
    "ResponseControlAction",
    "ResponsePreferenceControlV1",
    "SelectionDirective",
    "SelectionOutcomeCode",
    "SelectionStatus",
    "Sensitivity",
    "SourceContractVersionV1",
    "SurfacePolicy",
    "UseInstruction",
    "estimate_memory_record_tokens_v1",
    "select_governed_memory_v1",
]
