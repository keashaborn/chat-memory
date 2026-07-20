#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


CONTRACT_VERSION = "memory_v1_relational_extraction_v5"
REGISTRY_VERSION = "memory_predicate_registry_v5"
CONTRACT_VERSION_V5_1 = "memory_v1_relational_extraction_v5_1"
REGISTRY_VERSION_V5_1 = "memory_predicate_registry_v5_1"
SUPPORTED_CONTRACT_PROFILES = {
    REGISTRY_VERSION: CONTRACT_VERSION,
    REGISTRY_VERSION_V5_1: CONTRACT_VERSION_V5_1,
}
TEMPORAL_POLICY_VERSION = "memory_temporal_normalization_v5"
SYNTHETIC_PROVIDER_ID = "synthetic_fixture"
SYNTHETIC_PROVIDER_VERSION = "v1"
SOURCE_SYSTEM = "public.chat_log"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REASON_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,99}$"
REASON_CODE_RE = re.compile(REASON_CODE_PATTERN)
TRUSTED_SPAN_DELTA_MIN_QUOTE_CHARS = 24
PROJECT_PREDICATES = {
    "project.constraint",
    "project.current_state",
    "project.proposed_feature",
    "project.requirement",
}
ALWAYS_REVIEW_DEFERRALS = {
    "ambiguous_transcription",
    "compound_requires_split",
    "entity_resolution_unresolved",
    "mixed_authorship",
    "project_scope_unresolved",
    "sensitive_manual_review",
    "unregistered_predicate",
}
SENSITIVITY_RANK = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "restricted": 3,
}
SERVER_OWNED_KEYS = {
    "approved",
    "claim_id",
    "component_key",
    "durable_entity_id",
    "entity_id",
    "evidence_id",
    "job_id",
    "normalization_policy_version",
    "owner_user_id",
    "predicate_registry_status",
    "project_key",
    "project_scope",
    "resolution_state",
    "review_required",
    "salience",
    "source_external_id",
    "source_recorded_at",
    "source_sha256",
    "target_claim_id",
    "vantage_id",
}


EntityType = Literal[
    "self",
    "person",
    "animal",
    "organization",
    "place",
    "project",
    "object",
    "concept",
]
ReasonCode = Annotated[str, StringConstraints(pattern=REASON_CODE_PATTERN)]
ProjectionClass = Literal[
    "direct_claim",
    "supportive_context",
    "correction",
    "life_preference",
    "response_preference",
    "project_knowledge",
    "never_surface",
]
Sensitivity = Literal["low", "medium", "high", "restricted"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProposedSourceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=5000)


class NormalizedSourceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderEntityMention(StrictModel):
    entity_ref: str = Field(pattern=r"^e[0-9]{2}$")
    entity_type: EntityType
    mention_kind: Literal["self_reference", "named", "role_only", "anonymous"]
    name_text: str | None
    relationship_role: str | None
    source_spans: list[ProposedSourceSpan] = Field(min_length=1, max_length=8)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=20)


class NormalizedEntityMention(StrictModel):
    entity_ref: str = Field(pattern=r"^e[0-9]{2}$")
    entity_type: EntityType
    mention_kind: Literal["self_reference", "named", "role_only", "anonymous"]
    name_text: str | None
    relationship_role: str | None
    source_spans: list[NormalizedSourceSpan] = Field(min_length=1, max_length=8)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=20)


class EntityObject(StrictModel):
    kind: Literal["entity"]
    entity_ref: str = Field(pattern=r"^e[0-9]{2}$")


class LifePreferenceValue(StrictModel):
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    target: str = Field(min_length=1, max_length=500)
    polarity: Literal["likes", "dislikes", "prefers", "avoids"]
    context: str | None


class ResponsePreferenceValue(StrictModel):
    dimension: Literal[
        "epistemic_style",
        "format",
        "initiative",
        "reasoning_style",
        "response_length",
        "specificity",
        "tone",
        "voice",
    ]
    value: str = Field(min_length=1, max_length=500)


LiteralValue = (
    str
    | float
    | bool
    | LifePreferenceValue
    | ResponsePreferenceValue
)


class LiteralObject(StrictModel):
    kind: Literal["literal"]
    datatype: Literal[
        "text",
        "number",
        "boolean",
        "date",
        "duration",
        "location",
        "enum",
        "json",
    ]
    value: LiteralValue
    unit: str | None
    approximate: bool


class CalendarRange(StrictModel):
    lower: str | None
    upper: str | None
    bounds: Literal["[)"]


class InstantRange(StrictModel):
    lower: str | None
    upper: str | None
    bounds: Literal["[)"]


class RelativeOffset(StrictModel):
    direction: Literal["past", "future"]
    magnitude: float = Field(gt=0)
    unit: Literal["minute", "hour", "day", "week", "month", "year"]
    approximate: bool
    anchor_source: Literal["evidence_observed_at"]


class Recurrence(StrictModel):
    kind: Literal["unspecified_repeated"]


class ProviderTemporal(StrictModel):
    semantic: Literal[
        "occurrence",
        "state_validity",
        "planned_time",
        "observation_time",
        "none",
    ]
    shape: Literal[
        "none",
        "instant",
        "bounded_interval",
        "open_interval",
        "recurring",
    ]
    basis: Literal["none", "instant", "calendar", "relative", "recurring"]
    source_form: Literal[
        "none",
        "absolute",
        "partial_absolute",
        "relative",
        "implicit_source_time",
    ]
    certainty: Literal["exact", "approximate", "bounded", "unknown"]
    precision: Literal["exact", "minute", "day", "month", "year", "unknown"]
    instant: str | None
    calendar_range: CalendarRange | None
    instant_range: InstantRange | None
    relative_offset: RelativeOffset | None
    recurrence: Recurrence | None
    anchored_to_source_time: bool
    reason_codes: list[ReasonCode] = Field(max_length=10)


