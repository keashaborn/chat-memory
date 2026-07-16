#!/usr/bin/env python3
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    validate_and_normalize_observable,
)
from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
    ProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    canonical_json,
    load_registry,
    load_schema,
    sha256_text,
)


REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "c1d613b16795c181780d60219860f8068cee1369b069735db94887b0c1b8b377"
)
CONTENT = "My name is Avery."
SOURCE_TIME = "2026-07-16T20:30:00Z"
ALLOWED_PROVIDER = {OPENAI_PROVIDER_ID: OPENAI_PROVIDER_VERSION}


class FakeOneCallProvider:
    provider_id = OPENAI_PROVIDER_ID
    provider_version = OPENAI_PROVIDER_VERSION
    external_call_capability = True

    def __init__(
        self,
        packet: ProviderPacket | None = None,
        error: ProviderAdapterError | None = None,
    ) -> None:
        if (packet is None) == (error is None):
            raise ValueError("fake provider requires exactly one packet or error")
        self.external_model_calls = 0
        self._packet = packet
        self._error = error

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        if self.external_model_calls != 0:
            raise AssertionError("observable boundary retried the fake provider")
        self.external_model_calls += 1
        if self._error is not None:
            raise self._error
        if self._packet is None:
            raise AssertionError("fake provider packet disappeared")
        return self._packet.model_copy(deep=True)


def provider_packet(
    *,
    anchored_to_source_time: bool = False,
    source_quote: str = CONTENT,
) -> ProviderPacket:
    return ProviderPacket.model_validate(
        {
            "entity_mentions": [
                {
                    "entity_ref": "e01",
                    "entity_type": "self",
                    "mention_kind": "self_reference",
                    "name_text": "Avery",
                    "relationship_role": "user:self",
                    "source_spans": [
                        {"start": 0, "end": 17, "quote": source_quote}
                    ],
                    "extraction_confidence": 1.0,
                    "reason_codes": ["explicit_self_name"],
                }
            ],
            "observations": [
                {
                    "observation_ref": "o01",
                    "subject_entity_ref": "e01",
                    "predicate": "identity.name",
                    "object": {
                        "kind": "literal",
                        "datatype": "text",
                        "value": "Avery",
                        "unit": None,
                        "approximate": False,
                    },
                    "polarity": "affirmed",
                    "modality": "asserted",
                    "projection_class": "direct_claim",
                    "surface_policy": "direct_or_relevant",
                    "temporal": {
                        "semantic": "observation_time",
                        "shape": "instant",
                        "basis": "instant",
                        "source_form": "implicit_source_time",
                        "certainty": "exact",
                        "precision": "minute",
                        "instant": SOURCE_TIME,
                        "calendar_range": None,
                        "instant_range": None,
                        "relative_offset": None,
                        "recurrence": None,
                        "anchored_to_source_time": anchored_to_source_time,
                        "reason_codes": ["implicit_source_time"],
                    },
                    "sensitivity": "medium",
                    "extraction_confidence": 1.0,
                    "source_spans": [
                        {"start": 0, "end": 17, "quote": source_quote}
                    ],
                    "reason_codes": ["explicit_self_name"],
                }
            ],
            "comparison_hints": [],
            "deferrals": [],
            "packet_findings": ["synthetic_observable_test"],
        }
    )


def run(
    packet: ProviderPacket,
    *,
    source: TrustedExtractionSource,
    registry: dict[str, Any],
    schema: dict[str, Any],
):
    provider = FakeOneCallProvider(packet=packet)
    outcome = validate_and_normalize_observable(
        provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions=ALLOWED_PROVIDER,
        max_external_model_calls=1,
    )
    if provider.external_model_calls != 1:
        raise AssertionError("observable boundary did not preserve one-call behavior")
    return outcome


