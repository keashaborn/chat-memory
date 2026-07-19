#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from memory_v1_v5_mixed_legacy_claim_reintake import (
    APPLY_TOKEN,
    canonical_owners,
    loopback_dsn,
    secure_write,
)


def main() -> None:
    owner = "d839b4bc-0bd2-4f2d-aafe-0f3f75883db8"
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
    original = os.environ.get("MEMORY_V1_V5_MIXED_REINTAKE_APPLY")
    try:
        os.environ["MEMORY_V1_V5_MIXED_REINTAKE_APPLY"] = APPLY_TOKEN
        assert os.environ["MEMORY_V1_V5_MIXED_REINTAKE_APPLY"] == APPLY_TOKEN
    finally:
        if original is None:
            os.environ.pop("MEMORY_V1_V5_MIXED_REINTAKE_APPLY", None)
        else:
            os.environ["MEMORY_V1_V5_MIXED_REINTAKE_APPLY"] = original
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "report.json"
        secure_write(output, {"test": True})
        try:
            secure_write(output, {"test": False})
        except RuntimeError:
            pass
        else:
            raise AssertionError("mixed report overwrite was accepted")
    print("memory_v1_v5_mixed_legacy_claim_reintake: PASS")


if __name__ == "__main__":
    main()
