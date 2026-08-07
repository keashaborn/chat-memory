#!/usr/bin/env python3
from __future__ import annotations

import copy
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_provider import (
    _normalize_temporal,
    ProviderPacket,
    REASON_CODE_PATTERN,
    TrustedExtractionSource,
    TrustedProjectBinding,
    TrustedProjectComponent,
    load_registry,
    load_schema,
    sha256_text,
    synthetic_provider,
    validate_and_normalize,
)


REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "744ce1d466dfe502fb78fd0ba0a996dd723d1593f34bc456c849c20811f99e70"
)
SOURCE = "My name is Avery."
SOURCE_SHA256 = (
    "3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa"
)
PROJECT_SOURCE = "Verbal Sage must keep owner boundaries fail-closed."
PROJECT_SOURCE_SHA256 = (
    "9079a50293a8be146b295842466779da55f4fdedc649d7a3c041891e7ce0a21b"
)
COMPONENT_SOURCE = "Memory V1 must keep owner boundaries fail-closed."
COMPONENT_SOURCE_SHA256 = (
    "ce58230aed63507e2872a5c6cb203459a23e6269e17e800e1d4f24382da08cb1"
)


def expect_error(callback, label: str) -> None:
    try:
        callback()
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


def temporal_none() -> dict:
    return {
        "semantic": "observation_time",
        "shape": "none",
        "basis": "none",
        "source_form": "none",
        "certainty": "unknown",
        "precision": "unknown",
        "instant": None,
        "calendar_range": None,
        "instant_range": None,
        "relative_offset": None,
        "recurrence": None,
        "anchored_to_source_time": False,
        "reason_codes": [],
    }


def valid_output() -> dict:
    return {
        "entity_mentions": [
            {
                "entity_ref": "e01",
                "entity_type": "self",
                "mention_kind": "self_reference",
                "name_text": None,
                "relationship_role": "user:self",
                "source_spans": [
                    {"start": 0, "end": 2, "quote": "My"}
                ],
                "extraction_confidence": 1.0,
                "reason_codes": ["explicit_self_reference"],
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
                "temporal": temporal_none(),
                "sensitivity": "medium",
                "extraction_confidence": 0.99,
                "source_spans": [
                    {
                        "start": 0,
                        "end": 17,
                        "quote": "My name is Avery.",
                    }
                ],
                "reason_codes": ["explicit_identity_statement"],
            }
        ],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": ["synthetic_contract_test"],
    }


def project_output() -> dict:
    return {
        "entity_mentions": [
            {
                "entity_ref": "e01",
                "entity_type": "project",
                "mention_kind": "named",
                "name_text": "Verbal Sage",
                "relationship_role": None,
                "source_spans": [
                    {"start": 0, "end": 11, "quote": "Verbal Sage"}
                ],
                "extraction_confidence": 1.0,
                "reason_codes": ["explicit_project_name"],
            }
        ],
        "observations": [
            {
                "observation_ref": "o01",
                "subject_entity_ref": "e01",
                "predicate": "project.requirement",
                "object": {
                    "kind": "literal",
                    "datatype": "text",
                    "value": "keep owner boundaries fail-closed",
                    "unit": None,
                    "approximate": False,
                },
                "polarity": "affirmed",
                "modality": "asserted",
                "projection_class": "project_knowledge",
                "surface_policy": "exact_project_scope_only",
                "temporal": temporal_none(),
                "sensitivity": "medium",
                "extraction_confidence": 0.99,
                "source_spans": [
                    {"start": 0, "end": 51, "quote": PROJECT_SOURCE}
                ],
                "reason_codes": ["explicit_project_requirement"],
            }
        ],
        "comparison_hints": [],
        "deferrals": [
        {
            "reason_code": "project_scope_unresolved",
            "memory_shape": "project_knowledge",
            "source_spans": [
                    {"start": 0, "end": 51, "quote": PROJECT_SOURCE}
            ],
            "sensitivity": "medium",
        }
        ],
        "packet_findings": ["synthetic_project_contract_test"],
    }


