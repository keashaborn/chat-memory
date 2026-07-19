#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import uuid

from memory_v1_v5_legacy_claim_reintake import (
    APPLY_TOKEN,
    canonical_owners,
    loopback_dsn,
)


def main() -> None:
    owner = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
    assert canonical_owners([owner, owner]) == [uuid.UUID(owner)]
    for invalid in ([], ["invalid"]):
        try:
            canonical_owners(invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid owner allowlist was accepted")
    assert loopback_dsn("postgresql://u@127.0.0.1:5432/memory")
    try:
        loopback_dsn("postgresql://u@10.0.0.1:5432/memory")
    except RuntimeError:
        pass
    else:
        raise AssertionError("non-loopback DSN was accepted")
    original = os.environ.get("MEMORY_V1_V5_LEGACY_REINTAKE_APPLY")
    try:
        os.environ["MEMORY_V1_V5_LEGACY_REINTAKE_APPLY"] = APPLY_TOKEN
        assert os.environ["MEMORY_V1_V5_LEGACY_REINTAKE_APPLY"] == APPLY_TOKEN
    finally:
        if original is None:
            os.environ.pop("MEMORY_V1_V5_LEGACY_REINTAKE_APPLY", None)
        else:
            os.environ["MEMORY_V1_V5_LEGACY_REINTAKE_APPLY"] = original
    print("memory_v1_v5_legacy_claim_reintake: PASS")


if __name__ == "__main__":
    main()
