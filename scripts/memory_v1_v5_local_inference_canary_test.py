#!/usr/bin/env python3
from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_inference_canary import (
    canonical_source_external_id,
)


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
    print("memory_v1_v5_local_inference_canary_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
