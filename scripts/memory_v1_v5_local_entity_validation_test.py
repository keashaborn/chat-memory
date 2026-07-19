#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import replace

from scripts.memory_v1_v5_local_entity_validation_provider import (
    EntityValidationAssessment,
    governed_decision,
    validate_assessment,
)


def main() -> int:
    assert validate_assessment(
        {"decision": "supported", "confidence": "high"}
    ) == ("supported", "high")
    base = EntityValidationAssessment(
        decision="supported",
        confidence="high",
        request_sha256="1" * 64,
        response_sha256="2" * 64,
        output_schema_sha256="3" * 64,
        local_model_calls=1,
    )
    assert governed_decision(base) == (
        "accepted",
        "explicit_named_entity_supported",
    )
    assert governed_decision(replace(base, confidence="medium"))[0] == "deferred"
    assert governed_decision(replace(base, decision="contradicted"))[0] == "deferred"
    print("memory_v1_v5_local_entity_validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
