#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_external_smoke_v2 import (
    FORBIDDEN_AUDIT_CLEARTEXT,
    ZERO_PROHIBITED_EFFECTS,
    assert_audit_sanitized,
    candidate_preflight_report,
    execute_once,
    load_smoke_manifest,
    prepare_smoke,
    secure_write_audit,
)
from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    ProviderAdapterError,
    ResponsesRequest,
    ResponsesResult,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    canonical_json,
)


CONTENT = "My name is Avery."
SOURCE_TIME = "2026-07-16T20:30:00Z"
MANIFEST_RELATIVE = (
    "ops/manifests/"
    "memory_v1_relational_extraction_v5_external_smoke_v2_candidate_20260716.json"
)


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


class FakeOneCallTransport:
    external_call_capability = True

    def __init__(
        self,
        *,
        packet: ProviderPacket | None = None,
        error: ProviderAdapterError | None = None,
    ) -> None:
        if (packet is None) == (error is None):
            raise ValueError("fake transport requires exactly one packet or error")
        self.external_model_calls = 0
        self.packet = packet
        self.error = error
        self.requests: list[ResponsesRequest] = []

    def parse(self, request: ResponsesRequest) -> ResponsesResult:
        if self.external_model_calls != 0:
            raise AssertionError("fake smoke-v2 transport was retried")
        if request.store is not False:
            raise AssertionError("fake smoke-v2 request attempted response storage")
        self.external_model_calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.packet is None:
            raise AssertionError("fake packet disappeared")
        return ResponsesResult(
            response_id="resp_synthetic_smoke_v2",
            status="completed",
            parsed=self.packet.model_copy(deep=True),
            refusal=False,
            incomplete_reason=None,
        )


