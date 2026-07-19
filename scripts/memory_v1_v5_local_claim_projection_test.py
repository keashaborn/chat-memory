#!/usr/bin/env python3
from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_claim_projection import (
    POLICY_VERSION,
    operation_ids,
)


def main() -> int:
    owner=uuid.UUID("11111111-1111-4111-8111-111111111111")
    assessment=uuid.UUID("22222222-2222-4222-8222-222222222222")
    observation=uuid.UUID("33333333-3333-4333-8333-333333333333")
    first=operation_ids(owner,assessment,observation,"a"*64)
    assert first==operation_ids(owner,assessment,observation,"a"*64)
    assert len(set(first))==2
    assert first!=operation_ids(owner,assessment,observation,"b"*64)
    assert POLICY_VERSION=="memory_v1_v5_local_claim_projection_policy_v1"
    print("memory_v1_v5_local_claim_projection: PASS")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
