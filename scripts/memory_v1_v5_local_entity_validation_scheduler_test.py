#!/usr/bin/env python3
from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_entity_validation import operation_ids


def main() -> int:
    owner=uuid.UUID("11111111-1111-4111-8111-111111111111")
    artifact=uuid.UUID("22222222-2222-4222-8222-222222222222")
    first=operation_ids(owner,artifact,"e01","a"*64)
    assert first==operation_ids(owner,artifact,"e01","a"*64)
    assert len(first)==5 and len(set(first))==5
    assert first!=operation_ids(owner,artifact,"e02","a"*64)
    assert first!=operation_ids(owner,artifact,"e01","b"*64)
    print("memory_v1_v5_local_entity_validation_scheduler: PASS")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
