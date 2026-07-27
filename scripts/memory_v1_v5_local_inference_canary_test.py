#!/usr/bin/env python3
from __future__ import annotations

import uuid
from types import SimpleNamespace

from scripts.memory_v1_v5_local_inference_canary import (
    canonical_source_external_id,
    effective_policy_compiler_sha256,
    loopback_dsn,
    rejection_code,
    sha256_text,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_POLICY_COMPILER_VERSION,
    RELATIONSHIP_V5_1_POLICY_COMPILER_VERSION,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256


def main() -> int:
    evidence_id = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    canonical = "BBBBBBBB-0000-4000-8000-000000000002"
    if canonical_source_external_id(
        canonical,
        evidence_id=evidence_id,
    ) != "bbbbbbbb-0000-4000-8000-000000000002":
        raise AssertionError("UUID source IDs must remain canonical")
    if canonical_source_external_id(
        "legacy-chat-log-17",
        evidence_id=evidence_id,
    ) != str(evidence_id):
        raise AssertionError("legacy source IDs must bind to evidence UUID")
    if canonical_source_external_id(
        None,
        evidence_id=evidence_id,
    ) != str(evidence_id):
        raise AssertionError("absent source IDs must bind to evidence UUID")
    if loopback_dsn("postgresql://user@127.0.0.1:5432/memory") != (
        "postgresql://user@127.0.0.1:5432/memory"
    ):
        raise AssertionError("loopback database DSN changed")
    for invalid_dsn in (
        "postgresql://user@10.0.0.1:5432/memory",
        "https://127.0.0.1/memory",
    ):
        try:
            loopback_dsn(invalid_dsn)
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-loopback database DSN was accepted")
    if rejection_code(RuntimeError("validation")) != (
        "local_validation_internal_error"
    ):
        raise AssertionError("validation rejection code changed")
    if rejection_code(RuntimeError("persistence"), phase="persistence") != (
        "local_persistence_rejected"
    ):
        raise AssertionError("persistence rejection code changed")
    if rejection_code(RuntimeError("completion"), phase="completion") != (
        "local_completion_rejected"
    ):
        raise AssertionError("completion rejection code changed")
    if effective_policy_compiler_sha256(SimpleNamespace(name="v5")) != (
        canonical_sha256(LOCAL_POLICY_COMPILER_VERSION)
    ):
        raise AssertionError("legacy V5 compiler hash compatibility changed")
    if effective_policy_compiler_sha256(SimpleNamespace(name="v5_1")) != (
        canonical_sha256(RELATIONSHIP_V5_1_POLICY_COMPILER_VERSION)
    ):
        raise AssertionError("V5.1 compiler hash is not database compatible")
    if effective_policy_compiler_sha256(SimpleNamespace(name="v5_1")) != (
        "af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d"
    ):
        raise AssertionError("V5.1 compiler hash drifted from production contract")
    if effective_policy_compiler_sha256(SimpleNamespace(name="v5_2")) != (
        sha256_text(SEMANTIC_V5_2_POLICY_COMPILER_VERSION)
    ):
        raise AssertionError("V5.2 compiler hash is not database compatible")
    print("memory_v1_v5_local_inference_canary_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
