#!/usr/bin/env python3
"""Deterministic unit checks for the exact-link resolution worker."""

from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_auto_resolution import (
    POLICY_VERSION,
    operation_ids,
)


def main() -> int:
    owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
    stage = uuid.UUID("a3300000-0000-4000-8000-000000000001")
    resolution = uuid.UUID("a3600000-0000-4000-8000-000000000001")
    first = operation_ids(owner, stage, resolution, "a" * 64)
    second = operation_ids(owner, stage, resolution, "a" * 64)
    assert first == second
    assert len(set(first)) == 3
    assert operation_ids(owner, stage, resolution, "b" * 64) != first
    assert POLICY_VERSION == "memory_v1_v5_local_auto_resolution_policy_v1"
    print("memory_v1_v5_local_auto_resolution: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
