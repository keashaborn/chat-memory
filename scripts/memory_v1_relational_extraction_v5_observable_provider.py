#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    ProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    RelationalExtractionProvider,
    TrustedExtractionSource,
    TrustedProjectBinding,
    ValidatedProviderResult,
    canonical_sha256,
    sha256_text,
    validate_and_normalize,
)


VALIDATOR_REJECTION_PREFIXES: tuple[tuple[str, str], ...] = (
    (
        "provider cannot assert trusted source-time anchoring",
        "trusted_source_time_asserted_by_provider",
    ),
    ("source span is out of bounds:", "source_span_out_of_bounds"),
    ("source span quote mismatch:", "source_span_quote_mismatch"),
    ("duplicate source span:", "duplicate_source_span"),
    ("duplicate entity_ref:", "duplicate_entity_ref"),
    ("named entity requires name_text:", "named_entity_missing_name_text"),
    ("self entity requires user:self role:", "self_entity_role_mismatch"),
    ("duplicate observation_ref:", "duplicate_observation_ref"),
    ("dangling subject_entity_ref:", "dangling_subject_entity_ref"),
    ("dangling object entity_ref:", "dangling_object_entity_ref"),
    ("unregistered predicate:", "unregistered_predicate"),
    ("predicate registry violation for", "predicate_registry_violation"),
    (
        "comparison hint references an unknown observation_ref",
        "comparison_observation_ref_unknown",
    ),
    ("normalized deferrals exceed V5 packet budget", "deferral_budget_exceeded"),
    ("calendar temporal range is empty or reversed", "calendar_range_invalid"),
    ("instant temporal range is empty or reversed", "instant_range_invalid"),
    (
        "implicit source time requires an instant or open instant range",
        "implicit_source_time_shape_invalid",
    ),
    ("temporal none shape is inconsistent", "temporal_none_shape_invalid"),
    (
        "temporal must contain exactly one compatible value",
        "temporal_value_cardinality_invalid",
    ),
    ("instant temporal basis/value mismatch", "instant_temporal_basis_mismatch"),
    ("calendar temporal basis/value mismatch", "calendar_temporal_basis_mismatch"),
    ("relative temporal basis/value mismatch", "relative_temporal_basis_mismatch"),
    ("recurring temporal basis/value mismatch", "recurring_temporal_basis_mismatch"),
    ("relative source form must retain relative basis", "relative_source_form_invalid"),
    ("month precision requires a calendar range", "month_precision_range_missing"),
    ("normalized source envelope is not trusted-source bound", "source_binding_invalid"),
    ("normalized packet contains an ownership namespace", "ownership_namespace_forbidden"),
    (
        "normalized packet violates the authoritative V5 schema:",
        "normalized_schema_violation",
    ),
    ("extraction provider/version is not enabled", "provider_version_not_enabled"),
    ("external model call budget must be between 0 and 4", "call_budget_invalid"),
    ("external model calls are disabled", "external_calls_disabled"),
    ("provider external model call count is invalid", "provider_call_count_invalid"),
    ("provider external model call count regressed", "provider_call_count_regressed"),
    ("provider exceeded the external model call budget", "provider_call_budget_exceeded"),
)


class CapturingProvider:
    """Capture one validated provider packet without altering call behavior."""

    def __init__(self, delegate: RelationalExtractionProvider) -> None:
        self._delegate = delegate
        self.last_packet: ProviderPacket | None = None

    @property
    def provider_id(self) -> str:
        return self._delegate.provider_id

    @property
    def provider_version(self) -> str:
        return self._delegate.provider_version

    @property
    def external_model_calls(self) -> int:
        return int(self._delegate.external_model_calls)

    @property
    def external_call_capability(self) -> bool:
        return bool(self._delegate.external_call_capability)

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        packet = self._delegate.extract(source)
        self.last_packet = packet.model_copy(deep=True)
        return packet


@dataclass(frozen=True)
class ObservableValidationOutcome:
    passed: bool
    provider_id: str
    provider_version: str
    external_model_calls: int
    provider_output_sha256: str | None
    sanitized_provider_packet: dict[str, Any] | None
    validated_result: ValidatedProviderResult | None
    rejection: dict[str, Any] | None

    def audit_record(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "provider": {
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "external_model_calls": self.external_model_calls,
                "provider_output_sha256": self.provider_output_sha256,
            },
            "sanitized_provider_packet": self.sanitized_provider_packet,
            "normalized_packet_sha256": (
                self.validated_result.normalized_packet_sha256
                if self.validated_result is not None
                else None
            ),
            "manual_review_required": (
                self.validated_result.manual_review_required
                if self.validated_result is not None
                else None
            ),
            "rejection": self.rejection,
        }