def assert_sanitized(outcome) -> None:
    encoded = canonical_json(outcome.audit_record())
    for forbidden in (
        CONTENT,
        "Avery",
        SOURCE_TIME,
        "user:self",
        "explicit_self_name",
        "synthetic_observable_test",
    ):
        if forbidden in encoded:
            raise AssertionError(f"sanitized audit retained cleartext: {forbidden}")
    if outcome.provider_output_sha256 is None:
        raise AssertionError("provider packet was not fingerprinted")
    if outcome.sanitized_provider_packet is None:
        raise AssertionError("sanitized provider packet was not retained")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    registry = load_registry(
        repo_root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        repo_root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    source = TrustedExtractionSource.create(
        job_id="e4444444-4444-4444-8444-444444444444",
        source_system="public.chat_log",
        source_external_id="e3333333-3333-4333-8333-333333333333",
        source_sha256=sha256_text(CONTENT),
        source_recorded_at=SOURCE_TIME,
        content=CONTENT,
    )

    valid = run(
        provider_packet(),
        source=source,
        registry=registry,
        schema=schema,
    )
    if not valid.passed or valid.rejection is not None:
        raise AssertionError(f"valid packet failed: {valid.audit_record()}")
    if valid.validated_result is None:
        raise AssertionError("valid packet lost its normalized result")
    assert_sanitized(valid)

    temporal = run(
        provider_packet(anchored_to_source_time=True),
        source=source,
        registry=registry,
        schema=schema,
    )
    if temporal.passed or temporal.rejection is None:
        raise AssertionError("provider-owned trusted time passed")
    if temporal.rejection["code"] != "trusted_source_time_asserted_by_provider":
        raise AssertionError(f"temporal rejection lost: {temporal.rejection}")
    if temporal.rejection["stage"] != "post_provider_validation":
        raise AssertionError("temporal rejection stage is not observable")
    if not temporal.sanitized_provider_packet["observations"][0]["temporal"][
        "anchored_to_source_time"
    ]:
        raise AssertionError("temporal diagnostic flag was not retained")
    assert_sanitized(temporal)

    span = run(
        provider_packet(source_quote="My name is Wrong."),
        source=source,
        registry=registry,
        schema=schema,
    )
    if span.passed or span.rejection is None:
        raise AssertionError("mismatched source span passed")
    if span.rejection["code"] != "source_span_quote_mismatch":
        raise AssertionError(f"span rejection lost: {span.rejection}")
    assert_sanitized(span)

    failure_provider = FakeOneCallProvider(
        error=ProviderAdapterError(
            "synthetic_transport_failure",
            retryable=False,
        )
    )
    failure = validate_and_normalize_observable(
        failure_provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions=ALLOWED_PROVIDER,
        max_external_model_calls=1,
    )
    if failure.passed or failure.external_model_calls != 1:
        raise AssertionError("transport failure was retried or passed")
    if failure.provider_output_sha256 is not None:
        raise AssertionError("transport failure invented a provider packet")
    if failure.sanitized_provider_packet is not None:
        raise AssertionError("transport failure invented diagnostics")
    if failure.rejection != {
        "category": "provider_adapter",
        "stage": "provider_transport_or_parse",
        "code": "synthetic_transport_failure",
        "retryable": False,
        "http_status": None,
        "message_sha256": sha256_text("synthetic_transport_failure"),
    }:
        raise AssertionError(f"transport rejection changed: {failure.rejection}")

    wrong_allowlist = copy.deepcopy(ALLOWED_PROVIDER)
    wrong_allowlist[OPENAI_PROVIDER_ID] = "wrong-version"
    preflight_provider = FakeOneCallProvider(packet=provider_packet())
    preflight = validate_and_normalize_observable(
        preflight_provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions=wrong_allowlist,
        max_external_model_calls=1,
    )
    if preflight_provider.external_model_calls != 0:
        raise AssertionError("pre-provider rejection made an external call")
    if preflight.rejection is None or preflight.rejection["code"] != (
        "provider_version_not_enabled"
    ):
        raise AssertionError(f"pre-provider rejection changed: {preflight.rejection}")
    if preflight.rejection["stage"] != "pre_provider_validation":
        raise AssertionError("pre-provider rejection stage changed")

    print("memory_v1_relational_extraction_v5_observable_provider_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