class NormalizedTemporal(ProviderTemporal):
    normalization_policy_version: Literal["memory_temporal_normalization_v5"]


class ProjectScope(StrictModel):
    state: Literal["not_applicable", "resolved", "unresolved"]
    project_key: str | None
    component_key: str | None
    binding_source: Literal[
        "not_applicable",
        "explicit_source_text",
        "trusted_component_registry",
        "trusted_thread_binding",
        "unresolved",
    ]


class ProviderObservation(StrictModel):
    observation_ref: str = Field(pattern=r"^o[0-9]{2}$")
    subject_entity_ref: str = Field(pattern=r"^e[0-9]{2}$")
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,99}$")
    object: EntityObject | LiteralObject
    polarity: Literal["affirmed", "negated"]
    modality: Literal[
        "asserted",
        "negated",
        "uncertain",
        "corrective",
        "proposed",
        "planned",
        "endorsed",
        "reported_observation",
    ]
    projection_class: ProjectionClass
    surface_policy: Literal[
        "direct_or_relevant",
        "mention_when_directly_relevant",
        "explicit_recall_only",
        "normalization_only",
        "exact_project_scope_only",
        "relevant_recommendation_or_explicit_recall",
        "zero_token_control_only",
        "never",
    ]
    temporal: ProviderTemporal
    sensitivity: Sensitivity
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_spans: list[ProposedSourceSpan] = Field(min_length=1, max_length=8)
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=20)


class NormalizedObservation(StrictModel):
    observation_ref: str = Field(pattern=r"^o[0-9]{2}$")
    subject_entity_ref: str = Field(pattern=r"^e[0-9]{2}$")
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,99}$")
    predicate_registry_status: Literal["governed"]
    object: EntityObject | LiteralObject
    polarity: Literal["affirmed", "negated"]
    modality: Literal[
        "asserted",
        "negated",
        "uncertain",
        "corrective",
        "proposed",
        "planned",
        "endorsed",
        "reported_observation",
    ]
    projection_class: ProjectionClass
    surface_policy: Literal[
        "direct_or_relevant",
        "mention_when_directly_relevant",
        "explicit_recall_only",
        "normalization_only",
        "exact_project_scope_only",
        "relevant_recommendation_or_explicit_recall",
        "zero_token_control_only",
        "never",
    ]
    temporal: NormalizedTemporal
    project_scope: ProjectScope
    sensitivity: Sensitivity
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_spans: list[NormalizedSourceSpan] = Field(min_length=1, max_length=8)
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=20)


class ProviderComparisonHint(StrictModel):
    observation_ref: str = Field(pattern=r"^o[0-9]{2}$")
    relation_type: Literal[
        "duplicate_of",
        "supports",
        "opposes",
        "qualifies",
        "corrects",
        "supersedes",
    ]
    target_lookup_key: str | None
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=20)


class NormalizedComparisonHint(ProviderComparisonHint):
    resolution_state: Literal["unresolved", "verified"]
    target_claim_id: str | None


class ProviderDeferral(StrictModel):
    reason_code: Literal[
        "question_only",
        "context_missing",
        "ambiguous_transcription",
        "transient_state",
        "structured_domain",
        "project_scope_unresolved",
        "unregistered_predicate",
        "mixed_authorship",
        "sensitive_manual_review",
        "compound_requires_split",
        "entity_resolution_unresolved",
        "insufficient_evidence",
    ]
    memory_shape: Literal[
        "none",
        "direct_claim",
        "supportive_context",
        "correction",
        "life_preference",
        "response_preference",
        "project_knowledge",
        "never_surface",
    ]
    source_spans: list[ProposedSourceSpan] = Field(max_length=8)
    sensitivity: Sensitivity


class NormalizedDeferral(StrictModel):
    reason_code: Literal[
        "question_only",
        "context_missing",
        "ambiguous_transcription",
        "transient_state",
        "structured_domain",
        "project_scope_unresolved",
        "unregistered_predicate",
        "mixed_authorship",
        "sensitive_manual_review",
        "compound_requires_split",
        "entity_resolution_unresolved",
        "insufficient_evidence",
    ]
    memory_shape: Literal[
        "none",
        "direct_claim",
        "supportive_context",
        "correction",
        "life_preference",
        "response_preference",
        "project_knowledge",
        "never_surface",
    ]
    source_spans: list[NormalizedSourceSpan] = Field(max_length=8)
    sensitivity: Sensitivity
    review_required: bool


class ProviderPacket(StrictModel):
    entity_mentions: list[ProviderEntityMention] = Field(max_length=24)
    observations: list[ProviderObservation] = Field(max_length=32)
    comparison_hints: list[ProviderComparisonHint] = Field(max_length=32)
    deferrals: list[ProviderDeferral] = Field(max_length=32)
    packet_findings: list[ReasonCode] = Field(max_length=32)


class SourceEnvelope(StrictModel):
    job_id: str
    source_system: Literal["public.chat_log"]
    source_external_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_recorded_at: str