def write_manifest(path: Path, value: dict) -> str:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_no_cleartext(report: dict) -> None:
    assert_audit_sanitized(report)
    encoded = canonical_json(report)
    for forbidden in FORBIDDEN_AUDIT_CLEARTEXT:
        if forbidden in encoded:
            raise AssertionError(f"smoke-v2 audit retained cleartext: {forbidden}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    candidate_path = repo_root / MANIFEST_RELATIVE
    candidate_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    candidate, loaded_sha256 = load_smoke_manifest(
        candidate_path,
        candidate_sha256,
    )
    prepared_candidate = prepare_smoke(
        repo_root=repo_root,
        manifest=candidate,
        manifest_sha256=loaded_sha256,
        enforce_clean_repository=False,
    )
    preflight = candidate_preflight_report(prepared_candidate)
    if preflight["ready_for_separate_authorization"] is not True:
        raise AssertionError("candidate preflight is not ready")
    if preflight["effects"]["external_model_calls"] != 0:
        raise AssertionError("candidate preflight invoked an external model")
    if any(
        preflight["effects"][key] != value
        for key, value in ZERO_PROHIBITED_EFFECTS.items()
    ):
        raise AssertionError("candidate preflight reported a prohibited effect")
    try:
        load_smoke_manifest(
            candidate_path,
            candidate_sha256,
            require_authorized=True,
        )
    except RuntimeError as exc:
        if "does not authorize" not in str(exc):
            raise
    else:
        raise AssertionError("candidate manifest authorized transport")

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        armed_value = copy.deepcopy(candidate)
        armed_value["external_call_authorized"] = True
        armed_value["authorization"]["authorized_external_model_calls"] = 1
        armed_path = temporary / "armed.json"
        armed_sha256 = write_manifest(armed_path, armed_value)
        armed, armed_loaded_sha256 = load_smoke_manifest(
            armed_path,
            armed_sha256,
            require_authorized=True,
        )
        prepared = prepare_smoke(
            repo_root=repo_root,
            manifest=armed,
            manifest_sha256=armed_loaded_sha256,
            require_authorized=True,
            enforce_clean_repository=False,
        )

        valid_transport = FakeOneCallTransport(packet=provider_packet())
        valid = execute_once(prepared=prepared, transport=valid_transport)
        if valid["passed"] is not True:
            raise AssertionError(f"valid smoke-v2 failed: {valid}")
        if valid_transport.external_model_calls != 1:
            raise AssertionError("valid smoke-v2 call count changed")
        if valid["observable_validation"]["sanitized_provider_packet"] is None:
            raise AssertionError("valid smoke-v2 lost sanitized diagnostics")
        if valid["provider"]["audit"]["response_id_sha256"] != hashlib.sha256(
            b"resp_synthetic_smoke_v2"
        ).hexdigest():
            raise AssertionError("response ID was not reduced to a digest")
        assert_no_cleartext(valid)

        temporal_transport = FakeOneCallTransport(
            packet=provider_packet(anchored_to_source_time=True)
        )
        temporal = execute_once(prepared=prepared, transport=temporal_transport)
        rejection = temporal["observable_validation"]["rejection"]
        if temporal["passed"] is not False or rejection is None:
            raise AssertionError("provider-owned trusted time passed")
        if rejection["code"] != "trusted_source_time_asserted_by_provider":
            raise AssertionError(f"temporal rejection changed: {rejection}")
        if temporal["observable_validation"]["sanitized_provider_packet"] is None:
            raise AssertionError("temporal rejection lost sanitized packet")
        assert_no_cleartext(temporal)

        span_transport = FakeOneCallTransport(
            packet=provider_packet(source_quote="My name is Wrong.")
        )
        span = execute_once(prepared=prepared, transport=span_transport)
        rejection = span["observable_validation"]["rejection"]
        if span["passed"] is not False or rejection is None:
            raise AssertionError("mismatched source quote passed")
        if rejection["code"] != "source_span_quote_mismatch":
            raise AssertionError(f"source-span rejection changed: {rejection}")
        assert_no_cleartext(span)

        failure_transport = FakeOneCallTransport(
            error=ProviderAdapterError(
                "synthetic_transport_failure",
                retryable=False,
            )
        )
        failure = execute_once(prepared=prepared, transport=failure_transport)
        rejection = failure["observable_validation"]["rejection"]
        if failure["passed"] is not False or rejection is None:
            raise AssertionError("transport failure passed")
        if failure_transport.external_model_calls != 1:
            raise AssertionError("transport failure was retried")
        if rejection["code"] != "synthetic_transport_failure":
            raise AssertionError(f"transport rejection changed: {rejection}")
        if failure["observable_validation"]["sanitized_provider_packet"] is not None:
            raise AssertionError("transport failure invented a provider packet")
        assert_no_cleartext(failure)

        inconsistent = copy.deepcopy(candidate)
        inconsistent["external_call_authorized"] = True
        inconsistent_path = temporary / "inconsistent.json"
        inconsistent_sha256 = write_manifest(inconsistent_path, inconsistent)
        try:
            load_smoke_manifest(inconsistent_path, inconsistent_sha256)
        except RuntimeError as exc:
            if "budget is inconsistent" not in str(exc):
                raise
        else:
            raise AssertionError("inconsistent authorization was accepted")

        try:
            load_smoke_manifest(candidate_path, "0" * 64)
        except RuntimeError as exc:
            if "file SHA-256 mismatch" not in str(exc):
                raise
        else:
            raise AssertionError("incorrect manifest hash was accepted")

        artifact_tamper = copy.deepcopy(candidate)
        artifact_tamper["artifacts"][0]["sha256"] = "0" * 64
        try:
            prepare_smoke(
                repo_root=repo_root,
                manifest=artifact_tamper,
                manifest_sha256=candidate_sha256,
                enforce_clean_repository=False,
            )
        except RuntimeError as exc:
            if "artifact SHA-256 mismatch" not in str(exc):
                raise
        else:
            raise AssertionError("artifact tamper was accepted")

        audit_path = temporary / "audit.json"
        secure_write_audit(audit_path, valid)
        if audit_path.stat().st_mode & 0o777 != 0o600:
            raise AssertionError("audit mode is not 0600")
        try:
            secure_write_audit(audit_path, valid)
        except FileExistsError:
            pass
        else:
            raise AssertionError("audit replay overwrote the immutable output")

    print("memory_v1_relational_extraction_v5_external_smoke_v2_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
