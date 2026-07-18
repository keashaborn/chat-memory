#!/usr/bin/env python3
from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_inference_canary import (
    canonical_source_external_id,
    loopback_dsn,
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
    print("memory_v1_v5_local_inference_canary_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