def component_output() -> dict:
    output = copy.deepcopy(project_output())
    output["entity_mentions"][0].update(
        {
            "name_text": "Memory V1",
            "source_spans": [
                {"start": 0, "end": 9, "quote": "Memory V1"}
            ],
        }
    )
    output["observations"][0]["source_spans"] = [
        {"start": 0, "end": 49, "quote": COMPONENT_SOURCE}
    ]
    output["deferrals"][0]["source_spans"] = [
        {"start": 0, "end": 49, "quote": COMPONENT_SOURCE}
    ]
    return output


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    provider_schema = ProviderPacket.model_json_schema()
    packet_finding_items = provider_schema["properties"]["packet_findings"]["items"]
    if packet_finding_items.get("pattern") != REASON_CODE_PATTERN:
        raise AssertionError("provider packet findings lost reason-code pattern")
    prose_finding = valid_output()
    prose_finding["packet_findings"] = [
        "This is prose and must not enter the reason-code array."
    ]
    expect_error(
        lambda: ProviderPacket.model_validate(prose_finding),
        "provider packet finding prose",
    )
    registry = load_registry(
        repo_root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        repo_root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    undated_occurrence = temporal_none()
    undated_occurrence.update(
        {
            "semantic": "occurrence",
            "source_form": "implicit_source_time",
            "reason_codes": ["implicit_source_time"],
        }
    )
    normalized_occurrence = _normalize_temporal(
        undated_occurrence,
        "2026-07-16T12:01:00Z",
    )
    if (
        normalized_occurrence["shape"] != "none"
        or normalized_occurrence["basis"] != "none"
        or normalized_occurrence["source_form"] != "none"
        or normalized_occurrence["anchored_to_source_time"]
        or normalized_occurrence["instant"] is not None
        or "server_undated_temporal_unanchored"
        not in normalized_occurrence["reason_codes"]
    ):
        raise AssertionError("undated occurrence acquired a false source timestamp")
    source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0001-4000-8000-000000000001",
        source_system="public.chat_log",
        source_external_id="aaaaaaaa-0002-4000-8000-000000000002",
        source_sha256=SOURCE_SHA256,
        source_recorded_at="2026-07-16T12:01:00Z",
        content=SOURCE,
    )
    governed_external_id = (
        "chat_log:legacy-capture:"
        "aaaaaaaa-0002-4000-8000-000000000002"
    )
    governed_source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0006-4000-8000-000000000006",
        source_system="public.chat_log",
        source_external_id=governed_external_id,
        source_sha256=SOURCE_SHA256,
        source_recorded_at="2026-07-16T12:01:00Z",
        content=SOURCE,
    )
    if governed_source.source_external_id != governed_external_id:
        raise AssertionError(
            "governed source external ID was not preserved exactly"
        )
    maximum_external_id = "x" * 500
    maximum_source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0007-4000-8000-000000000007",
        source_system="public.chat_log",
        source_external_id=maximum_external_id,
        source_sha256=SOURCE_SHA256,
        source_recorded_at="2026-07-16T12:01:00Z",
        content=SOURCE,
    )
    if maximum_source.source_external_id != maximum_external_id:
        raise AssertionError("maximum governed external ID changed")
    for invalid_external_id in (
        None,
        1,
        "",
        " \t\n",
        "x" * 501,
        "unsafe\x00id",
    ):
        expect_error(
            lambda value=invalid_external_id: TrustedExtractionSource.create(
                job_id="aaaaaaaa-0008-4000-8000-000000000008",
                source_system="public.chat_log",
                source_external_id=value,
                source_sha256=SOURCE_SHA256,
                source_recorded_at="2026-07-16T12:01:00Z",
                content=SOURCE,
            ),
            "invalid governed source external ID",
        )
    shifted_source_text = (
        "XXMy name is Avery in this exact synthetic record. Avery."
    )
    shifted_source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0004-4000-8000-000000000004",
        source_system="public.chat_log",
        source_external_id="aaaaaaaa-0005-4000-8000-000000000005",
        source_sha256=sha256_text(shifted_source_text),
        source_recorded_at="2026-07-16T12:01:30Z",
        content=shifted_source_text,
    )
    shifted_output = valid_output()
    shifted_output["entity_mentions"][0]["source_spans"] = [
        {
            "start": 0,
            "end": 48,
            "quote": "My name is Avery in this exact synthetic record.",
        }
    ]
    shifted_output["observations"][0]["source_spans"] = [
        {"start": 11, "end": 16, "quote": "Avery"}
    ]
    shifted_result = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=shifted_output,
        ),
        source=shifted_source,
        registry=registry,
        schema=schema,
    )
    shifted_span = shifted_result.normalized_packet["observations"][0][
        "source_spans"
    ][0]
    if shifted_span["start"] != 13 or shifted_span["end"] != 18:
        raise AssertionError("packet-wide source offset delta was not applied")
    offset_repair_output = valid_output()
    offset_repair_output["observations"][0]["source_spans"][0]["end"] = 18
    offset_repair = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=offset_repair_output,
        ),
        source=source,
        registry=registry,
        schema=schema,
    )
    repaired_span = offset_repair.normalized_packet["observations"][0][
        "source_spans"
    ][0]
    if repaired_span["start"] != 0 or repaired_span["end"] != 17:
        raise AssertionError("unique source quote did not repair offsets")
    ambiguous_span_output = valid_output()
    ambiguous_span_output["entity_mentions"][0]["source_spans"] = [
        {"start": 0, "end": 2, "quote": " "}
    ]
    expect_error(
        lambda: validate_and_normalize(
            synthetic_provider(
                provider_id="synthetic_fixture",
                provider_version="v1",
                output=ambiguous_span_output,
            ),
            source=source,
            registry=registry,
            schema=schema,
        ),
        "ambiguous source quote repair",
    )
    provider = synthetic_provider(
        provider_id="synthetic_fixture",
        provider_version="v1",
        output=valid_output(),
    )
    result = validate_and_normalize(
        provider,
        source=source,
        registry=registry,
        schema=schema,
    )
    packet = result.normalized_packet
    if result.external_model_calls != 0:
        raise AssertionError("synthetic provider declared an external model call")
    if result.manual_review_required is not False:
        raise AssertionError("self-name fixture unexpectedly requires manual review")
    if packet["source_envelope"] != {
        "job_id": source.job_id,
        "source_system": "public.chat_log",
        "source_external_id": source.source_external_id,
        "source_sha256": SOURCE_SHA256,
        "source_recorded_at": "2026-07-16T12:01:00.000000Z",
    }:
        raise AssertionError("trusted source envelope was not injected exactly")
    observation = packet["observations"][0]
    if observation["predicate_registry_status"] != "governed":
        raise AssertionError("predicate registry status was not server-injected")
    if observation["temporal"]["instant"] != (
        "2026-07-16T12:01:00.000000Z"
    ):
        raise AssertionError("observation time was not source-time normalized")
    if observation["project_scope"]["state"] != "not_applicable":
        raise AssertionError("non-project observation received project scope")
    if "quote" in str(packet):
        raise AssertionError("normalized packet retained provider span quotes")
    if "owner_user_id" in str(packet) or "vantage_id" in str(packet):
        raise AssertionError("normalized packet contains an ownership namespace")

    project_source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0020-4000-8000-000000000020",
        source_system="public.chat_log",
        source_external_id="aaaaaaaa-0021-4000-8000-000000000021",
        source_sha256=PROJECT_SOURCE_SHA256,
        source_recorded_at="2026-07-16T12:02:00Z",
        content=PROJECT_SOURCE,
    )
    temporal_default_output = project_output()
    temporal_default_output["observations"][0]["temporal"]["semantic"] = "none"
    temporal_default = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=temporal_default_output,
        ),
        source=project_source,
        registry=registry,
        schema=schema,
    )
    defaulted_temporal = temporal_default.normalized_packet["observations"][0][
        "temporal"
    ]
    if (
        defaulted_temporal["semantic"] != "observation_time"
        or not defaulted_temporal["anchored_to_source_time"]
        or "server_observation_time_default"
        not in defaulted_temporal["reason_codes"]
    ):
        raise AssertionError("governed observation-time default was not applied")
    project_provider = synthetic_provider(
        provider_id="synthetic_fixture",
        provider_version="v1",
        output=project_output(),
    )
    unresolved_project = validate_and_normalize(
        project_provider,
        source=project_source,
        registry=registry,
        schema=schema,
    )
    unresolved_observation = unresolved_project.normalized_packet[
        "observations"
    ][0]
    if unresolved_observation["project_scope"]["state"] != "unresolved":
        raise AssertionError("unbound project observation was not deferred")
    if not any(
        item["reason_code"] == "project_scope_unresolved"
        for item in unresolved_project.normalized_packet["deferrals"]
    ):
        raise AssertionError("unbound project observation lost its deferral")

    memory_component = TrustedProjectComponent.create(
        component_id="aaaaaaaa-0013-4000-8000-000000000013",
        component_key="memory-v1",
        display_name="Memory V1",
        parent_component_id=None,
        aliases=["memory-v1", "memory-system", "memory"],
    )
    binding = TrustedProjectBinding.create(
        thread_id="aaaaaaaa-0010-4000-8000-000000000010",
        project_id="aaaaaaaa-0011-4000-8000-000000000011",
        project_key="verbal-sage",
        binding_event_id="aaaaaaaa-0012-4000-8000-000000000012",
        components=[memory_component],
    )
    bound_project = validate_and_normalize(
        project_provider,
        source=project_source,
        registry=registry,
        schema=schema,
        trusted_project_binding=binding,
    )
    bound_observation = bound_project.normalized_packet["observations"][0]
    if bound_observation["project_scope"] != {
        "state": "resolved",
        "project_key": "verbal-sage",
        "component_key": None,
        "binding_source": "trusted_thread_binding",
    }:
        raise AssertionError("trusted project binding was not injected")
    if any(
        item["reason_code"] == "project_scope_unresolved"
        for item in bound_project.normalized_packet["deferrals"]
    ):
        raise AssertionError("resolved project retained unresolved deferral")

    anonymous_output = copy.deepcopy(project_output())
    anonymous_output["entity_mentions"][0].update(
        {
            "mention_kind": "anonymous",
            "name_text": None,
            "relationship_role": "project:current_thread",
        }
    )
    anonymous_project = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=anonymous_output,
        ),
        source=project_source,
        registry=registry,
        schema=schema,
        trusted_project_binding=binding,
    )
    if anonymous_project.normalized_packet["observations"][0][
        "project_scope"
    ]["state"] != "resolved":
        raise AssertionError("trusted anonymous thread project was not resolved")

    component_source = TrustedExtractionSource.create(
        job_id="aaaaaaaa-0030-4000-8000-000000000030",
        source_system="public.chat_log",
        source_external_id="aaaaaaaa-0031-4000-8000-000000000031",
        source_sha256=COMPONENT_SOURCE_SHA256,
        source_recorded_at="2026-07-16T12:03:00Z",
        content=COMPONENT_SOURCE,
    )
    component_project = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=component_output(),
        ),
        source=component_source,
        registry=registry,
        schema=schema,
        trusted_project_binding=binding,
    )
    if component_project.normalized_packet["observations"][0][
        "project_scope"
    ] != {
        "state": "resolved",
        "project_key": "verbal-sage",
        "component_key": "memory-v1",
        "binding_source": "trusted_component_registry",
    }:
        raise AssertionError("trusted named component was not resolved")
    if any(
        item["reason_code"] == "project_scope_unresolved"
        for item in component_project.normalized_packet["deferrals"]
    ):
        raise AssertionError("resolved component retained unresolved deferral")

    mismatched_output = copy.deepcopy(project_output())
    mismatched_output["entity_mentions"][0]["name_text"] = "Other Project"
    mismatched_project = validate_and_normalize(
        synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=mismatched_output,
        ),
        source=project_source,
        registry=registry,
        schema=schema,
        trusted_project_binding=binding,
    )
    if mismatched_project.normalized_packet["observations"][0][
        "project_scope"
    ]["state"] != "unresolved":
        raise AssertionError("mismatched named project used thread binding")
    if not any(
        item["reason_code"] == "project_scope_unresolved"
        for item in mismatched_project.normalized_packet["deferrals"]
    ):
        raise AssertionError("mismatched named project lost review deferral")

    expect_error(
        lambda: TrustedProjectBinding.create(
            thread_id=binding.thread_id,
            project_id=binding.project_id,
            project_key="   ",
            binding_event_id=binding.binding_event_id,
        ),
        "empty trusted project key",
    )
    conflicting_component = TrustedProjectComponent.create(
        component_id="aaaaaaaa-0014-4000-8000-000000000014",
        component_key="other-component",
        display_name="Other Component",
        parent_component_id=None,
        aliases=["memory"],
    )
    expect_error(
        lambda: TrustedProjectBinding.create(
            thread_id=binding.thread_id,
            project_id=binding.project_id,
            project_key=binding.project_key,
            binding_event_id=binding.binding_event_id,
            components=[memory_component, conflicting_component],
        ),
        "ambiguous trusted component aliases",
    )
    root_collision = TrustedProjectComponent.create(
        component_id="aaaaaaaa-0015-4000-8000-000000000015",
        component_key="root-collision",
        display_name="Root Collision",
        parent_component_id=None,
        aliases=["verbal-sage"],
    )
    expect_error(
        lambda: TrustedProjectBinding.create(
            thread_id=binding.thread_id,
            project_id=binding.project_id,
            project_key=binding.project_key,
            binding_event_id=binding.binding_event_id,
            components=[root_collision],
        ),
        "component alias conflicting with project root",
    )

    malformed = valid_output()
    malformed["owner_user_id"] = "aaaaaaaa-0003-4000-8000-000000000003"
    expect_error(
        lambda: synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=malformed,
        ),
        "provider-authored owner",
    )
    malformed = valid_output()
    malformed["observations"][0]["source_spans"][0]["quote"] = "wrong"
    expect_error(
        lambda: validate_and_normalize(
            synthetic_provider(
                provider_id="synthetic_fixture",
                provider_version="v1",
                output=malformed,
            ),
            source=source,
            registry=registry,
            schema=schema,
        ),
        "source span mismatch",
    )
    malformed = valid_output()
    malformed["observations"][0]["subject_entity_ref"] = "e02"
    expect_error(
        lambda: validate_and_normalize(
            synthetic_provider(
                provider_id="synthetic_fixture",
                provider_version="v1",
                output=malformed,
            ),
            source=source,
            registry=registry,
            schema=schema,
        ),
        "dangling entity reference",
    )
    malformed = valid_output()
    malformed["observations"][0]["predicate"] = "identity.unregistered"
    expect_error(
        lambda: validate_and_normalize(
            synthetic_provider(
                provider_id="synthetic_fixture",
                provider_version="v1",
                output=malformed,
            ),
            source=source,
            registry=registry,
            schema=schema,
        ),
        "unregistered predicate",
    )
    malformed = valid_output()
    malformed["packet_findings"] = ["duplicate_code", "duplicate_code"]
    expect_error(
        lambda: validate_and_normalize(
            synthetic_provider(
                provider_id="synthetic_fixture",
                provider_version="v1",
                output=malformed,
            ),
            source=source,
            registry=registry,
            schema=schema,
        ),
        "duplicate packet finding",
    )
    malformed = valid_output()
    malformed["observations"][0]["object"]["value"] = {
        "owner_user_id": "aaaaaaaa-0003-4000-8000-000000000003"
    }
    expect_error(
        lambda: synthetic_provider(
            provider_id="synthetic_fixture",
            provider_version="v1",
            output=malformed,
        ),
        "server-owned key inside literal JSON",
    )
    expect_error(
        lambda: synthetic_provider(
            provider_id="openai",
            provider_version="v1",
            output=valid_output(),
        ),
        "non-synthetic provider",
    )
    expect_error(
        lambda: TrustedExtractionSource.create(
            job_id=source.job_id,
            source_system="public.chat_log",
            source_external_id=source.source_external_id,
            source_sha256="0" * 64,
            source_recorded_at=source.source_recorded_at,
            content=source.content,
        ),
        "trusted source hash mismatch",
    )
    altered_registry = copy.deepcopy(registry)
    altered_registry["runtime_active"] = True
    expect_error(
        lambda: validate_and_normalize(
            provider,
            source=source,
            registry=altered_registry,
            schema=schema,
        ),
        "runtime-active registry",
    )

    print("memory_v1_relational_extraction_v5_provider_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
