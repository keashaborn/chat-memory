#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_external_smoke import (
    ZERO_PROHIBITED_EFFECTS,
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
from scripts.memory_v1_relational_extraction_v5_provider import ProviderPacket


class FakeOneCallTransport:
    external_call_capability = True

    def __init__(self, *, fail: bool = False) -> None:
        self.external_model_calls = 0
        self.fail = fail
        self.requests: list[ResponsesRequest] = []

    def parse(self, request: ResponsesRequest) -> ResponsesResult:
        if self.external_model_calls != 0:
            raise AssertionError("fake transport was invoked more than once")
        if request.store is not False:
            raise AssertionError("fake request attempted response storage")
        self.external_model_calls += 1
        self.requests.append(request)
        if self.fail:
            raise ProviderAdapterError(
                "synthetic_transport_failure",
                retryable=False,
            )
        return ResponsesResult(
            response_id="resp_synthetic_smoke",
            status="completed",
            parsed=ProviderPacket.model_validate(
                {
                    "entity_mentions": [
                        {
                            "entity_ref": "e01",
                            "entity_type": "self",
                            "mention_kind": "self_reference",
                            "name_text": "Avery",
                            "relationship_role": "user:self",
                            "source_spans": [
                                {
                                    "start": 0,
                                    "end": 17,
                                    "quote": "My name is Avery.",
                                }
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
                                "instant": "2026-07-16T20:30:00Z",
                                "calendar_range": None,
                                "instant_range": None,
                                "relative_offset": None,
                                "recurrence": None,
                                "anchored_to_source_time": False,
                                "reason_codes": ["implicit_source_time"],
                            },
                            "sensitivity": "medium",
                            "extraction_confidence": 1.0,
                            "source_spans": [
                                {
                                    "start": 0,
                                    "end": 17,
                                    "quote": "My name is Avery.",
                                }
                            ],
                            "reason_codes": ["explicit_self_name"],
                        }
                    ],
                    "comparison_hints": [],
                    "deferrals": [],
                    "packet_findings": [],
                }
            ),
            refusal=False,
            incomplete_reason=None,
        )


def _manifest(repo_root: Path) -> tuple[dict, str]:
    path = (
        repo_root
        / "ops/manifests/"
        "memory_v1_relational_extraction_v5_external_smoke_20260716.json"
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return load_smoke_manifest(path, digest)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    manifest, manifest_sha256 = _manifest(repo_root)
    prepared = prepare_smoke(
        repo_root=repo_root,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        enforce_clean_repository=False,
    )

    transport = FakeOneCallTransport()
    report = execute_once(prepared=prepared, transport=transport)
    if report["passed"] is not True:
        raise AssertionError(f"fake one-call smoke failed: {report}")
    if transport.external_model_calls != 1 or len(transport.requests) != 1:
        raise AssertionError("fake one-call budget was not exact")
    if report["provider"]["audit"]["response_id_sha256"] != hashlib.sha256(
        b"resp_synthetic_smoke"
    ).hexdigest():
        raise AssertionError("response ID was not reduced to a digest")
    if any(report["effects"][key] != value for key, value in ZERO_PROHIBITED_EFFECTS.items()):
        raise AssertionError("fake smoke reported a prohibited write")

    failure_transport = FakeOneCallTransport(fail=True)
    failure = execute_once(prepared=prepared, transport=failure_transport)
    if failure["passed"] is not False:
        raise AssertionError("failed fake transport passed")
    if failure_transport.external_model_calls != 1:
        raise AssertionError("failed fake transport was retried or not invoked")
    if failure["error"]["code"] != "synthetic_transport_failure":
        raise AssertionError("failed fake transport error was not audited")

    changed = copy.deepcopy(manifest)
    changed["external_call_authorized"] = False
    with tempfile.TemporaryDirectory() as directory:
        changed_path = Path(directory) / "changed.json"
        changed_path.write_text(
            json.dumps(changed, sort_keys=True),
            encoding="utf-8",
        )
        changed_sha256 = hashlib.sha256(changed_path.read_bytes()).hexdigest()
        try:
            load_smoke_manifest(changed_path, changed_sha256)
        except RuntimeError as exc:
            if "does not authorize" not in str(exc):
                raise
        else:
            raise AssertionError("unauthorized smoke manifest was accepted")

        audit_path = Path(directory) / "audit.json"
        secure_write_audit(audit_path, report)
        if not audit_path.is_file():
            raise AssertionError("secure audit was not created")
        try:
            secure_write_audit(audit_path, report)
        except FileExistsError:
            pass
        else:
            raise AssertionError("audit replay unexpectedly overwrote output")

    print("memory_v1_relational_extraction_v5_external_smoke_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
