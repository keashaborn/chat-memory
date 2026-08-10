from __future__ import annotations

"""Pure provider request binding and untrusted-result validation."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid5

from .contracts import (
    ContractViolation,
    EntityKind,
    EpistemicStatus,
    ExtractionJobState,
    ObjectKind,
    ProposalState,
    Sensitivity,
    canonical_sha256,
    framed_sha256,
    require_bounded_text,
    require_exact_int,
    require_key,
    require_sha256,
    require_utc,
    require_uuid,
    semantic_key_sha256,
    selection_binding_sha256,
    sha256_text,
)


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,159}$")
_PREDICATE_RE = re.compile(
    r"^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31})+$"
)
CANONICAL_PREDICATE_CATALOG_SHA256 = (
    "5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded"
)

# This is a validator, not configuration. The complete catalog is injected by
# the caller and must equal this closed matrix before any provider request can
# be built or any result can be admitted.
_BROAD_SUBJECTS = (
    EntityKind.ORGANIZATION,
    EntityKind.OTHER,
    EntityKind.PERSON,
    EntityKind.PET,
    EntityKind.PLACE,
    EntityKind.SELF,
)
_THREE_SENSITIVITIES = (
    Sensitivity.ORDINARY,
    Sensitivity.SENSITIVE_SELF,
    Sensitivity.SENSITIVE_THIRD_PARTY,
)
_ALL_EPISTEMIC = (
    EpistemicStatus.DISPUTED,
    EpistemicStatus.SUPPORTED,
    EpistemicStatus.UNCERTAIN,
)
_CANONICAL_RULE_MATRIX: dict[
    str,
    tuple[
        tuple[EntityKind, ...],
        tuple[ObjectKind, ...],
        tuple[Sensitivity, ...],
        tuple[EpistemicStatus, ...],
    ],
] = {
    "commitment.active": (
        (EntityKind.SELF,),
        (ObjectKind.LITERAL,),
        (Sensitivity.ORDINARY, Sensitivity.SENSITIVE_SELF),
        _ALL_EPISTEMIC,
    ),
    "entity.attribute": (
        _BROAD_SUBJECTS,
        (ObjectKind.ENTITY, ObjectKind.LITERAL),
        _THREE_SENSITIVITIES,
        _ALL_EPISTEMIC,
    ),
    "event.occurred": (
        _BROAD_SUBJECTS,
        (ObjectKind.LITERAL,),
        _THREE_SENSITIVITIES,
        _ALL_EPISTEMIC,
    ),
    "identity.alias": (
        _BROAD_SUBJECTS,
        (ObjectKind.LITERAL,),
        _THREE_SENSITIVITIES,
        _ALL_EPISTEMIC,
    ),
    "identity.preferred_name": (
        (EntityKind.PERSON, EntityKind.SELF),
        (ObjectKind.LITERAL,),
        _THREE_SENSITIVITIES,
        _ALL_EPISTEMIC,
    ),
    "location.association": (
        _BROAD_SUBJECTS,
        (ObjectKind.ENTITY, ObjectKind.LITERAL),
        _THREE_SENSITIVITIES,
        _ALL_EPISTEMIC,
    ),
    "preference.personal": (
        (EntityKind.SELF,),
        (ObjectKind.ENTITY, ObjectKind.LITERAL),
        (Sensitivity.ORDINARY, Sensitivity.SENSITIVE_SELF),
        _ALL_EPISTEMIC,
    ),
    "relationship.kind": (
        (EntityKind.PERSON, EntityKind.SELF),
        (ObjectKind.ENTITY,),
        (Sensitivity.SENSITIVE_THIRD_PARTY,),
        _ALL_EPISTEMIC,
    ),
}

class ProposalPurpose(str, Enum):
    NEW_CLAIM = "new_claim"
    CORRECTION = "correction"


@dataclass(frozen=True, slots=True, kw_only=True)
class PredicateRule:
    predicate: str
    subject_kinds: tuple[EntityKind, ...]
    object_kinds: tuple[ObjectKind, ...]
    sensitivities: tuple[Sensitivity, ...]
    epistemic_statuses: tuple[EpistemicStatus, ...]

    def __post_init__(self) -> None:
        if not _PREDICATE_RE.fullmatch(self.predicate):
            raise ContractViolation("invalid_catalog_predicate")
        for values, enum_type, code in (
            (self.subject_kinds, EntityKind, "invalid_catalog_subject_kinds"),
            (self.object_kinds, ObjectKind, "invalid_catalog_object_kinds"),
            (self.sensitivities, Sensitivity, "invalid_catalog_sensitivities"),
            (
                self.epistemic_statuses,
                EpistemicStatus,
                "invalid_catalog_epistemic_statuses",
            ),
        ):
            if not values or any(not isinstance(item, enum_type) for item in values):
                raise ContractViolation(code)
            if tuple(sorted(set(values), key=lambda item: item.value)) != values:
                raise ContractViolation(code)
        if any(
            kind not in {ObjectKind.LITERAL, ObjectKind.ENTITY}
            for kind in self.object_kinds
        ):
            raise ContractViolation("unsupported_catalog_object_kind")

    def material(self) -> dict[str, object]:
        return {
            "predicate": self.predicate,
            "subject_kinds": self.subject_kinds,
            "object_kinds": self.object_kinds,
            "sensitivities": self.sensitivities,
            "epistemic_statuses": self.epistemic_statuses,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PredicateCatalog:
    catalog_name: str
    schema_revision: int
    rules: tuple[PredicateRule, ...]

    def __post_init__(self) -> None:
        if self.catalog_name != "personal-memory":
            raise ContractViolation("predicate_catalog_name_mismatch")
        if self.schema_revision != 1:
            raise ContractViolation("predicate_catalog_revision_mismatch")
        if tuple(sorted(self.rules, key=lambda rule: rule.predicate)) != self.rules:
            raise ContractViolation("predicate_catalog_not_sorted")
        actual = {
            rule.predicate: (
                rule.subject_kinds,
                rule.object_kinds,
                rule.sensitivities,
                rule.epistemic_statuses,
            )
            for rule in self.rules
        }
        if actual != _CANONICAL_RULE_MATRIX:
            raise ContractViolation("predicate_catalog_matrix_mismatch")
        if self.catalog_sha256 != CANONICAL_PREDICATE_CATALOG_SHA256:
            raise ContractViolation("predicate_catalog_sha256_mismatch")

    @property
    def catalog_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.predicate_catalog",
            {
                "catalog_name": self.catalog_name,
                "schema_revision": self.schema_revision,
                "rules": tuple(rule.material() for rule in self.rules),
            },
        )

    def rule_for(self, predicate: str) -> PredicateRule:
        for rule in self.rules:
            if rule.predicate == predicate:
                return rule
        raise ContractViolation("predicate_not_in_catalog")

    def external_material(self) -> dict[str, object]:
        return {
            "catalog_name": self.catalog_name,
            "schema_revision": self.schema_revision,
            "catalog_sha256": self.catalog_sha256,
            "rules": [rule.material() for rule in self.rules],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderRequestBinding:
    owner_user_id: UUID
    job_id: UUID
    evidence_id: UUID
    provider_call_id: UUID
    operation_id: UUID
    source_kind: str
    source_message_id: UUID
    source_thread_id: UUID
    source_window_id: UUID
    window_sha256: str
    source_sha256: str
    selected_sha256: str
    selection_binding_sha256: str
    start_utf8: int
    end_utf8: int
    context_message_id: UUID | None
    context_sha256: str | None
    attempt_number: int
    max_output_tokens: int
    model: str
    schema: str
    predicate_catalog_sha256: str
    external_payload_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        for value, code in (
            (self.owner_user_id, "invalid_provider_owner"),
            (self.job_id, "invalid_provider_job"),
            (self.evidence_id, "invalid_provider_evidence"),
            (self.provider_call_id, "invalid_provider_call"),
            (self.operation_id, "invalid_provider_operation"),
            (self.source_message_id, "invalid_provider_source_message"),
            (self.source_thread_id, "invalid_provider_source_thread"),
            (self.source_window_id, "invalid_provider_source_window"),
        ):
            require_uuid(value, code)
        if self.source_kind != "conversation_message":
            raise ContractViolation("invalid_provider_source_kind")
        require_sha256(self.window_sha256, "invalid_provider_window_sha256")
        require_sha256(self.source_sha256, "invalid_provider_source_sha256")
        require_sha256(self.selected_sha256, "invalid_provider_selected_sha256")
        require_sha256(
            self.selection_binding_sha256,
            "invalid_provider_selection_binding_sha256",
        )
        start = require_exact_int(
            self.start_utf8, code="invalid_provider_start", maximum=10_000_000
        )
        end = require_exact_int(
            self.end_utf8, code="invalid_provider_end", maximum=10_000_000
        )
        if end <= start:
            raise ContractViolation("invalid_provider_range")
        if (self.context_message_id is None) != (self.context_sha256 is None):
            raise ContractViolation("incomplete_provider_context_binding")
        if self.context_message_id is not None:
            require_uuid(self.context_message_id, "invalid_provider_context_message")
            require_sha256(self.context_sha256, "invalid_provider_context_sha256")
        require_exact_int(
            self.attempt_number,
            code="invalid_provider_attempt",
            minimum=1,
            maximum=100,
        )
        require_exact_int(
            self.max_output_tokens,
            code="invalid_provider_max_output_tokens",
            minimum=1,
            maximum=100_000,
        )
        if not _MODEL_RE.fullmatch(self.model):
            raise ContractViolation("invalid_provider_model")
        require_key(self.schema, "invalid_provider_schema")
        require_sha256(
            self.predicate_catalog_sha256,
            "invalid_predicate_catalog_sha256",
        )
        require_sha256(
            self.external_payload_sha256,
            "invalid_external_payload_sha256",
        )
        require_sha256(self.request_sha256, "invalid_provider_request_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderCompletionBinding:
    provider_call_id: UUID
    request_sha256: str
    response_sha256: str
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.provider_call_id, "invalid_completion_provider_call")
        require_sha256(self.request_sha256, "invalid_completion_request_sha256")
        require_sha256(self.response_sha256, "invalid_completion_response_sha256")
        if self.completed_at is not None:
            require_utc(self.completed_at, "invalid_completion_timestamp")


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuredFact:
    subject_entity_type: EntityKind
    subject_entity_key: str
    subject_display_name: str | None
    predicate: str
    object_kind: ObjectKind
    object_entity_type: EntityKind | None
    object_entity_key: str | None
    object_display_name: str | None
    object_literal: str | None
    epistemic_status: EpistemicStatus
    sensitivity: Sensitivity

    @property
    def material(self) -> dict[str, object]:
        return {
            "subject_entity_type": self.subject_entity_type.value,
            "subject_entity_key": self.subject_entity_key,
            "subject_display_name": self.subject_display_name,
            "predicate": self.predicate,
            "object_kind": self.object_kind.value,
            "object_entity_type": (
                self.object_entity_type.value
                if self.object_entity_type is not None
                else None
            ),
            "object_entity_key": self.object_entity_key,
            "object_display_name": self.object_display_name,
            "object_literal": self.object_literal,
            "epistemic_status": self.epistemic_status.value,
            "sensitivity": self.sensitivity.value,
        }

    @property
    def fact_sha256(self) -> str:
        return canonical_sha256("governed_memory.structured_fact", self.material)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedProposal:
    proposal_id: UUID
    operation_id: UUID
    owner_user_id: UUID
    job_id: UUID
    provider_call_id: UUID
    evidence_id: UUID
    state: ProposalState
    proposal_sha256: str

    def __post_init__(self) -> None:
        for value, code in (
            (self.proposal_id, "invalid_proposal_id"),
            (self.operation_id, "invalid_proposal_operation"),
            (self.owner_user_id, "invalid_proposal_owner"),
            (self.job_id, "invalid_proposal_job"),
            (self.provider_call_id, "invalid_proposal_provider_call"),
            (self.evidence_id, "invalid_proposal_evidence"),
        ):
            require_uuid(value, code)
        if self.state is not ProposalState.PENDING_REVIEW:
            raise ContractViolation("invalid_new_proposal_state")
        require_sha256(self.proposal_sha256, "invalid_proposal_sha256")


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _closed_mapping(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation(code)
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise ContractViolation(code)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ContractViolation(code)
    return value


def _enum_tuple(
    value: object,
    *,
    enum_type: type[Enum],
    code: str,
) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractViolation(code)
    try:
        parsed = tuple(enum_type(item) for item in value)
    except (ValueError, TypeError) as exc:
        raise ContractViolation(code) from exc
    if tuple(sorted(set(parsed), key=lambda item: item.value)) != parsed:
        raise ContractViolation(code)
    return parsed


def parse_predicate_catalog(value: Mapping[str, Any]) -> PredicateCatalog:
    row = _closed_mapping(
        value,
        required=frozenset({"catalog_name", "schema_revision", "predicates"}),
        code="invalid_predicate_catalog",
    )
    predicates = row["predicates"]
    if not isinstance(predicates, Mapping) or not predicates:
        raise ContractViolation("invalid_predicate_catalog")
    rules: list[PredicateRule] = []
    for predicate in sorted(predicates):
        if not isinstance(predicate, str):
            raise ContractViolation("invalid_catalog_predicate")
        rule_row = _closed_mapping(
            predicates[predicate],
            required=frozenset(
                {
                    "subject_kinds",
                    "object_kinds",
                    "sensitivities",
                    "epistemic_statuses",
                }
            ),
            code="invalid_predicate_rule",
        )
        rules.append(
            PredicateRule(
                predicate=predicate,
                subject_kinds=_enum_tuple(
                    rule_row["subject_kinds"],
                    enum_type=EntityKind,
                    code="invalid_catalog_subject_kinds",
                ),
                object_kinds=_enum_tuple(
                    rule_row["object_kinds"],
                    enum_type=ObjectKind,
                    code="invalid_catalog_object_kinds",
                ),
                sensitivities=_enum_tuple(
                    rule_row["sensitivities"],
                    enum_type=Sensitivity,
                    code="invalid_catalog_sensitivities",
                ),
                epistemic_statuses=_enum_tuple(
                    rule_row["epistemic_statuses"],
                    enum_type=EpistemicStatus,
                    code="invalid_catalog_epistemic_statuses",
                ),
            )
        )
    return PredicateCatalog(
        catalog_name=str(row["catalog_name"]),
        schema_revision=require_exact_int(
            row["schema_revision"],
            code="invalid_predicate_catalog_revision",
            minimum=1,
        ),
        rules=tuple(rules),
    )


def _catalog_from_external(value: object) -> PredicateCatalog:
    row = _closed_mapping(
        value,
        required=frozenset(
            {"catalog_name", "schema_revision", "catalog_sha256", "rules"}
        ),
        code="invalid_external_predicate_catalog",
    )
    rules = row["rules"]
    if not isinstance(rules, list) or not rules:
        raise ContractViolation("invalid_external_predicate_catalog")
    predicates: dict[str, object] = {}
    for material in rules:
        rule = _closed_mapping(
            material,
            required=frozenset(
                {
                    "predicate",
                    "subject_kinds",
                    "object_kinds",
                    "sensitivities",
                    "epistemic_statuses",
                }
            ),
            code="invalid_external_predicate_rule",
        )
        predicate = rule["predicate"]
        if not isinstance(predicate, str) or predicate in predicates:
            raise ContractViolation("invalid_external_predicate_rule")
        predicates[predicate] = {
            "subject_kinds": rule["subject_kinds"],
            "object_kinds": rule["object_kinds"],
            "sensitivities": rule["sensitivities"],
            "epistemic_statuses": rule["epistemic_statuses"],
        }
    catalog = parse_predicate_catalog(
        {
            "catalog_name": row["catalog_name"],
            "schema_revision": row["schema_revision"],
            "predicates": predicates,
        }
    )
    if row["catalog_sha256"] != catalog.catalog_sha256:
        raise ContractViolation("external_predicate_catalog_sha256_mismatch")
    return catalog


def _normalize_subject(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContractViolation("invalid_fact_subject")
    try:
        kind = EntityKind(value.get("kind"))
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_fact_subject_kind") from exc
    expected = {"kind"} if kind is EntityKind.SELF else {"kind", "label"}
    if set(value) != expected:
        raise ContractViolation("invalid_fact_subject")
    result: dict[str, object] = {
        "subject_entity_type": kind.value,
        "subject_display_name": None,
    }
    if kind is not EntityKind.SELF:
        result["subject_display_name"] = require_bounded_text(
            value["label"],
            code="invalid_fact_subject_label",
            maximum_bytes=256,
            strip=True,
        )
    return result


def _normalize_object(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContractViolation("invalid_fact_object")
    try:
        kind = ObjectKind(value.get("kind"))
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_fact_object_kind") from exc
    if kind is ObjectKind.LITERAL:
        if set(value) != {"kind", "value"}:
            raise ContractViolation("invalid_fact_object")
        return {
            "object_kind": kind.value,
            "object_entity_type": None,
            "object_display_name": None,
            "object_literal": require_bounded_text(
                value["value"],
                code="invalid_fact_object_literal",
                maximum_bytes=2_000,
                strip=True,
            ),
        }
    if set(value) != {"kind", "entity_type", "label"}:
        raise ContractViolation("invalid_fact_object")
    try:
        entity_type = EntityKind(value["entity_type"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_fact_object_entity_type") from exc
    return {
        "object_kind": kind.value,
        "object_entity_type": entity_type.value,
        "object_display_name": require_bounded_text(
            value["label"],
            code="invalid_fact_object_value",
            maximum_bytes=256,
            strip=True,
        ),
        "object_literal": None,
    }


def normalize_catalog_fact(
    value: Mapping[str, Any], catalog: PredicateCatalog
) -> dict[str, object]:
    row = _closed_mapping(
        value,
        required=frozenset(
            {"subject", "predicate", "object", "epistemic_status", "sensitivity"}
        ),
        code="invalid_structured_fact",
    )
    subject = _normalize_subject(row["subject"])
    predicate = row["predicate"]
    if not isinstance(predicate, str) or not _PREDICATE_RE.fullmatch(predicate):
        raise ContractViolation("invalid_fact_predicate")
    object_row = _normalize_object(row["object"])
    try:
        epistemic = EpistemicStatus(row["epistemic_status"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_fact_epistemic_status") from exc
    try:
        sensitivity = Sensitivity(row["sensitivity"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_fact_sensitivity") from exc
    rule = catalog.rule_for(predicate)
    if EntityKind(subject["subject_entity_type"]) not in rule.subject_kinds:
        raise ContractViolation("predicate_subject_kind_denied")
    if ObjectKind(object_row["object_kind"]) not in rule.object_kinds:
        raise ContractViolation("predicate_object_kind_denied")
    if sensitivity not in rule.sensitivities:
        raise ContractViolation("predicate_sensitivity_denied")
    if epistemic not in rule.epistemic_statuses:
        raise ContractViolation("predicate_epistemic_status_denied")
    return {
        **subject,
        "predicate": predicate,
        **object_row,
        "epistemic_state": epistemic.value,
        "sensitivity": sensitivity.value,
    }


def _request_material(request: Mapping[str, Any]) -> dict[str, object]:
    fields = (
        "owner_user_id",
        "job_id",
        "evidence_id",
        "provider_call_id",
        "operation_id",
        "source_kind",
        "source_message_id",
        "source_thread_id",
        "source_window_id",
        "window_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "context_message_id",
        "context_sha256",
        "start_utf8",
        "end_utf8",
        "attempt_number",
        "max_output_tokens",
        "model",
        "schema",
        "predicate_catalog_sha256",
        "external_payload_sha256",
    )
    try:
        return {field: request[field] for field in fields}
    except KeyError as exc:
        raise ContractViolation("provider_request_missing_field") from exc


def _validated_transient_context(
    value: Mapping[str, Any] | None,
    *,
    context_sha256: str | None,
) -> dict[str, object] | None:
    if context_sha256 is None:
        if value is not None:
            raise ContractViolation("unexpected_provider_context")
        return None
    row = _closed_mapping(
        value,
        required=frozenset({"role", "content", "content_sha256"}),
        code="invalid_provider_context",
    )
    if row["role"] != "assistant":
        raise ContractViolation("invalid_provider_context_role")
    content = require_bounded_text(
        row["content"],
        code="invalid_provider_context_content",
        maximum_bytes=2_000,
    )
    supplied_sha256 = require_sha256(
        row["content_sha256"], "invalid_provider_context_sha256"
    )
    if supplied_sha256 != context_sha256 or sha256_text(content) != supplied_sha256:
        raise ContractViolation("provider_context_sha256_mismatch")
    return {
        "role": "assistant",
        "content": content,
        "content_sha256": supplied_sha256,
    }


def build_provider_request(
    job: Mapping[str, Any],
    evidence: Mapping[str, Any],
    model: str,
    schema: str,
    predicate_catalog: Mapping[str, Any],
    *,
    bounded_context: Mapping[str, Any] | None = None,
    max_output_tokens: int = 4_096,
) -> dict[str, object]:
    job_row = _closed_mapping(
        job,
        required=frozenset(
            {
                "owner_user_id",
                "job_id",
                "evidence_id",
                "provider_call_id",
                "source_sha256",
                "state",
                "attempt_number",
                "operation_id",
            }
        ),
        optional=frozenset({"fixture_provenance"}),
        code="invalid_extraction_job",
    )
    evidence_row = _closed_mapping(
        evidence,
        required=frozenset(
            {
                "owner_user_id",
                "evidence_id",
                "source_kind",
                "source_message_id",
                "source_thread_id",
                "source_window_id",
                "start_utf8",
                "end_utf8",
                "selected_text",
                "selected_sha256",
                "source_sha256",
                "window_sha256",
                "context_message_id",
                "context_sha256",
                "selection_binding_sha256",
                "category",
                "assertion_mode",
                "subject_hint",
                "sensitivity",
            }
        ),
        optional=frozenset({"fixture_provenance"}),
        code="invalid_selected_evidence",
    )
    catalog = parse_predicate_catalog(predicate_catalog)
    owner = _uuid(job_row["owner_user_id"], "invalid_extraction_owner")
    if _uuid(evidence_row["owner_user_id"], "invalid_evidence_owner") != owner:
        raise ContractViolation("cross_owner_extraction")
    evidence_id = _uuid(job_row["evidence_id"], "invalid_extraction_evidence")
    if _uuid(evidence_row["evidence_id"], "invalid_selected_evidence_id") != evidence_id:
        raise ContractViolation("extraction_evidence_id_mismatch")
    if job_row["state"] != ExtractionJobState.CLAIMED.value:
        raise ContractViolation("extraction_job_not_claimed")
    source_sha256 = require_sha256(
        job_row["source_sha256"], "invalid_extraction_source_sha256"
    )
    if source_sha256 != require_sha256(
        evidence_row["source_sha256"], "invalid_selected_source_sha256"
    ):
        raise ContractViolation("extraction_source_sha256_mismatch")
    selected_sha256 = require_sha256(
        evidence_row["selected_sha256"], "invalid_selected_evidence_sha256"
    )
    selected_text = require_bounded_text(
        evidence_row["selected_text"],
        code="invalid_selected_text",
        maximum_bytes=16_000,
    )
    if sha256_text(selected_text) != selected_sha256:
        raise ContractViolation("selected_text_sha256_mismatch")
    start = require_exact_int(
        evidence_row["start_utf8"],
        code="invalid_selected_start",
        maximum=16_000_000,
    )
    end = require_exact_int(
        evidence_row["end_utf8"],
        code="invalid_selected_end",
        maximum=16_000_000,
    )
    if end <= start or end - start != len(selected_text.encode("utf-8")):
        raise ContractViolation("selected_evidence_range_mismatch")
    source_kind = require_key(
        evidence_row["source_kind"], "invalid_selected_source_kind"
    )
    if source_kind != "conversation_message":
        raise ContractViolation("unsupported_selected_source_kind")
    source_message_id = _uuid(
        evidence_row["source_message_id"], "invalid_selected_source_message"
    )
    source_thread_id = _uuid(
        evidence_row["source_thread_id"], "invalid_selected_source_thread"
    )
    source_window_id = _uuid(
        evidence_row["source_window_id"], "invalid_selected_source_window"
    )
    window_sha256 = require_sha256(
        evidence_row["window_sha256"], "invalid_selected_window_sha256"
    )
    context_id_value = evidence_row["context_message_id"]
    context_sha_value = evidence_row["context_sha256"]
    if (context_id_value is None) != (context_sha_value is None):
        raise ContractViolation("incomplete_selected_context_binding")
    context_message_id = (
        _uuid(context_id_value, "invalid_selected_context_message")
        if context_id_value is not None
        else None
    )
    context_sha256 = (
        require_sha256(context_sha_value, "invalid_selected_context_sha256")
        if context_sha_value is not None
        else None
    )
    expected_selection_binding = selection_binding_sha256(
        owner_user_id=owner,
        source_kind=source_kind,
        source_message_id=source_message_id,
        source_thread_id=source_thread_id,
        source_window_id=source_window_id,
        window_sha256=window_sha256,
        source_sha256=source_sha256,
        selected_sha256=selected_sha256,
        start_utf8=start,
        end_utf8=end,
        context_message_id=context_message_id,
        context_sha256=context_sha256,
    )
    supplied_selection_binding = require_sha256(
        evidence_row["selection_binding_sha256"],
        "invalid_selected_selection_binding_sha256",
    )
    if supplied_selection_binding != expected_selection_binding:
        raise ContractViolation("selection_binding_sha256_mismatch")
    transient_context = _validated_transient_context(
        bounded_context,
        context_sha256=context_sha256,
    )
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise ContractViolation("invalid_provider_model")
    require_key(schema, "invalid_provider_schema")
    external_payload: dict[str, object] = {
        "schema": schema,
        "selected_text": selected_text,
        "selected_sha256": selected_sha256,
        "bounded_context": transient_context,
        "predicate_catalog": catalog.external_material(),
        "result_contract": {
            "top_level_keys": ["facts", "schema", "usage"],
            "fact_keys": [
                "epistemic_status",
                "object",
                "predicate",
                "selected_sha256",
                "sensitivity",
                "subject",
            ],
            "subject_contract": {
                "self": {"kind": "self"},
                "non_self_keys": ["kind", "label"],
            },
            "object_contract": {
                "literal_keys": ["kind", "value"],
                "entity_keys": ["entity_type", "kind", "label"],
            },
            "minimum_facts": 0,
            "maximum_facts": 8,
            "usage_keys": ["input_tokens", "output_tokens"],
        },
    }
    external_payload_sha256 = canonical_sha256(
        "governed_memory.external_extraction_payload", external_payload
    )
    request: dict[str, object] = {
        "owner_user_id": str(owner),
        "job_id": str(_uuid(job_row["job_id"], "invalid_extraction_job_id")),
        "evidence_id": str(evidence_id),
        "provider_call_id": str(
            _uuid(job_row["provider_call_id"], "invalid_extraction_provider_call")
        ),
        "operation_id": str(
            _uuid(job_row["operation_id"], "invalid_extraction_operation")
        ),
        "source_kind": source_kind,
        "source_message_id": str(source_message_id),
        "source_thread_id": str(source_thread_id),
        "source_window_id": str(source_window_id),
        "window_sha256": window_sha256,
        "source_sha256": source_sha256,
        "selected_sha256": selected_sha256,
        "selection_binding_sha256": supplied_selection_binding,
        "context_message_id": (
            str(context_message_id) if context_message_id is not None else None
        ),
        "context_sha256": context_sha256,
        "start_utf8": start,
        "end_utf8": end,
        "attempt_number": require_exact_int(
            job_row["attempt_number"],
            code="invalid_extraction_attempt",
            minimum=1,
            maximum=100,
        ),
        "max_output_tokens": require_exact_int(
            max_output_tokens,
            code="invalid_provider_max_output_tokens",
            minimum=1,
            maximum=100_000,
        ),
        "model": model,
        "schema": schema,
        "predicate_catalog_sha256": catalog.catalog_sha256,
        "external_payload": external_payload,
        "external_payload_sha256": external_payload_sha256,
    }
    request["request_sha256"] = canonical_sha256(
        "governed_memory.provider_request", _request_material(request)
    )
    return request


def _validated_request(request: Mapping[str, Any]) -> ProviderRequestBinding:
    row = _closed_mapping(
        request,
        required=frozenset(
            {
                "owner_user_id",
                "job_id",
                "evidence_id",
                "provider_call_id",
                "operation_id",
                "source_kind",
                "source_message_id",
                "source_thread_id",
                "source_window_id",
                "window_sha256",
                "source_sha256",
                "selected_sha256",
                "selection_binding_sha256",
                "context_message_id",
                "context_sha256",
                "start_utf8",
                "end_utf8",
                "attempt_number",
                "max_output_tokens",
                "model",
                "schema",
                "predicate_catalog_sha256",
                "external_payload",
                "external_payload_sha256",
                "request_sha256",
            }
        ),
        code="invalid_provider_request",
    )
    expected = canonical_sha256(
        "governed_memory.provider_request", _request_material(row)
    )
    if row["request_sha256"] != expected:
        raise ContractViolation("provider_request_sha256_mismatch")
    external = _closed_mapping(
        row["external_payload"],
        required=frozenset(
            {
                "schema",
                "selected_text",
                "selected_sha256",
                "bounded_context",
                "predicate_catalog",
                "result_contract",
            }
        ),
        code="invalid_external_payload",
    )
    external_sha256 = canonical_sha256(
        "governed_memory.external_extraction_payload", external
    )
    if row["external_payload_sha256"] != external_sha256:
        raise ContractViolation("external_payload_sha256_mismatch")
    for key in ("schema", "selected_sha256"):
        if external[key] != row[key]:
            raise ContractViolation("external_payload_binding_mismatch")
    expected_result_contract = {
        "top_level_keys": ["facts", "schema", "usage"],
        "fact_keys": [
            "epistemic_status",
            "object",
            "predicate",
            "selected_sha256",
            "sensitivity",
            "subject",
        ],
        "subject_contract": {
            "self": {"kind": "self"},
            "non_self_keys": ["kind", "label"],
        },
        "object_contract": {
            "literal_keys": ["kind", "value"],
            "entity_keys": ["entity_type", "kind", "label"],
        },
        "minimum_facts": 0,
        "maximum_facts": 8,
        "usage_keys": ["input_tokens", "output_tokens"],
    }
    if external["result_contract"] != expected_result_contract:
        raise ContractViolation("external_result_contract_mismatch")
    selected_text = external["selected_text"]
    if not isinstance(selected_text, str) or sha256_text(selected_text) != row[
        "selected_sha256"
    ]:
        raise ContractViolation("provider_request_selected_text_mismatch")
    catalog = _catalog_from_external(external["predicate_catalog"])
    if catalog.catalog_sha256 != row["predicate_catalog_sha256"]:
        raise ContractViolation("provider_request_catalog_sha256_mismatch")
    context_message_id = (
        _uuid(row["context_message_id"], "invalid_provider_context_message")
        if row["context_message_id"] is not None
        else None
    )
    context_sha256 = (
        require_sha256(row["context_sha256"], "invalid_provider_context_sha256")
        if row["context_sha256"] is not None
        else None
    )
    _validated_transient_context(
        external["bounded_context"],
        context_sha256=context_sha256,
    )
    binding = ProviderRequestBinding(
        owner_user_id=_uuid(row["owner_user_id"], "invalid_provider_owner"),
        job_id=_uuid(row["job_id"], "invalid_provider_job"),
        evidence_id=_uuid(row["evidence_id"], "invalid_provider_evidence"),
        provider_call_id=_uuid(
            row["provider_call_id"], "invalid_provider_call"
        ),
        operation_id=_uuid(row["operation_id"], "invalid_provider_operation"),
        source_kind=str(row["source_kind"]),
        source_message_id=_uuid(
            row["source_message_id"], "invalid_provider_source_message"
        ),
        source_thread_id=_uuid(
            row["source_thread_id"], "invalid_provider_source_thread"
        ),
        source_window_id=_uuid(
            row["source_window_id"], "invalid_provider_source_window"
        ),
        window_sha256=str(row["window_sha256"]),
        source_sha256=str(row["source_sha256"]),
        selected_sha256=str(row["selected_sha256"]),
        selection_binding_sha256=str(row["selection_binding_sha256"]),
        start_utf8=row["start_utf8"],
        end_utf8=row["end_utf8"],
        context_message_id=context_message_id,
        context_sha256=context_sha256,
        attempt_number=row["attempt_number"],
        max_output_tokens=row["max_output_tokens"],
        model=str(row["model"]),
        schema=str(row["schema"]),
        predicate_catalog_sha256=str(row["predicate_catalog_sha256"]),
        external_payload_sha256=str(row["external_payload_sha256"]),
        request_sha256=str(row["request_sha256"]),
    )
    expected_selection = selection_binding_sha256(
        owner_user_id=binding.owner_user_id,
        source_kind=binding.source_kind,
        source_message_id=binding.source_message_id,
        source_thread_id=binding.source_thread_id,
        source_window_id=binding.source_window_id,
        window_sha256=binding.window_sha256,
        source_sha256=binding.source_sha256,
        selected_sha256=binding.selected_sha256,
        start_utf8=binding.start_utf8,
        end_utf8=binding.end_utf8,
        context_message_id=binding.context_message_id,
        context_sha256=binding.context_sha256,
    )
    if binding.selection_binding_sha256 != expected_selection:
        raise ContractViolation("provider_request_selection_binding_mismatch")
    return binding


COMPLETE_EXTRACTION_ITEM_FIELDS = (
    "epistemic_state",
    "fact_index",
    "object_display_name",
    "object_entity_key",
    "object_entity_type",
    "object_kind",
    "object_literal",
    "operation_id",
    "predicate",
    "proposal_id",
    "proposal_sha256",
    "semantic_key_sha256",
    "sensitivity",
    "subject_display_name",
    "subject_entity_key",
    "subject_entity_type",
)

PROPOSAL_HASH_BINDING_FIELDS = (
    "owner_user_id",
    "job_id",
    "evidence_id",
    "provider_call_id",
    "source_kind",
    "source_message_id",
    "source_thread_id",
    "source_window_id",
    "window_sha256",
    "source_sha256",
    "selected_sha256",
    "selection_binding_sha256",
    "context_message_id",
    "context_sha256",
    "predicate_catalog_sha256",
    "request_sha256",
    "response_sha256",
    "purpose",
    "correction_target_claim_id",
    "correction_target_revision_id",
    "correction_target_revision_number",
    "correction_target_revision_sha256",
    "correction_target_state_sha256",
    "correction_target_identity_sha256",
    "correction_target_projection_sequence",
    "correction_pending_state_sha256",
)


def _text_or_none(value: object, code: str) -> str | None:
    if value is None:
        return None
    return require_bounded_text(value, code=code, maximum_bytes=2_000)


def policy_for_sensitivity(value: object) -> dict[str, object]:
    if value == "restricted_identifier":
        raise ContractViolation("restricted_identifier_not_projectable")
    try:
        sensitivity = Sensitivity(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_proposal_sensitivity") from exc
    ordinary = sensitivity is Sensitivity.ORDINARY
    return {
        "projectable": True,
        "domains": [],
        "intents": [],
        "surface": "normal" if ordinary else "explicit_only",
        "requires_explicit": not ordinary,
        "valid_from": None,
        "valid_to": None,
    }


def derive_source_local_entity_key(
    *,
    owner_user_id: UUID,
    selection_binding: str,
    fact_index: int,
    role: str,
    entity_type: EntityKind | str,
) -> str:
    require_uuid(owner_user_id, "invalid_entity_key_owner")
    binding = require_sha256(
        selection_binding, "invalid_entity_key_selection_binding"
    )
    index = require_exact_int(
        fact_index, code="invalid_entity_key_fact_index", maximum=7
    )
    if role not in {"subject", "object"}:
        raise ContractViolation("invalid_entity_key_role")
    try:
        checked_entity_type = EntityKind(entity_type)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_entity_key_type") from exc
    if checked_entity_type is EntityKind.SELF:
        return "self"
    digest = framed_sha256(
        "governed_memory.source_local_entity.v1",
        (
            ("owner_user_id", str(owner_user_id)),
            ("selection_binding_sha256", binding),
            ("fact_index", str(index)),
            ("role", role),
            ("entity_type", checked_entity_type.value),
        ),
    )
    return f"local:{digest}"


ENTITY_KEY_TEST_VECTOR = MappingProxyType(
    {
        "domain": "governed_memory.source_local_entity.v1",
        "ordered_fields": (
            ("owner_user_id", "00000000-0000-0000-0000-000000000001"),
            (
                "selection_binding_sha256",
                "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619",
            ),
            ("fact_index", "0"),
            ("role", "object"),
            ("entity_type", "person"),
        ),
        "sha256": (
            "fd84fb3d7dc64f134e0c7b42df0307be5cc87693a9e58649c94f04cadb253e82"
        ),
        "entity_key": (
            "local:fd84fb3d7dc64f134e0c7b42df0307be5cc87693a9e58649c94f04cadb253e82"
        ),
        "irrelevant_display_name": "Zoë:\n猫",
    }
)


def _materialize_fact(
    normalized: Mapping[str, Any],
    *,
    owner_user_id: UUID,
    selection_binding: str,
    fact_index: int,
) -> dict[str, object]:
    subject_type = EntityKind(normalized["subject_entity_type"])
    object_kind = ObjectKind(normalized["object_kind"])
    subject_key = derive_source_local_entity_key(
        owner_user_id=owner_user_id,
        selection_binding=selection_binding,
        fact_index=fact_index,
        role="subject",
        entity_type=subject_type,
    )
    if object_kind is ObjectKind.ENTITY:
        object_type = EntityKind(normalized["object_entity_type"])
        object_key = derive_source_local_entity_key(
            owner_user_id=owner_user_id,
            selection_binding=selection_binding,
            fact_index=fact_index,
            role="object",
            entity_type=object_type,
        )
    else:
        object_type = None
        object_key = None
    semantic = semantic_key_sha256(
        subject_entity_key=subject_key,
        predicate=str(normalized["predicate"]),
        object_kind=object_kind,
        object_entity_key=object_key,
        object_literal=(
            str(normalized["object_literal"])
            if normalized["object_literal"] is not None
            else None
        ),
    )
    return {
        **normalized,
        "subject_entity_key": subject_key,
        "object_entity_type": object_type.value if object_type else None,
        "object_entity_key": object_key,
        "semantic_key_sha256": semantic,
    }


def validate_proposal_item(
    proposal: Mapping[str, Any],
    *,
    allow_placeholder_hash: bool = False,
    allow_null_fact_index: bool = False,
) -> dict[str, object]:
    if not isinstance(proposal, Mapping) or tuple(sorted(proposal)) != (
        COMPLETE_EXTRACTION_ITEM_FIELDS
    ):
        raise ContractViolation("invalid_complete_extraction_item")
    if proposal["fact_index"] is None and allow_null_fact_index:
        index = None
    else:
        index = require_exact_int(
            proposal["fact_index"], code="invalid_proposal_fact_index", maximum=7
        )
    try:
        subject_type = EntityKind(proposal["subject_entity_type"])
        object_kind = ObjectKind(proposal["object_kind"])
        epistemic = EpistemicStatus(proposal["epistemic_state"])
        sensitivity = Sensitivity(proposal["sensitivity"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_complete_extraction_item") from exc
    subject_key = require_key(
        proposal["subject_entity_key"], "invalid_proposal_subject_entity_key"
    )
    subject_display = _text_or_none(
        proposal["subject_display_name"], "invalid_proposal_subject_display_name"
    )
    if (subject_type is EntityKind.SELF) != (subject_key == "self"):
        raise ContractViolation("invalid_proposal_self_subject_key")
    if subject_type is EntityKind.SELF and subject_display is not None:
        raise ContractViolation("invalid_proposal_self_subject_display")
    if subject_type is not EntityKind.SELF and subject_display is None:
        raise ContractViolation("missing_proposal_subject_display")
    predicate = require_key(proposal["predicate"], "invalid_proposal_predicate")
    object_display = _text_or_none(
        proposal["object_display_name"], "invalid_proposal_object_display_name"
    )
    object_literal = _text_or_none(
        proposal["object_literal"], "invalid_proposal_object_literal"
    )
    if object_kind is ObjectKind.ENTITY:
        try:
            object_type = EntityKind(proposal["object_entity_type"])
        except (TypeError, ValueError) as exc:
            raise ContractViolation("invalid_proposal_object_entity_type") from exc
        object_key = require_key(
            proposal["object_entity_key"], "invalid_proposal_object_entity_key"
        )
        if object_literal is not None or object_display is None:
            raise ContractViolation("invalid_proposal_entity_object")
        if (object_type is EntityKind.SELF) != (object_key == "self"):
            raise ContractViolation("invalid_proposal_self_object_key")
    else:
        if (
            proposal["object_entity_type"] is not None
            or proposal["object_entity_key"] is not None
            or object_display is not None
            or object_literal is None
        ):
            raise ContractViolation("invalid_proposal_literal_object")
        object_type = None
        object_key = None
    semantic = require_sha256(
        proposal["semantic_key_sha256"], "invalid_proposal_semantic_key_sha256"
    )
    expected_semantic = semantic_key_sha256(
        subject_entity_key=subject_key,
        predicate=predicate,
        object_kind=object_kind,
        object_entity_key=object_key,
        object_literal=object_literal,
    )
    if semantic != expected_semantic:
        raise ContractViolation("proposal_semantic_key_sha256_mismatch")
    _uuid(proposal["proposal_id"], "invalid_proposal_id")
    _uuid(proposal["operation_id"], "invalid_proposal_operation")
    proposal_hash = require_sha256(
        proposal["proposal_sha256"], "invalid_proposal_sha256"
    )
    if not allow_placeholder_hash and proposal_hash == "0" * 64:
        raise ContractViolation("placeholder_proposal_sha256")
    return {
        "epistemic_state": epistemic.value,
        "fact_index": index,
        "object_display_name": object_display,
        "object_entity_key": object_key,
        "object_entity_type": object_type.value if object_type else None,
        "object_kind": object_kind.value,
        "object_literal": object_literal,
        "operation_id": str(_uuid(proposal["operation_id"], "invalid_proposal_operation")),
        "predicate": predicate,
        "proposal_id": str(_uuid(proposal["proposal_id"], "invalid_proposal_id")),
        "proposal_sha256": proposal_hash,
        "semantic_key_sha256": semantic,
        "sensitivity": sensitivity.value,
        "subject_display_name": subject_display,
        "subject_entity_key": subject_key,
        "subject_entity_type": subject_type.value,
    }


def _validate_proposal_binding(value: Mapping[str, Any]) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or tuple(sorted(value)) != tuple(
        sorted(PROPOSAL_HASH_BINDING_FIELDS)
    ):
        raise ContractViolation("invalid_proposal_hash_binding")
    result: dict[str, str | None] = {}
    uuid_fields = {
        "owner_user_id",
        "evidence_id",
        "source_message_id",
        "source_thread_id",
        "source_window_id",
    }
    nullable_uuid_fields = {
        "job_id",
        "provider_call_id",
        "context_message_id",
        "correction_target_claim_id",
        "correction_target_revision_id",
    }
    sha_fields = {
        "window_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "predicate_catalog_sha256",
    }
    nullable_sha_fields = {
        "context_sha256",
        "request_sha256",
        "response_sha256",
        "correction_target_revision_sha256",
        "correction_target_state_sha256",
        "correction_target_identity_sha256",
        "correction_pending_state_sha256",
    }
    for field in PROPOSAL_HASH_BINDING_FIELDS:
        raw = value[field]
        if field in uuid_fields:
            result[field] = str(_uuid(raw, f"invalid_proposal_binding_{field}"))
        elif field in nullable_uuid_fields:
            result[field] = (
                str(_uuid(raw, f"invalid_proposal_binding_{field}"))
                if raw is not None
                else None
            )
        elif field in sha_fields:
            result[field] = require_sha256(
                raw, f"invalid_proposal_binding_{field}"
            )
        elif field in nullable_sha_fields:
            result[field] = (
                require_sha256(raw, f"invalid_proposal_binding_{field}")
                if raw is not None
                else None
            )
        elif field in {
            "correction_target_revision_number",
            "correction_target_projection_sequence",
        }:
            result[field] = (
                str(
                    require_exact_int(
                        raw,
                        code=f"invalid_proposal_binding_{field}",
                        minimum=(
                            1
                            if field == "correction_target_revision_number"
                            else 0
                        ),
                    )
                )
                if raw is not None
                else None
            )
        else:
            result[field] = require_key(
                raw, f"invalid_proposal_binding_{field}"
            )
    purpose = result["purpose"]
    new_claim = purpose == ProposalPurpose.NEW_CLAIM.value
    correction = purpose == ProposalPurpose.CORRECTION.value
    if not new_claim and not correction:
        raise ContractViolation("invalid_proposal_binding_purpose")
    new_only = ("job_id", "provider_call_id", "request_sha256", "response_sha256")
    correction_only = (
        "correction_target_claim_id",
        "correction_target_revision_id",
        "correction_target_revision_number",
        "correction_target_revision_sha256",
        "correction_target_state_sha256",
        "correction_target_identity_sha256",
        "correction_target_projection_sequence",
        "correction_pending_state_sha256",
    )
    if new_claim and (
        any(result[field] is None for field in new_only)
        or any(result[field] is not None for field in correction_only)
    ):
        raise ContractViolation("invalid_new_claim_proposal_binding")
    if correction and (
        any(result[field] is not None for field in new_only)
        or any(result[field] is None for field in correction_only)
    ):
        raise ContractViolation("invalid_correction_proposal_binding")
    if (result["context_message_id"] is None) != (
        result["context_sha256"] is None
    ):
        raise ContractViolation("incomplete_proposal_context_binding")
    return result


def recompute_proposal_sha256(
    proposal: Mapping[str, Any], proposal_binding: Mapping[str, Any]
) -> str:
    binding = _validate_proposal_binding(proposal_binding)
    is_correction = binding["purpose"] == ProposalPurpose.CORRECTION.value
    item = validate_proposal_item(
        proposal,
        allow_placeholder_hash=True,
        allow_null_fact_index=is_correction,
    )
    if is_correction != (item["fact_index"] is None):
        raise ContractViolation("proposal_fact_index_purpose_mismatch")
    policy = policy_for_sensitivity(item["sensitivity"])
    ordered: list[tuple[str, str | None]] = [
        (field, binding[field]) for field in PROPOSAL_HASH_BINDING_FIELDS
    ]
    ordered.extend(
        (
            (
                "fact_index",
                str(item["fact_index"])
                if item["fact_index"] is not None
                else None,
            ),
            ("proposal_id", str(item["proposal_id"])),
            ("operation_id", str(item["operation_id"])),
            ("subject_entity_type", str(item["subject_entity_type"])),
            ("subject_entity_key", str(item["subject_entity_key"])),
            ("subject_display_name", item["subject_display_name"]),
            ("predicate", str(item["predicate"])),
            ("object_kind", str(item["object_kind"])),
            ("object_entity_type", item["object_entity_type"]),
            ("object_entity_key", item["object_entity_key"]),
            ("object_display_name", item["object_display_name"]),
            ("object_literal", item["object_literal"]),
            ("epistemic_state", str(item["epistemic_state"])),
            ("sensitivity", str(item["sensitivity"])),
            ("semantic_key_sha256", str(item["semantic_key_sha256"])),
            ("projectable", "true"),
            ("domains", "[]"),
            ("intents", "[]"),
            ("surface", str(policy["surface"])),
            (
                "requires_explicit",
                "true" if policy["requires_explicit"] else "false",
            ),
            ("valid_from", None),
            ("valid_to", None),
        )
    )
    return framed_sha256("governed_memory.proposal.v1", tuple(ordered))


def recompute_extraction_proposal_sha256(
    proposal: Mapping[str, Any], proposal_binding: Mapping[str, Any]
) -> str:
    binding = _validate_proposal_binding(proposal_binding)
    if binding["purpose"] != ProposalPurpose.NEW_CLAIM.value:
        raise ContractViolation("extraction_proposal_purpose_mismatch")
    return recompute_proposal_sha256(proposal, binding)


PROPOSAL_TEST_VECTORS = MappingProxyType(
    {
        "new_claim": MappingProxyType(
            {
                "binding": MappingProxyType(
                    {
                        "owner_user_id": "00000000-0000-0000-0000-000000000001",
                        "job_id": "00000000-0000-0000-0000-000000000006",
                        "evidence_id": "00000000-0000-0000-0000-000000000007",
                        "provider_call_id": "00000000-0000-0000-0000-000000000008",
                        "source_kind": "conversation_message",
                        "source_message_id": "00000000-0000-0000-0000-000000000002",
                        "source_thread_id": "00000000-0000-0000-0000-000000000003",
                        "source_window_id": "00000000-0000-0000-0000-000000000004",
                        "window_sha256": "1" * 64,
                        "source_sha256": "2" * 64,
                        "selected_sha256": "3" * 64,
                        "selection_binding_sha256": (
                            "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619"
                        ),
                        "context_message_id": None,
                        "context_sha256": None,
                        "predicate_catalog_sha256": CANONICAL_PREDICATE_CATALOG_SHA256,
                        "request_sha256": "5" * 64,
                        "response_sha256": "6" * 64,
                        "purpose": "new_claim",
                        "correction_target_claim_id": None,
                        "correction_target_revision_id": None,
                        "correction_target_revision_number": None,
                        "correction_target_revision_sha256": None,
                        "correction_target_state_sha256": None,
                        "correction_target_identity_sha256": None,
                        "correction_target_projection_sequence": None,
                        "correction_pending_state_sha256": None,
                    }
                ),
                "item": MappingProxyType(
                    {
                        "epistemic_state": "supported",
                        "fact_index": 0,
                        "object_display_name": None,
                        "object_entity_key": None,
                        "object_entity_type": None,
                        "object_kind": "literal",
                        "object_literal": "café:\n猫",
                        "operation_id": "00000000-0000-0000-0000-00000000000a",
                        "predicate": "preference.personal",
                        "proposal_id": "00000000-0000-0000-0000-000000000009",
                        "proposal_sha256": (
                            "a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c"
                        ),
                        "semantic_key_sha256": (
                            "3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc"
                        ),
                        "sensitivity": "ordinary",
                        "subject_display_name": None,
                        "subject_entity_key": "self",
                        "subject_entity_type": "self",
                    }
                ),
                "sha256": (
                    "a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c"
                ),
            }
        ),
        "correction": MappingProxyType(
            {
                "binding": MappingProxyType(
                    {
                        "owner_user_id": "00000000-0000-0000-0000-000000000001",
                        "job_id": None,
                        "evidence_id": "00000000-0000-0000-0000-00000000000b",
                        "provider_call_id": None,
                        "source_kind": "owner_correction",
                        "source_message_id": "00000000-0000-0000-0000-00000000000c",
                        "source_thread_id": "00000000-0000-0000-0000-00000000000d",
                        "source_window_id": "00000000-0000-0000-0000-00000000000e",
                        "window_sha256": "7" * 64,
                        "source_sha256": "8" * 64,
                        "selected_sha256": "8" * 64,
                        "selection_binding_sha256": (
                            "2d7c910118d227a1ad151999b63728fcc7e6c8d53c9ddadbd5bd289a37b11ac9"
                        ),
                        "context_message_id": None,
                        "context_sha256": None,
                        "predicate_catalog_sha256": CANONICAL_PREDICATE_CATALOG_SHA256,
                        "request_sha256": None,
                        "response_sha256": None,
                        "purpose": "correction",
                        "correction_target_claim_id": "00000000-0000-0000-0000-000000000006",
                        "correction_target_revision_id": "00000000-0000-0000-0000-000000000007",
                        "correction_target_revision_number": 2,
                        "correction_target_revision_sha256": "9" * 64,
                        "correction_target_state_sha256": "a" * 64,
                        "correction_target_identity_sha256": (
                            "dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1"
                        ),
                        "correction_target_projection_sequence": 4,
                        "correction_pending_state_sha256": "b" * 64,
                    }
                ),
                "item": MappingProxyType(
                    {
                        "epistemic_state": "supported",
                        "fact_index": None,
                        "object_display_name": None,
                        "object_entity_key": None,
                        "object_entity_type": None,
                        "object_kind": "literal",
                        "object_literal": "tea",
                        "operation_id": "fe148132-3436-5202-9895-37133aef69d2",
                        "predicate": "preference.personal",
                        "proposal_id": "75faff1f-5e2c-5525-9483-fe97f4afae69",
                        "proposal_sha256": (
                            "f793cbc8773cfc39b11cd4867d277f3b5957037dc9ccd94b8aff431198d5960f"
                        ),
                        "semantic_key_sha256": (
                            "7246dc1185cc5b11fd5d825111c4e33e62fc083f5df797a98fd0ebbfb9f90e65"
                        ),
                        "sensitivity": "ordinary",
                        "subject_display_name": None,
                        "subject_entity_key": "self",
                        "subject_entity_type": "self",
                    }
                ),
                "sha256": (
                    "f793cbc8773cfc39b11cd4867d277f3b5957037dc9ccd94b8aff431198d5960f"
                ),
            }
        ),
    }
)


def derive_fact_proposal_ids(
    *,
    provider_call_id: UUID,
    fact_index: int,
    normalized_fact: Mapping[str, Any],
    selection_binding: str,
) -> tuple[UUID, UUID]:
    require_uuid(provider_call_id, "invalid_proposal_provider_call")
    index = require_exact_int(
        fact_index, code="invalid_proposal_fact_index", maximum=7
    )
    binding = require_sha256(
        selection_binding, "invalid_proposal_selection_binding_sha256"
    )
    if not isinstance(normalized_fact, Mapping):
        raise ContractViolation("invalid_structured_fact")
    fact_sha256 = framed_sha256(
        "governed_memory.validated_fact.v1",
        (
            ("selection_binding_sha256", binding),
            ("fact_index", str(index)),
            ("subject_entity_type", str(normalized_fact["subject_entity_type"])),
            ("subject_entity_key", str(normalized_fact["subject_entity_key"])),
            ("subject_display_name", normalized_fact["subject_display_name"]),
            ("predicate", str(normalized_fact["predicate"])),
            ("object_kind", str(normalized_fact["object_kind"])),
            ("object_entity_type", normalized_fact["object_entity_type"]),
            ("object_entity_key", normalized_fact["object_entity_key"]),
            ("object_display_name", normalized_fact["object_display_name"]),
            ("object_literal", normalized_fact["object_literal"]),
            ("epistemic_state", str(normalized_fact["epistemic_state"])),
            ("sensitivity", str(normalized_fact["sensitivity"])),
            ("semantic_key_sha256", str(normalized_fact["semantic_key_sha256"])),
        ),
    )
    return (
        uuid5(provider_call_id, f"proposal:{index}:{fact_sha256}"),
        uuid5(provider_call_id, f"admission:{index}:{fact_sha256}"),
    )


def _new_claim_proposal_binding(
    binding: ProviderRequestBinding,
    *,
    response_sha256: str,
    predicate_catalog_sha256: str,
) -> dict[str, object]:
    return {
        "owner_user_id": str(binding.owner_user_id),
        "job_id": str(binding.job_id),
        "evidence_id": str(binding.evidence_id),
        "provider_call_id": str(binding.provider_call_id),
        "source_kind": binding.source_kind,
        "source_message_id": str(binding.source_message_id),
        "source_thread_id": str(binding.source_thread_id),
        "source_window_id": str(binding.source_window_id),
        "window_sha256": binding.window_sha256,
        "source_sha256": binding.source_sha256,
        "selected_sha256": binding.selected_sha256,
        "selection_binding_sha256": binding.selection_binding_sha256,
        "context_message_id": (
            str(binding.context_message_id)
            if binding.context_message_id is not None
            else None
        ),
        "context_sha256": binding.context_sha256,
        "predicate_catalog_sha256": require_sha256(
            predicate_catalog_sha256, "invalid_predicate_catalog_sha256"
        ),
        "request_sha256": binding.request_sha256,
        "response_sha256": require_sha256(
            response_sha256, "invalid_provider_response_sha256"
        ),
        "purpose": ProposalPurpose.NEW_CLAIM.value,
        "correction_target_claim_id": None,
        "correction_target_revision_id": None,
        "correction_target_revision_number": None,
        "correction_target_revision_sha256": None,
        "correction_target_state_sha256": None,
        "correction_target_identity_sha256": None,
        "correction_target_projection_sequence": None,
        "correction_pending_state_sha256": None,
    }


def _validate_provider_fact(
    raw_fact: object,
    *,
    binding: ProviderRequestBinding,
    catalog: PredicateCatalog,
    fact_index: int,
    proposal_binding: Mapping[str, Any],
) -> dict[str, object]:
    row = _closed_mapping(
        raw_fact,
        required=frozenset(
            {
                "subject",
                "predicate",
                "object",
                "epistemic_status",
                "sensitivity",
                "selected_sha256",
            }
        ),
        code="invalid_provider_fact",
    )
    supplied_selected_sha256 = require_sha256(
        row["selected_sha256"],
        "invalid_provider_selected_sha256",
    )
    if supplied_selected_sha256 != binding.selected_sha256:
        raise ContractViolation("provider_result_selected_sha256_mismatch")
    normalized = normalize_catalog_fact(
        {
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "epistemic_status": row["epistemic_status"],
            "sensitivity": row["sensitivity"],
        },
        catalog,
    )
    duplicate_identity = framed_sha256(
        "governed_memory.provider_fact_identity.v1",
        (
            ("selected_sha256", supplied_selected_sha256),
            ("subject_entity_type", str(normalized["subject_entity_type"])),
            ("subject_display_name", normalized["subject_display_name"]),
            ("predicate", str(normalized["predicate"])),
            ("object_kind", str(normalized["object_kind"])),
            ("object_entity_type", normalized["object_entity_type"]),
            ("object_display_name", normalized["object_display_name"]),
            ("object_literal", normalized["object_literal"]),
            ("epistemic_state", str(normalized["epistemic_state"])),
            ("sensitivity", str(normalized["sensitivity"])),
        ),
    )
    materialized = _materialize_fact(
        normalized,
        owner_user_id=binding.owner_user_id,
        selection_binding=binding.selection_binding_sha256,
        fact_index=fact_index,
    )
    fact = StructuredFact(
        subject_entity_type=EntityKind(materialized["subject_entity_type"]),
        subject_entity_key=str(materialized["subject_entity_key"]),
        subject_display_name=materialized["subject_display_name"],
        predicate=str(materialized["predicate"]),
        object_kind=ObjectKind(materialized["object_kind"]),
        object_entity_type=(
            EntityKind(materialized["object_entity_type"])
            if materialized["object_entity_type"] is not None
            else None
        ),
        object_entity_key=materialized["object_entity_key"],
        object_display_name=materialized["object_display_name"],
        object_literal=materialized["object_literal"],
        epistemic_status=EpistemicStatus(materialized["epistemic_state"]),
        sensitivity=Sensitivity(materialized["sensitivity"]),
    )
    proposal_id, operation_id = derive_fact_proposal_ids(
        provider_call_id=binding.provider_call_id,
        fact_index=fact_index,
        normalized_fact=materialized,
        selection_binding=binding.selection_binding_sha256,
    )
    proposal: dict[str, object] = {
        "epistemic_state": materialized["epistemic_state"],
        "fact_index": fact_index,
        "object_display_name": materialized["object_display_name"],
        "object_entity_key": materialized["object_entity_key"],
        "object_entity_type": materialized["object_entity_type"],
        "object_kind": materialized["object_kind"],
        "object_literal": materialized["object_literal"],
        "operation_id": str(operation_id),
        "predicate": materialized["predicate"],
        "proposal_id": str(proposal_id),
        "proposal_sha256": "0" * 64,
        "semantic_key_sha256": materialized["semantic_key_sha256"],
        "sensitivity": materialized["sensitivity"],
        "subject_display_name": materialized["subject_display_name"],
        "subject_entity_key": materialized["subject_entity_key"],
        "subject_entity_type": materialized["subject_entity_type"],
    }
    proposal["proposal_sha256"] = recompute_extraction_proposal_sha256(
        proposal, proposal_binding
    )
    validate_proposal_item(proposal)
    ValidatedProposal(
        proposal_id=proposal_id,
        operation_id=operation_id,
        owner_user_id=binding.owner_user_id,
        job_id=binding.job_id,
        provider_call_id=binding.provider_call_id,
        evidence_id=binding.evidence_id,
        state=ProposalState.PENDING_REVIEW,
        proposal_sha256=str(proposal["proposal_sha256"]),
    )
    return {
        "item": proposal,
        "hash_binding": proposal_binding,
        "fact": fact.material,
        "duplicate_identity_sha256": duplicate_identity,
    }


def validate_provider_result(
    request: Mapping[str, Any], output: Mapping[str, Any]
) -> dict[str, object]:
    binding = _validated_request(request)
    external = request["external_payload"]
    if not isinstance(external, Mapping):
        raise ContractViolation("invalid_external_payload")
    catalog = _catalog_from_external(external["predicate_catalog"])
    envelope = _closed_mapping(
        output,
        required=frozenset({"schema", "facts", "usage"}),
        code="invalid_provider_result",
    )
    if envelope["schema"] != binding.schema:
        raise ContractViolation("provider_result_schema_mismatch")
    facts = envelope["facts"]
    if not isinstance(facts, list) or not 0 <= len(facts) <= 8:
        raise ContractViolation("provider_fact_count_out_of_bounds")
    usage = _closed_mapping(
        envelope["usage"],
        required=frozenset({"input_tokens", "output_tokens"}),
        code="invalid_provider_usage",
    )
    input_tokens = require_exact_int(
        usage["input_tokens"],
        code="invalid_provider_input_tokens",
        maximum=100_000,
    )
    output_tokens = require_exact_int(
        usage["output_tokens"],
        code="invalid_provider_output_tokens",
        maximum=binding.max_output_tokens,
    )
    response_sha256 = canonical_sha256(
        "governed_memory.provider_response", envelope
    )
    proposals: list[dict[str, object]] = []
    seen_facts: set[str] = set()
    proposal_hash_binding = _new_claim_proposal_binding(
        binding,
        response_sha256=response_sha256,
        predicate_catalog_sha256=catalog.catalog_sha256,
    )
    for index, raw_fact in enumerate(facts):
        validated = _validate_provider_fact(
            raw_fact,
            binding=binding,
            catalog=catalog,
            fact_index=index,
            proposal_binding=proposal_hash_binding,
        )
        proposal = validated["item"]
        hash_binding = validated["hash_binding"]
        if not isinstance(proposal, dict) or not isinstance(hash_binding, dict):
            raise ContractViolation("invalid_validated_provider_fact")
        if proposal_hash_binding != hash_binding:
            raise ContractViolation("inconsistent_proposal_hash_binding")
        identity = require_sha256(
            validated["duplicate_identity_sha256"],
            "invalid_provider_fact_identity_sha256",
        )
        if identity in seen_facts:
            raise ContractViolation("duplicate_provider_fact")
        seen_facts.add(identity)
        proposals.append(proposal)
    completion = ProviderCompletionBinding(
        provider_call_id=binding.provider_call_id,
        request_sha256=binding.request_sha256,
        response_sha256=response_sha256,
    )
    receipt_material: dict[str, object] = {
        "owner_user_id_sha256": canonical_sha256(
            "governed_memory.owner", str(binding.owner_user_id)
        ),
        "job_id": str(binding.job_id),
        "evidence_id": str(binding.evidence_id),
        "provider_call_id": str(completion.provider_call_id),
        "request_sha256": completion.request_sha256,
        "external_payload_sha256": binding.external_payload_sha256,
        "response_sha256": completion.response_sha256,
        "selection_binding_sha256": binding.selection_binding_sha256,
        "predicate_catalog_sha256": catalog.catalog_sha256,
        "proposal_ids": [proposal["proposal_id"] for proposal in proposals],
        "operation_ids": [proposal["operation_id"] for proposal in proposals],
        "proposal_sha256s": [proposal["proposal_sha256"] for proposal in proposals],
        "proposal_count": len(proposals),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider_outcome": "completed",
        "external_model_calls": 1,
    }
    receipt = dict(receipt_material)
    receipt["receipt_sha256"] = canonical_sha256(
        "governed_memory.provider_completion_receipt", receipt_material
    )
    return {
        "proposals": tuple(proposals),
        "proposal_hash_binding": proposal_hash_binding,
        "provider_call_id": str(binding.provider_call_id),
        "response_sha256": response_sha256,
        "predicate_catalog_sha256": catalog.catalog_sha256,
        "receipt": receipt,
    }


__all__ = [
    "CANONICAL_PREDICATE_CATALOG_SHA256",
    "COMPLETE_EXTRACTION_ITEM_FIELDS",
    "EntityKind",
    "ENTITY_KEY_TEST_VECTOR",
    "PredicateCatalog",
    "PredicateRule",
    "ProposalPurpose",
    "ProviderCompletionBinding",
    "ProviderRequestBinding",
    "PROPOSAL_HASH_BINDING_FIELDS",
    "PROPOSAL_TEST_VECTORS",
    "Sensitivity",
    "StructuredFact",
    "ValidatedProposal",
    "build_provider_request",
    "derive_fact_proposal_ids",
    "derive_source_local_entity_key",
    "normalize_catalog_fact",
    "parse_predicate_catalog",
    "policy_for_sensitivity",
    "recompute_extraction_proposal_sha256",
    "recompute_proposal_sha256",
    "validate_provider_result",
    "validate_proposal_item",
]