def validate_and_normalize_observable(
    provider: RelationalExtractionProvider,
    *,
    source: TrustedExtractionSource,
    registry: dict[str, Any],
    schema: dict[str, Any],
    trusted_project_binding: TrustedProjectBinding | None = None,
    allowed_provider_versions: Mapping[str, str],
    max_external_model_calls: int,
) -> ObservableValidationOutcome:
    """Run once and retain only a non-content diagnostic projection."""

    calls_before = int(provider.external_model_calls)
    capturing = CapturingProvider(provider)
    try:
        result = validate_and_normalize(
            capturing,
            source=source,
            registry=registry,
            schema=schema,
            trusted_project_binding=trusted_project_binding,
            allowed_provider_versions=allowed_provider_versions,
            max_external_model_calls=max_external_model_calls,
        )
    except ProviderAdapterError as exc:
        return _failure_outcome(
            capturing,
            calls_before=calls_before,
            category="provider_adapter",
            stage="provider_transport_or_parse",
            code=exc.code,
            retryable=exc.retryable,
            http_status=exc.http_status,
            message=str(exc),
        )
    except ValueError as exc:
        return _failure_outcome(
            capturing,
            calls_before=calls_before,
            category="deterministic_validator",
            stage=(
                "post_provider_validation"
                if capturing.last_packet is not None
                else "pre_provider_validation"
            ),
            code=classify_validator_rejection(str(exc)),
            retryable=False,
            http_status=None,
            message=str(exc),
        )

    packet = capturing.last_packet
    if packet is None:
        raise AssertionError("validated provider result lost its provider packet")
    raw = packet.model_dump(mode="json")
    return ObservableValidationOutcome(
        passed=True,
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        external_model_calls=result.external_model_calls,
        provider_output_sha256=canonical_sha256(raw),
        sanitized_provider_packet=sanitize_provider_packet(packet),
        validated_result=result,
        rejection=None,
    )


def classify_validator_rejection(message: str) -> str:
    for prefix, code in VALIDATOR_REJECTION_PREFIXES:
        if message.startswith(prefix):
            return code
    if "contains duplicate reason codes" in message:
        return "duplicate_reason_codes"
    if "contains an invalid reason code" in message:
        return "invalid_reason_code"
    return "uncatalogued_validator_rejection"


def sanitize_provider_packet(packet: ProviderPacket) -> dict[str, Any]:
    raw = packet.model_dump(mode="json")
    return {
        "entity_mentions": [
            {
                "entity_ref": item["entity_ref"],
                "entity_type": item["entity_type"],
                "mention_kind": item["mention_kind"],
                "name_text": _text_fingerprint(item["name_text"]),
                "relationship_role": _text_fingerprint(item["relationship_role"]),
                "source_spans": _sanitize_spans(item["source_spans"]),
                "extraction_confidence": item["extraction_confidence"],
                "reason_codes": _fingerprint_text_list(item["reason_codes"]),
            }
            for item in raw["entity_mentions"]
        ],
        "observations": [
            {
                "observation_ref": item["observation_ref"],
                "subject_entity_ref": item["subject_entity_ref"],
                "predicate": item["predicate"],
                "object": _sanitize_object(item["object"]),
                "polarity": item["polarity"],
                "modality": item["modality"],
                "projection_class": item["projection_class"],
                "surface_policy": item["surface_policy"],
                "temporal": _sanitize_temporal(item["temporal"]),
                "sensitivity": item["sensitivity"],
                "extraction_confidence": item["extraction_confidence"],
                "source_spans": _sanitize_spans(item["source_spans"]),
                "reason_codes": _fingerprint_text_list(item["reason_codes"]),
            }
            for item in raw["observations"]
        ],
        "comparison_hints": [
            {
                "observation_ref": item["observation_ref"],
                "relation_type": item["relation_type"],
                "target_lookup_key": _text_fingerprint(item["target_lookup_key"]),
                "reason_codes": _fingerprint_text_list(item["reason_codes"]),
            }
            for item in raw["comparison_hints"]
        ],
        "deferrals": [
            {
                "reason_code": item["reason_code"],
                "memory_shape": item["memory_shape"],
                "source_spans": _sanitize_spans(item["source_spans"]),
                "sensitivity": item["sensitivity"],
            }
            for item in raw["deferrals"]
        ],
        "packet_findings": _fingerprint_text_list(raw["packet_findings"]),
    }


