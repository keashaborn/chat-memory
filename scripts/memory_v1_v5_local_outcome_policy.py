#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


POLICY_VERSION: Final = "memory_v1_v5_local_outcome_policy_v2"

RECORD_TERMINAL_CODES: Final = frozenset(
    {
        "ambiguous_transcription",
        "compound_requires_split",
        "context_coreference_unresolved",
        "context_missing",
        "entity_resolution_review_required",
        "entity_resolution_unresolved",
        "insufficient_durable_evidence",
        "insufficient_evidence",
        "local_validation_rejected",
        "mixed_authorship",
        "nothing_durable_to_stage",
        "ordinary_semantic_rejection",
        "predicate_review_required",
        "project_scope_unresolved",
        "question_only",
        "semantic_validation_rejected",
        "sensitive_manual_review",
        "sensitive_or_ambiguous_review",
        "structured_domain",
        "transient_state",
        "unregistered_predicate",
    }
)

SYSTEMIC_THRESHOLD_CODES: Final = frozenset(
    {
        "duplicate_entity_ref",
        "duplicate_observation_ref",
        "duplicate_reason_codes",
        "duplicate_source_span",
        "call_budget_invalid",
        "calendar_range_invalid",
        "calendar_temporal_basis_mismatch",
        "comparison_observation_ref_unknown",
        "dangling_object_entity_ref",
        "dangling_subject_entity_ref",
        "deferral_budget_exceeded",
        "external_calls_disabled",
        "implicit_source_time_shape_invalid",
        "instant_range_invalid",
        "instant_temporal_basis_mismatch",
        "invalid_reason_code",
        "invalid_structured_output",
        "local_completion_rejected",
        "local_incomplete_response",
        "local_model_alias_mismatch",
        "local_persistence_contract_mismatch",
        "local_persistence_rejected",
        "local_provider_disabled",
        "local_reasoning_content_forbidden",
        "local_response_choice_count_invalid",
        "local_response_content_invalid",
        "local_response_invalid_json",
        "local_response_message_invalid",
        "local_response_metadata_invalid",
        "local_response_shape_invalid",
        "local_response_too_large",
        "local_response_usage_invalid",
        "local_structured_content_invalid",
        "local_structured_enum_invalid",
        "local_structured_extra_field",
        "local_structured_required_field_missing",
        "local_structured_type_invalid",
        "local_validation_internal_error",
        "local_transport_auth_rejected",
        "local_transport_http_rejected",
        "local_transport_rate_limited",
        "local_transport_server_error",
        "local_transport_timeout",
        "local_transport_unavailable",
        "normalized_schema_violation",
        "named_entity_missing_name_text",
        "ownership_namespace_forbidden",
        "predicate_registry_violation",
        "provider_call_budget_exceeded",
        "provider_call_count_invalid",
        "provider_call_count_regressed",
        "provider_version_not_enabled",
        "source_binding_invalid",
        "source_span_out_of_bounds",
        "source_span_quote_ambiguous",
        "source_span_quote_mismatch",
        "self_entity_role_mismatch",
        "temporal_none_shape_invalid",
        "temporal_value_cardinality_invalid",
        "trusted_source_time_asserted_by_provider",
        "uncatalogued_validator_rejection",
        "month_precision_range_missing",
        "recurring_temporal_basis_mismatch",
        "relative_source_form_invalid",
        "relative_temporal_basis_mismatch",
    }
)

SYSTEMIC_IMMEDIATE_CODES: Final = frozenset(
    {
        "local_compiler_hash_mismatch",
        "local_model_hash_mismatch",
        "local_owner_scope_violation",
        "local_rls_invariant_failed",
        "local_runtime_hash_mismatch",
        "local_security_invariant_failed",
    }
)

NEUTRAL_CODES: Final = frozenset(
    {
        "local_worker_abandoned",
    }
)

CONTROL_CODES: Final = frozenset(
    {
        "circuit_open",
        "quota_exhausted",
    }
)

NORMALIZED_REASON_CODES: Final = {
    "context_coreference_unresolved": "context_reference_unresolved",
    "local_validation_rejected": "semantic_validation_rejected_legacy",
    "insufficient_evidence": "insufficient_durable_evidence",
    "sensitive_manual_review": "sensitive_or_ambiguous_review",
    "entity_resolution_unresolved": "entity_resolution_review_required",
    "unregistered_predicate": "predicate_review_required",
}

REVIEW_REQUIRED_CODES: Final = frozenset(
    {
        "compound_requires_split",
        "entity_resolution_review_required",
        "entity_resolution_unresolved",
        "mixed_authorship",
        "predicate_review_required",
        "project_scope_unresolved",
        "sensitive_manual_review",
        "sensitive_or_ambiguous_review",
        "unregistered_predicate",
    }
)

DEFERRED_CODES: Final = frozenset(
    {
        "ambiguous_transcription",
        "context_coreference_unresolved",
        "context_missing",
    }
)


@dataclass(frozen=True)
class OutcomePolicy:
    outcome_class: str
    normalized_reason_code: str | None
    disposition: str | None
    circuit_impact: bool
    immediate_open: bool


def classify_rejection_code(code: str | None) -> OutcomePolicy:
    if code is None:
        return OutcomePolicy("success", None, None, False, False)
    normalized = NORMALIZED_REASON_CODES.get(code, code)
    if code in RECORD_TERMINAL_CODES:
        if code in REVIEW_REQUIRED_CODES:
            disposition = "review_required"
        elif code in DEFERRED_CODES:
            disposition = "deferred"
        else:
            disposition = "skipped"
        return OutcomePolicy(
            "record_terminal", normalized, disposition, False, False
        )
    if code in NEUTRAL_CODES:
        return OutcomePolicy("neutral", normalized, None, False, False)
    if code in CONTROL_CODES:
        return OutcomePolicy("control", normalized, None, False, False)
    if code in SYSTEMIC_THRESHOLD_CODES:
        return OutcomePolicy(
            "systemic_threshold", normalized, None, True, False
        )
    if code in SYSTEMIC_IMMEDIATE_CODES:
        return OutcomePolicy(
            "systemic_immediate", normalized, None, True, True
        )
    return OutcomePolicy(
        "systemic_unclassified",
        "unclassified_failure",
        None,
        True,
        True,
    )