class NormalizedPacket(StrictModel):
    contract_version: Literal[
        "memory_v1_relational_extraction_v5",
        "memory_v1_relational_extraction_v5_1",
    ]
    source_envelope: SourceEnvelope
    predicate_registry_version: Literal[
        "memory_predicate_registry_v5",
        "memory_predicate_registry_v5_1",
    ]
    entity_mentions: list[NormalizedEntityMention] = Field(max_length=24)
    observations: list[NormalizedObservation] = Field(max_length=32)
    comparison_hints: list[NormalizedComparisonHint] = Field(max_length=32)
    deferrals: list[NormalizedDeferral] = Field(max_length=32)
    packet_findings: list[ReasonCode] = Field(max_length=32)


@dataclass(frozen=True)
class TrustedExtractionSource:
    job_id: str
    source_system: str
    source_external_id: str
    source_sha256: str
    source_recorded_at: str
    content: str

    @classmethod
    def create(
        cls,
        *,
        job_id: Any,
        source_system: Any,
        source_external_id: Any,
        source_sha256: Any,
        source_recorded_at: Any,
        content: Any,
    ) -> "TrustedExtractionSource":
        canonical_job_id = _uuid_text(job_id, "job_id")
        canonical_external_id = _uuid_text(
            source_external_id,
            "source_external_id",
        )
        if source_system != SOURCE_SYSTEM:
            raise ValueError("V5 source system must be public.chat_log")
        if not isinstance(source_sha256, str) or not SHA256_RE.fullmatch(
            source_sha256
        ):
            raise ValueError("source SHA-256 is invalid")
        if not isinstance(content, str):
            raise ValueError("source content must be text")
        if sha256_text(content) != source_sha256:
            raise ValueError("source content SHA-256 mismatch")
        return cls(
            job_id=canonical_job_id,
            source_system=SOURCE_SYSTEM,
            source_external_id=canonical_external_id,
            source_sha256=source_sha256,
            source_recorded_at=_datetime_text(
                source_recorded_at,
                "source_recorded_at",
            ),
            content=content,
        )


@dataclass(frozen=True)
class TrustedProjectComponent:
    component_id: str
    component_key: str
    display_name: str
    parent_component_id: str | None
    aliases: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        component_id: Any,
        component_key: Any,
        display_name: Any,
        parent_component_id: Any,
        aliases: Any,
    ) -> "TrustedProjectComponent":
        if not isinstance(component_key, str):
            raise ValueError("component_key must be text")
        canonical_key = _project_name_key(component_key)
        if not canonical_key or len(canonical_key) > 100:
            raise ValueError("component_key must contain 1 to 100 characters")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("component display_name must be non-empty text")
        if len(display_name.strip()) > 200:
            raise ValueError("component display_name exceeds 200 characters")
        if not isinstance(aliases, (list, tuple)) or not 1 <= len(aliases) <= 22:
            raise ValueError("component aliases must contain 1 to 22 values")
        canonical_aliases: set[str] = {
            canonical_key,
            _project_name_key(display_name),
        }
        for alias in aliases:
            if not isinstance(alias, str):
                raise ValueError("component aliases must be text")
            canonical_alias = _project_name_key(alias)
            if not canonical_alias or len(canonical_alias) > 200:
                raise ValueError("component alias is invalid")
            canonical_aliases.add(canonical_alias)
        return cls(
            component_id=_uuid_text(component_id, "component_id"),
            component_key=canonical_key,
            display_name=display_name.strip(),
            parent_component_id=(
                _uuid_text(parent_component_id, "parent_component_id")
                if parent_component_id is not None
                else None
            ),
            aliases=tuple(sorted(canonical_aliases)),
        )


@dataclass(frozen=True)
class TrustedProjectBinding:
    thread_id: str
    project_id: str
    project_key: str
    binding_event_id: str
    components: tuple[TrustedProjectComponent, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        thread_id: Any,
        project_id: Any,
        project_key: Any,
        binding_event_id: Any,
        components: Any = (),
    ) -> "TrustedProjectBinding":
        if not isinstance(project_key, str):
            raise ValueError("project_key must be text")
        canonical_key = project_key.strip()
        if not canonical_key or len(canonical_key) > 500:
            raise ValueError("project_key must contain 1 to 500 characters")
        if not isinstance(components, (list, tuple)) or len(components) > 100:
            raise ValueError("trusted components must contain at most 100 rows")
        canonical_components = tuple(components)
        root_key = _project_name_key(canonical_key)
        component_keys: set[str] = set()
        component_ids: set[str] = set()
        component_aliases: set[str] = set()
        for component in canonical_components:
            if not isinstance(component, TrustedProjectComponent):
                raise ValueError("trusted components have an invalid type")
            if component.component_key in component_keys:
                raise ValueError("trusted component keys are not unique")
            if component.component_id in component_ids:
                raise ValueError("trusted component ids are not unique")
            if component_aliases.intersection(component.aliases):
                raise ValueError("trusted component aliases are ambiguous")
            if root_key in component.aliases:
                raise ValueError("trusted component alias conflicts with project root")
            component_keys.add(component.component_key)
            component_ids.add(component.component_id)
            component_aliases.update(component.aliases)
        return cls(
            thread_id=_uuid_text(thread_id, "thread_id"),
            project_id=_uuid_text(project_id, "project_id"),
            project_key=canonical_key,
            binding_event_id=_uuid_text(
                binding_event_id,
                "binding_event_id",
            ),
            components=canonical_components,
        )


@dataclass(frozen=True)
class ValidatedProviderResult:
    provider_id: str
    provider_version: str
    external_model_calls: int
    provider_output_sha256: str
    normalized_packet_sha256: str
    normalized_packet: dict[str, Any]
    manual_review_required: bool


class RelationalExtractionProvider(Protocol):
    provider_id: str
    provider_version: str
    external_model_calls: int
    external_call_capability: bool

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        ...


