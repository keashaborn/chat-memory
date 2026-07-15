#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace

from scripts.memory_v1_projection_worker import _owners, _validate_limits


OWNER_A = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OWNER_B = "557ea042-cb82-48f8-9429-472e96c957ef"
OWNER_C = "d839b4bc-0bd2-4f2d-aafe-0f3f75883db8"


def args(**updates):
    values = {
        "owner_user_id": [],
        "limit": 25,
        "max_attempts": 8,
        "lease_seconds": 600,
        "vector_size": 3072,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def expect_error(callback, message: str) -> None:
    try:
        callback()
    except Exception:
        return
    raise AssertionError(message)


def main() -> int:
    original = dict(os.environ)
    try:
        os.environ.pop("MEMORY_V1_PROJECTION_OWNER_IDS", None)
        expect_error(
            lambda: _owners(args()),
            "missing projection allowlist did not fail closed",
        )

        os.environ["MEMORY_V1_PROJECTION_OWNER_IDS"] = f"{OWNER_C},{OWNER_A}"
        owners = _owners(args(owner_user_id=[OWNER_B, OWNER_A]))
        if [str(owner) for owner in owners] != [OWNER_A, OWNER_B, OWNER_C]:
            raise AssertionError(f"owner allowlist was not canonical: {owners}")

        _validate_limits(args())
        expect_error(
            lambda: _validate_limits(args(limit=0)),
            "zero batch limit was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(limit=101)),
            "oversized batch limit was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(max_attempts=0)),
            "zero max-attempts was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(max_attempts=51)),
            "oversized max-attempts was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(lease_seconds=29)),
            "undersized lease was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(lease_seconds=3601)),
            "oversized lease was accepted",
        )
        expect_error(
            lambda: _validate_limits(args(vector_size=0)),
            "zero vector size was accepted",
        )
    finally:
        os.environ.clear()
        os.environ.update(original)

    print("memory_v1_projection_worker_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