def _failure_outcome(
    capturing: CapturingProvider,
    *,
    calls_before: int,
    category: str,
    stage: str,
    code: str,
    retryable: bool,
    http_status: int | None,
    message: str,
) -> ObservableValidationOutcome:
    packet = capturing.last_packet
    raw = packet.model_dump(mode="json") if packet is not None else None
    calls_after = int(capturing.external_model_calls)
    external_model_calls = calls_after - calls_before
    if external_model_calls < 0:
        raise AssertionError("observable provider call count regressed")
    return ObservableValidationOutcome(
        passed=False,
        provider_id=capturing.provider_id,
        provider_version=capturing.provider_version,
        external_model_calls=external_model_calls,
        provider_output_sha256=(canonical_sha256(raw) if raw is not None else None),
        sanitized_provider_packet=(
            sanitize_provider_packet(packet) if packet is not None else None
        ),
        validated_result=None,
        rejection={
            "category": category,
            "stage": stage,
            "code": code,
            "retryable": retryable,
            "http_status": http_status,
            "message_sha256": sha256_text(message),
        },
    )


def _text_fingerprint(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "sha256": sha256_text(value),
        "characters": len(value),
    }


def _value_fingerprint(value: Any) -> dict[str, Any]:
    if value is None:
        value_type = "null"
    elif isinstance(value, bool):
        value_type = "boolean"
    elif isinstance(value, (int, float)):
        value_type = "number"
    elif isinstance(value, str):
        value_type = "string"
    elif isinstance(value, list):
        value_type = "array"
    elif isinstance(value, dict):
        value_type = "object"
    else:
        raise TypeError("provider literal value is not JSON-compatible")
    output: dict[str, Any] = {
        "json_type": value_type,
        "sha256": canonical_sha256(value),
    }
    if isinstance(value, (str, list, dict)):
        output["length"] = len(value)
    return output


def _fingerprint_text_list(values: list[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for value in values:
        fingerprint = _text_fingerprint(value)
        if fingerprint is None:
            raise AssertionError("text fingerprint unexpectedly returned null")
        output.append(fingerprint)
    return output


def _sanitize_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "start": item["start"],
            "end": item["end"],
            "quote": _text_fingerprint(item["quote"]),
        }
        for item in spans
    ]


def _sanitize_object(value: dict[str, Any]) -> dict[str, Any]:
    if value["kind"] == "entity":
        return {
            "kind": "entity",
            "entity_ref": value["entity_ref"],
        }
    return {
        "kind": "literal",
        "datatype": value["datatype"],
        "value": _value_fingerprint(value["value"]),
        "unit": _text_fingerprint(value["unit"]),
        "approximate": value["approximate"],
    }


def _sanitize_temporal(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "semantic": value["semantic"],
        "shape": value["shape"],
        "basis": value["basis"],
        "source_form": value["source_form"],
        "certainty": value["certainty"],
        "precision": value["precision"],
        "instant": _text_fingerprint(value["instant"]),
        "calendar_range": _sanitize_range(value["calendar_range"]),
        "instant_range": _sanitize_range(value["instant_range"]),
        "relative_offset": _sanitize_relative_offset(value["relative_offset"]),
        "recurrence": value["recurrence"],
        "anchored_to_source_time": value["anchored_to_source_time"],
        "reason_codes": _fingerprint_text_list(value["reason_codes"]),
    }


def _sanitize_range(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "lower": _text_fingerprint(value["lower"]),
        "upper": _text_fingerprint(value["upper"]),
        "bounds": value["bounds"],
    }


def _sanitize_relative_offset(
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "direction": value["direction"],
        "magnitude": _value_fingerprint(value["magnitude"]),
        "unit": value["unit"],
        "approximate": value["approximate"],
        "anchor_source": value["anchor_source"],
    }