class SyntheticFixtureProvider:
    provider_id = SYNTHETIC_PROVIDER_ID
    provider_version = SYNTHETIC_PROVIDER_VERSION
    external_model_calls = 0
    external_call_capability = False

    def __init__(self, output: dict[str, Any] | ProviderPacket) -> None:
        raw = (
            output.model_dump(mode="json")
            if isinstance(output, ProviderPacket)
            else output
        )
        _reject_server_owned_keys(raw)
        self._output = ProviderPacket.model_validate(raw)

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        if sha256_text(source.content) != source.source_sha256:
            raise ValueError("synthetic provider source binding changed")
        return self._output.model_copy(deep=True)

    def output_copy(self) -> ProviderPacket:
        return self._output.model_copy(deep=True)


def synthetic_provider(
    *,
    provider_id: str,
    provider_version: str,
    output: dict[str, Any],
) -> SyntheticFixtureProvider:
    if provider_id != SYNTHETIC_PROVIDER_ID:
        raise ValueError("only the synthetic fixture provider is enabled")
    if provider_version != SYNTHETIC_PROVIDER_VERSION:
        raise ValueError("synthetic provider version mismatch")
    return SyntheticFixtureProvider(output)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_registry(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected registry SHA-256 is invalid")
    if file_sha256(path) != expected_sha256:
        raise ValueError("predicate registry SHA-256 mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    _validate_registry(value)
    return value


def load_schema(
    path: Path,
    expected_sha256: str,
    *,
    expected_contract_version: str = CONTRACT_VERSION,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected extraction schema SHA-256 is invalid")
    if file_sha256(path) != expected_sha256:
        raise ValueError("extraction schema SHA-256 mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("extraction schema draft changed")
    if (
        value.get("properties", {})
        .get("contract_version", {})
        .get("const")
        != expected_contract_version
    ):
        raise ValueError("extraction schema contract mismatch")
    if not isinstance(value.get("$defs"), dict):
        raise ValueError("extraction schema definitions are missing")
    return value


def _validate_registry(value: dict[str, Any]) -> None:
    registry_version = value.get("registry_version")
    contract_version = value.get("contract_version")
    if registry_version not in SUPPORTED_CONTRACT_PROFILES:
        raise ValueError("predicate registry version mismatch")
    if SUPPORTED_CONTRACT_PROFILES[registry_version] != contract_version:
        raise ValueError("predicate registry contract mismatch")
    if value.get("status") != "proposed":
        raise ValueError("predicate registry status changed")
    if value.get("runtime_active") is not False:
        raise ValueError("predicate registry must remain runtime-inactive")
    predicates = value.get("predicates")
    contracts = value.get("object_contracts")
    if not isinstance(predicates, list) or not isinstance(contracts, dict):
        raise ValueError("predicate registry shape is invalid")
    names = [item.get("predicate") for item in predicates if isinstance(item, dict)]
    if len(names) != len(predicates) or len(names) != len(set(names)):
        raise ValueError("predicate registry contains invalid or duplicate predicates")
    for item in predicates:
        if item.get("object_contract") not in contracts:
            raise ValueError("predicate registry references an unknown object contract")


def validate_and_normalize(
    provider: RelationalExtractionProvider,
    *,
    source: TrustedExtractionSource,
    registry: dict[str, Any],
    schema: dict[str, Any],
    trusted_project_binding: TrustedProjectBinding | None = None,
    allowed_provider_versions: Mapping[str, str] | None = None,
    max_external_model_calls: int = 0,
) -> ValidatedProviderResult:
    _validate_registry(registry)
    registry_version = str(registry["registry_version"])
    contract_version = SUPPORTED_CONTRACT_PROFILES[registry_version]
    schema_contract_version = (
        schema.get("properties", {})
        .get("contract_version", {})
        .get("const")
    )
    if schema_contract_version != contract_version:
        raise ValueError("extraction schema/registry contract mismatch")
    allowlist = (
        {SYNTHETIC_PROVIDER_ID: SYNTHETIC_PROVIDER_VERSION}
        if allowed_provider_versions is None
        else dict(allowed_provider_versions)
    )
    if allowlist.get(provider.provider_id) != provider.provider_version:
        raise ValueError("extraction provider/version is not enabled")
    if not 0 <= max_external_model_calls <= 4:
        raise ValueError("external model call budget must be between 0 and 4")
    if provider.external_call_capability and max_external_model_calls == 0:
        raise ValueError("external model calls are disabled")
    calls_before = int(provider.external_model_calls)
    if calls_before < 0:
        raise ValueError("provider external model call count is invalid")

    proposed = provider.extract(source)
    calls_after = int(provider.external_model_calls)
    external_model_calls = calls_after - calls_before
    if external_model_calls < 0:
        raise ValueError("provider external model call count regressed")
    if external_model_calls > max_external_model_calls:
        raise ValueError("provider exceeded the external model call budget")
    raw = proposed.model_dump(mode="json")
    _reject_server_owned_keys(raw)
    _validate_reason_code_tree(proposed)
    trusted_span_deltas = _trusted_source_span_deltas(raw, source.content)

    mentions: list[dict[str, Any]] = []
    entity_refs: set[str] = set()
    entity_types: dict[str, str] = {}
    entity_mentions_by_ref: dict[str, dict[str, Any]] = {}
    for item in raw["entity_mentions"]:
        ref = item["entity_ref"]
        if ref in entity_refs:
            raise ValueError(f"duplicate entity_ref: {ref}")
        if item["mention_kind"] == "named" and item["name_text"] is None:
            raise ValueError(f"named entity requires name_text: {ref}")
        if item["entity_type"] == "self" and item["relationship_role"] != "user:self":
            raise ValueError(f"self entity requires user:self role: {ref}")
        item["source_spans"] = _normalize_spans(
            item["source_spans"],
            source.content,
            trusted_span_deltas,
        )
        mentions.append(item)
        entity_refs.add(ref)
        entity_types[ref] = item["entity_type"]
        entity_mentions_by_ref[ref] = item

    rules = {item["predicate"]: item for item in registry["predicates"]}
    observations: list[dict[str, Any]] = []
    observation_refs: set[str] = set()
    for item in raw["observations"]:
        ref = item["observation_ref"]
        if ref in observation_refs:
            raise ValueError(f"duplicate observation_ref: {ref}")
        if item["subject_entity_ref"] not in entity_refs:
            raise ValueError(f"dangling subject_entity_ref: {ref}")
        if (
            item["object"]["kind"] == "entity"
            and item["object"]["entity_ref"] not in entity_refs
        ):
            raise ValueError(f"dangling object entity_ref: {ref}")
        rule = rules.get(item["predicate"])
        if rule is None:
            raise ValueError(f"unregistered predicate: {item['predicate']}")
        if (
            item["temporal"]["semantic"] == "none"
            and "observation_time" in rule["temporal_semantics"]
        ):
            item["temporal"]["semantic"] = "observation_time"
            if (
                "server_observation_time_default"
                not in item["temporal"]["reason_codes"]
                and len(item["temporal"]["reason_codes"]) < 10
            ):
                item["temporal"]["reason_codes"].append(
                    "server_observation_time_default"
                )
        _validate_observation_registry(item, rule, registry, entity_types)
        item["source_spans"] = _normalize_spans(
            item["source_spans"],
            source.content,
            trusted_span_deltas,
        )
        item["temporal"] = _normalize_temporal(
            item["temporal"],
            source.source_recorded_at,
        )
        item["predicate_registry_status"] = "governed"
        if (
            item["predicate"] in PROJECT_PREDICATES
            or item["projection_class"] == "project_knowledge"
        ):
            subject_mention = entity_mentions_by_ref[
                item["subject_entity_ref"]
            ]
            resolution = (
                _project_mention_resolution(
                    subject_mention,
                    trusted_project_binding,
                )
                if trusted_project_binding is not None
                else None
            )
            if resolution is None:
                item["project_scope"] = {
                    "state": "unresolved",
                    "project_key": None,
                    "component_key": None,
                    "binding_source": "unresolved",
                }
            else:
                item["project_scope"] = {
                    "state": "resolved",
                    "project_key": trusted_project_binding.project_key,
                    "component_key": resolution[0],
                    "binding_source": resolution[1],
                }
        else:
            item["project_scope"] = {
                "state": "not_applicable",
                "project_key": None,
                "component_key": None,
                "binding_source": "not_applicable",
            }
        observations.append(item)
        observation_refs.add(ref)

    comparisons: list[dict[str, Any]] = []
    for item in raw["comparison_hints"]:
        if item["observation_ref"] not in observation_refs:
            raise ValueError(
                "comparison hint references an unknown observation_ref"
            )
        item["resolution_state"] = "unresolved"
        item["target_claim_id"] = None
        comparisons.append(item)

    project_observations = [
        item
        for item in observations
        if item["projection_class"] == "project_knowledge"
    ]
    project_scope_resolved = bool(project_observations) and all(
        item["project_scope"]["state"] == "resolved"
        for item in project_observations
    )

    deferrals: list[dict[str, Any]] = []
    for item in raw["deferrals"]:
        if (
            project_scope_resolved
            and item["reason_code"] == "project_scope_unresolved"
            and item["memory_shape"] == "project_knowledge"
        ):
            continue
        item["source_spans"] = _normalize_spans(
            item["source_spans"],
            source.content,
            trusted_span_deltas,
        )
        item["review_required"] = (
            item["reason_code"] in ALWAYS_REVIEW_DEFERRALS
        )
        deferrals.append(item)

    if not project_scope_resolved and project_observations and not any(
        item["reason_code"] == "project_scope_unresolved"
        for item in deferrals
    ):
        first = project_observations[0]
        deferrals.append(
            {
                "reason_code": "project_scope_unresolved",
                "memory_shape": "project_knowledge",
                "source_spans": first["source_spans"],
                "sensitivity": first["sensitivity"],
                "review_required": True,
            }
        )
    sensitive_observations = [
        item
        for item in observations
        if item["sensitivity"] in {"high", "restricted"}
    ]
    if sensitive_observations and not any(
        item["reason_code"] == "sensitive_manual_review"
        for item in deferrals
    ):
        first = sensitive_observations[0]
        deferrals.append(
            {
                "reason_code": "sensitive_manual_review",
                "memory_shape": first["projection_class"],
                "source_spans": first["source_spans"],
                "sensitivity": first["sensitivity"],
                "review_required": True,
            }
        )
    if len(deferrals) > 32:
        raise ValueError("normalized deferrals exceed V5 packet budget")

    packet_value = {
        "contract_version": contract_version,
        "source_envelope": {
            "job_id": source.job_id,
            "source_system": SOURCE_SYSTEM,
            "source_external_id": source.source_external_id,
            "source_sha256": source.source_sha256,
            "source_recorded_at": source.source_recorded_at,
        },
        "predicate_registry_version": registry_version,
        "entity_mentions": mentions,
        "observations": observations,
        "comparison_hints": comparisons,
        "deferrals": deferrals,
        "packet_findings": raw["packet_findings"],
    }
    packet = NormalizedPacket.model_validate(packet_value)
    normalized = packet.model_dump(mode="json")
    _validate_normalized_packet(normalized, source, schema)
    manual_review = (
        bool(comparisons)
        or any(item["review_required"] for item in deferrals)
        or any(item["entity_type"] != "self" for item in mentions)
        or any(rules[item["predicate"]]["manual_review_rules"] for item in observations)
    )
    return ValidatedProviderResult(
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        external_model_calls=external_model_calls,
        provider_output_sha256=canonical_sha256(raw),
        normalized_packet_sha256=canonical_sha256(normalized),
        normalized_packet=normalized,
        manual_review_required=manual_review,
    )


def _uuid_text(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _project_name_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "-", unaccented.casefold()).strip("-")


def _project_mention_resolution(
    mention: dict[str, Any],
    binding: TrustedProjectBinding,
) -> tuple[str | None, str] | None:
    if mention["entity_type"] != "project":
        return None
    if mention["mention_kind"] == "anonymous":
        if (
            mention["name_text"] is None
            and mention["relationship_role"] == "project:current_thread"
        ):
            return None, "trusted_thread_binding"
        return None
    if mention["mention_kind"] == "named" and mention["name_text"]:
        mention_key = _project_name_key(mention["name_text"])
        if mention_key == _project_name_key(binding.project_key):
            return None, "trusted_thread_binding"
        matches = [
            component
            for component in binding.components
            if mention_key in component.aliases
        ]
        if len(matches) == 1:
            return matches[0].component_key, "trusted_component_registry"
    return None


def _datetime_text(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    else:
        raise ValueError(f"{label} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _date_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _reject_server_owned_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        forbidden = sorted(set(value) & SERVER_OWNED_KEYS)
        if forbidden:
            raise ValueError(
                f"{path} contains server-owned keys: {','.join(forbidden)}"
            )
        for key, child in value.items():
            _reject_server_owned_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_server_owned_keys(child, f"{path}[{index}]")


def _ensure_reason_codes(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} contains duplicate reason codes")
    if any(not REASON_CODE_RE.fullmatch(value) for value in values):
        raise ValueError(f"{label} contains an invalid reason code")


def _validate_reason_code_tree(packet: ProviderPacket) -> None:
    for item in packet.entity_mentions:
        _ensure_reason_codes(item.reason_codes, item.entity_ref)
    for item in packet.observations:
        _ensure_reason_codes(item.reason_codes, item.observation_ref)
        _ensure_reason_codes(
            item.temporal.reason_codes,
            f"{item.observation_ref}.temporal",
        )
    for index, item in enumerate(packet.comparison_hints):
        _ensure_reason_codes(item.reason_codes, f"comparison_hints[{index}]")
    _ensure_reason_codes(packet.packet_findings, "packet_findings")


def _source_span_occurrences(text: str, quote: str) -> list[tuple[int, int]]:
    occurrences: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(quote, cursor)
        if start < 0:
            return occurrences
        occurrences.append((start, start + len(quote)))
        cursor = start + 1


def _trusted_source_span_deltas(
    packet: dict[str, Any],
    text: str,
) -> set[tuple[int, int]]:
    deltas: set[tuple[int, int]] = {(0, 0)}
    span_groups = (
        packet["entity_mentions"],
        packet["observations"],
        packet["deferrals"],
    )
    for group in span_groups:
        for item in group:
            for span in item["source_spans"]:
                occurrences = _source_span_occurrences(text, span["quote"])
                if (
                    len(span["quote"]) >= TRUSTED_SPAN_DELTA_MIN_QUOTE_CHARS
                    and len(occurrences) == 1
                ):
                    verified_start, verified_end = occurrences[0]
                    start_delta = verified_start - span["start"]
                    end_delta = verified_end - span["end"]
                    if start_delta == end_delta:
                        deltas.add((start_delta, end_delta))
    return deltas


def _normalize_spans(
    spans: list[dict[str, Any]],
    text: str,
    trusted_deltas: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in spans:
        start = item["start"]
        end = item["end"]
        quote = item["quote"]
        if (
            start < 0
            or end <= start
            or end > len(text)
            or text[start:end] != quote
        ):
            occurrences = _source_span_occurrences(text, quote)
            if not occurrences:
                raise ValueError(f"source span quote mismatch: {start}:{end}")
            if len(occurrences) == 1:
                start, end = occurrences[0]
            else:
                verified = [
                    candidate
                    for candidate in occurrences
                    if (
                        candidate[0] - start,
                        candidate[1] - end,
                    )
                    in trusted_deltas
                ]
                if len(verified) != 1:
                    raise ValueError(
                        f"source span quote is ambiguous: {start}:{end}"
                    )
                start, end = verified[0]
        key = (start, end)
        if key in seen:
            raise ValueError(f"duplicate source span: {start}:{end}")
        seen.add(key)
        output.append(
            {
                "start": start,
                "end": end,
                "span_sha256": sha256_text(text[start:end]),
            }
        )
    return output


def _normalize_temporal(
    value: dict[str, Any],
    source_recorded_at: str,
) -> dict[str, Any]:
    temporal = dict(value)
    if temporal["instant"] is not None:
        temporal["instant"] = _datetime_text(
            temporal["instant"],
            "temporal.instant",
        )
    if temporal["calendar_range"] is not None:
        calendar_range = dict(temporal["calendar_range"])
        calendar_range["lower"] = _date_text(
            calendar_range["lower"],
            "temporal.calendar_range.lower",
        )
        calendar_range["upper"] = _date_text(
            calendar_range["upper"],
            "temporal.calendar_range.upper",
        )
        if (
            calendar_range["lower"] is not None
            and calendar_range["upper"] is not None
            and calendar_range["lower"] >= calendar_range["upper"]
        ):
            raise ValueError("calendar temporal range is empty or reversed")
        temporal["calendar_range"] = calendar_range
    if temporal["instant_range"] is not None:
        instant_range = dict(temporal["instant_range"])
        instant_range["lower"] = (
            _datetime_text(
                instant_range["lower"],
                "temporal.instant_range.lower",
            )
            if instant_range["lower"] is not None
            else None
        )
        instant_range["upper"] = (
            _datetime_text(
                instant_range["upper"],
                "temporal.instant_range.upper",
            )
            if instant_range["upper"] is not None
            else None
        )
        if (
            instant_range["lower"] is not None
            and instant_range["upper"] is not None
            and instant_range["lower"] >= instant_range["upper"]
        ):
            raise ValueError("instant temporal range is empty or reversed")
        temporal["instant_range"] = instant_range

    if temporal["anchored_to_source_time"]:
        raise ValueError("provider cannot assert trusted source-time anchoring")
    trusted = _datetime_text(source_recorded_at, "source_recorded_at")
    if (
        temporal["shape"] == "none"
        and temporal["semantic"] == "observation_time"
    ):
        temporal.update(
            {
                "shape": "instant",
                "basis": "instant",
                "source_form": "implicit_source_time",
                "certainty": "exact",
                "precision": "exact",
                "instant": trusted,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": True,
            }
        )
    elif temporal["semantic"] == "state_validity" and (
        temporal["shape"] == "none"
        or (
            temporal["shape"] == "open_interval"
            and temporal["basis"] == "none"
            and temporal["instant_range"] is None
        )
    ):
        temporal.update(
            {
                "shape": "open_interval",
                "basis": "instant",
                "source_form": "implicit_source_time",
                "certainty": "exact",
                "precision": "exact",
                "instant": None,
                "calendar_range": None,
                "instant_range": {
                    "lower": trusted,
                    "upper": None,
                    "bounds": "[)",
                },
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": True,
            }
        )
    elif (
        temporal["shape"] == "none"
        and temporal["basis"] == "none"
        and temporal["source_form"] == "implicit_source_time"
    ):
        # An undated occurrence or planned event is not known to have happened
        # at ingestion time. Preserve the unknown time instead of converting
        # source-recording time into a false event timestamp.
        temporal["source_form"] = "none"
        temporal["anchored_to_source_time"] = False
        repair_code = "server_undated_temporal_unanchored"
        if (
            repair_code not in temporal["reason_codes"]
            and len(temporal["reason_codes"]) < 10
        ):
            temporal["reason_codes"].append(repair_code)
    elif temporal["source_form"] == "implicit_source_time":
        temporal["anchored_to_source_time"] = True
        if temporal["shape"] == "instant" and temporal["basis"] == "instant":
            temporal["instant"] = trusted
        elif (
            temporal["shape"] == "open_interval"
            and temporal["basis"] == "instant"
            and temporal["instant_range"] is not None
        ):
            if (
                "historical_relationship_ended_before_source"
                in temporal["reason_codes"]
                and temporal["instant_range"]["lower"] is None
                and temporal["instant_range"]["upper"] is None
            ):
                # Preserve the unknown start and use source time only as a
                # conservative upper bound. This is a one-sided open interval,
                # not a falsely precise closed/bounded interval.
                temporal["instant_range"]["upper"] = trusted
            else:
                temporal["instant_range"]["lower"] = trusted
                temporal["instant_range"]["upper"] = None
        else:
            raise ValueError(
                "implicit source time requires an instant or open instant range"
            )

    populated = [
        key
        for key in (
            "instant",
            "calendar_range",
            "instant_range",
            "relative_offset",
            "recurrence",
        )
        if temporal[key] is not None
    ]
    if temporal["shape"] == "none":
        if populated or temporal["basis"] != "none":
            raise ValueError("temporal none shape is inconsistent")
    elif len(populated) != 1:
        raise ValueError("temporal must contain exactly one compatible value")
    if temporal["basis"] == "instant" and populated[0] not in {
        "instant",
        "instant_range",
    }:
        raise ValueError("instant temporal basis/value mismatch")
    if temporal["basis"] == "calendar" and populated[0] != "calendar_range":
        raise ValueError("calendar temporal basis/value mismatch")
    if temporal["basis"] == "relative" and populated[0] != "relative_offset":
        raise ValueError("relative temporal basis/value mismatch")
    if temporal["basis"] == "recurring" and populated[0] != "recurrence":
        raise ValueError("recurring temporal basis/value mismatch")
    if temporal["source_form"] == "relative" and temporal["basis"] != "relative":
        raise ValueError("relative source form must retain relative basis")
    if temporal["precision"] == "month" and temporal["calendar_range"] is None:
        raise ValueError("month precision requires a calendar range")
    temporal["normalization_policy_version"] = TEMPORAL_POLICY_VERSION
    return temporal


def _validate_observation_registry(
    observation: dict[str, Any],
    rule: dict[str, Any],
    registry: dict[str, Any],
    entity_types: dict[str, str],
) -> None:
    errors: list[str] = []
    subject_type = entity_types[observation["subject_entity_ref"]]
    if subject_type not in rule["subject_entity_types"]:
        errors.append("subject entity type")
    if observation["modality"] not in rule["modalities"]:
        errors.append("modality")
    if observation["projection_class"] not in rule["projection_classes"]:
        errors.append("projection class")
    if observation["surface_policy"] not in rule["surface_policies"]:
        errors.append("surface policy")
    if observation["temporal"]["semantic"] not in rule["temporal_semantics"]:
        errors.append("temporal semantic")
    if (
        SENSITIVITY_RANK[observation["sensitivity"]]
        < SENSITIVITY_RANK[rule["sensitivity_floor"]]
    ):
        errors.append("sensitivity floor")
    contract = registry["object_contracts"][rule["object_contract"]]
    errors.extend(
        _validate_object_contract(
            observation["object"],
            contract,
            entity_types,
        )
    )
    if errors:
        raise ValueError(
            f"predicate registry violation for {observation['observation_ref']}: "
            + ",".join(errors)
        )


def _validate_object_contract(
    obj: dict[str, Any],
    contract: dict[str, Any],
    entity_types: dict[str, str],
) -> list[str]:
    if obj["kind"] != contract["kind"]:
        return ["object kind"]
    if obj["kind"] == "entity":
        if entity_types[obj["entity_ref"]] not in contract["entity_types"]:
            return ["object entity type"]
        return []
    errors: list[str] = []
    _ensure_json_value(obj["value"], "object.value")
    if obj["datatype"] != contract["datatype"]:
        errors.append("literal datatype")
    if obj["unit"] is not None and obj["unit"] not in contract["allowed_units"]:
        errors.append("literal unit")
    if obj["approximate"] and not contract["approximate_allowed"]:
        errors.append("literal approximate")
    errors.extend(
        _simple_schema_validate(
            obj["value"],
            contract["value_schema"],
            "object.value",
        )
    )
    return errors


def _ensure_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path} has a non-string JSON key")
        for key, item in value.items():
            _ensure_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} is not JSON-compatible")


def _simple_schema_validate(
    value: Any,
    schema: dict[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return []
        expected = next((item for item in expected if item != "null"), None)
    type_ok = {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
    }.get(expected, True)
    if not type_ok:
        return [f"{path} expected {expected}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} enum")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path} minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path} maxLength")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path} pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path} exclusiveMinimum")
    if isinstance(value, dict) and expected == "object":
        properties = schema.get("properties", {})
        missing = sorted(set(schema.get("required", [])) - set(value))
        if missing:
            errors.append(f"{path} missing {','.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path} extra {','.join(extra)}")
        for key, child in properties.items():
            if key in value:
                errors.extend(
                    _simple_schema_validate(
                        value[key],
                        child,
                        f"{path}.{key}",
                    )
                )
    return errors


def _validate_normalized_packet(
    packet: dict[str, Any],
    source: TrustedExtractionSource,
    schema: dict[str, Any],
) -> None:
    envelope = packet["source_envelope"]
    expected = {
        "job_id": source.job_id,
        "source_system": source.source_system,
        "source_external_id": source.source_external_id,
        "source_sha256": source.source_sha256,
        "source_recorded_at": source.source_recorded_at,
    }
    if envelope != expected:
        raise ValueError("normalized source envelope is not trusted-source bound")
    encoded = canonical_json(packet)
    if "owner_user_id" in encoded or "vantage_id" in encoded:
        raise ValueError("normalized packet contains an ownership namespace")
    errors = _schema_errors(packet, schema, schema, "$")
    if errors:
        raise ValueError(
            "normalized packet violates the authoritative V5 schema: "
            + "; ".join(errors[:12])
        )


def _schema_errors(
    value: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return [f"{path}: unsupported schema reference"]
        target: Any = root
        for component in reference[2:].split("/"):
            component = component.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or component not in target:
                return [f"{path}: unresolved schema reference"]
            target = target[component]
        if not isinstance(target, dict):
            return [f"{path}: invalid schema reference target"]
        return _schema_errors(value, target, root, path)

    if "oneOf" in schema:
        matches = [
            branch
            for branch in schema["oneOf"]
            if not _schema_errors(value, branch, root, path)
        ]
        return [] if len(matches) == 1 else [f"{path}: oneOf mismatch"]

    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: const mismatch")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum mismatch")

    expected = schema.get("type")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return errors
        expected = next((item for item in expected if item != "null"), None)
    if expected == "null":
        if value is not None:
            errors.append(f"{path}: expected null")
        return errors
    if expected == "object":
        if not isinstance(value, dict):
            return errors + [f"{path}: expected object"]
        properties = schema.get("properties", {})
        missing = sorted(set(schema.get("required", [])) - set(value))
        if missing:
            errors.append(f"{path}: missing {','.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: extra {','.join(extra)}")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(
                    _schema_errors(
                        value[key],
                        child_schema,
                        root,
                        f"{path}.{key}",
                    )
                )
        return errors
    if expected == "array":
        if not isinstance(value, list):
            return errors + [f"{path}: expected array"]
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: maxItems")
        if schema.get("uniqueItems"):
            encoded = [canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: uniqueItems")
        child_schema = schema.get("items")
        if isinstance(child_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _schema_errors(
                        item,
                        child_schema,
                        root,
                        f"{path}[{index}]",
                    )
                )
        return errors
    if expected == "string":
        if not isinstance(value, str):
            return errors + [f"{path}: expected string"]
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: maxLength")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path}: pattern")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(value)
            except ValueError:
                errors.append(f"{path}: uuid format")
        elif schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: date format")
        elif schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError
            except ValueError:
                errors.append(f"{path}: date-time format")
        return errors
    if expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return errors + [f"{path}: expected integer"]
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return errors + [f"{path}: expected number"]
    elif expected == "boolean":
        if not isinstance(value, bool):
            return errors + [f"{path}: expected boolean"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: exclusiveMinimum")
    return errors
