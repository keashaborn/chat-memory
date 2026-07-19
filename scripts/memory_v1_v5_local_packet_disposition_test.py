#!/usr/bin/env python3
from __future__ import annotations

import uuid

from scripts.memory_v1_v5_local_packet_disposition import (
    canonical_owners,
    loopback_dsn,
    operation_ids,
)


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
PACKET = uuid.UUID("50c8fb51-f847-5423-b9b6-285c79703270")


def main() -> int:
    if canonical_owners([str(OWNER), str(OWNER)]) != [OWNER]:
        raise AssertionError("owner allowlist is not deterministic")
    if loopback_dsn("postgresql://user@127.0.0.1:5432/memory") != (
        "postgresql://user@127.0.0.1:5432/memory"
    ):
        raise AssertionError("loopback DSN changed")
    for invalid in (
        "postgresql://user@10.0.0.1:5432/memory",
        "https://127.0.0.1/memory",
    ):
        try:
            loopback_dsn(invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe database DSN was accepted")
    first = operation_ids(OWNER, PACKET, "a" * 64)
    second = operation_ids(OWNER, PACKET, "a" * 64)
    changed = operation_ids(OWNER, PACKET, "b" * 64)
    review_unresolved = operation_ids(
        OWNER,
        PACKET,
        "a" * 64,
        "deferral_only_review_unresolved",
    )
    if (
        first != second
        or first == changed
        or first == review_unresolved
        or first[0] == first[1]
    ):
        raise AssertionError("disposition identities are not deterministic")
    try:
        operation_ids(OWNER, PACKET, "a" * 64, "unsafe_reason")
    except RuntimeError:
        pass
    else:
        raise AssertionError("unsupported disposition reason was accepted")
    for invalid_owners in (
        [],
        ["not-a-uuid"],
        [str(uuid.UUID(int=index + 1)) for index in range(7)],
    ):
        try:
            canonical_owners(invalid_owners)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe owner allowlist was accepted")
    print("memory_v1_v5_local_packet_disposition: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
