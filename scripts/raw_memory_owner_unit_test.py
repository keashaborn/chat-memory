#!/usr/bin/env python3
from __future__ import annotations

import uuid
from types import SimpleNamespace

from rag_engine import retriever_unified
from rag_engine.raw_memory_ownership import (
    RawMemoryOwnershipError,
    assert_raw_payload_owner,
    canonical_owner_user_id,
    owned_raw_payload,
)


OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"
OTHER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


class FakeQdrant:
    def __init__(self, hits):
        self.hits = hits
        self.filters = []

    def search(self, **kwargs):
        self.filters.append(kwargs["query_filter"])
        return self.hits


def hit(payload):
    return SimpleNamespace(id=str(uuid.uuid4()), score=0.9, payload=payload)


def main() -> int:
    assert canonical_owner_user_id(OWNER.upper()) == OWNER
    payload = owned_raw_payload(OWNER, {"text": "synthetic account fact"})
    assert payload["owner_user_id"] == OWNER
    assert payload["user_id"] == OWNER
    assert_raw_payload_owner(payload, OWNER)

    try:
        owned_raw_payload(OWNER, {"user_id": OTHER})
    except RawMemoryOwnershipError:
        pass
    else:
        raise AssertionError("legacy owner conflict was accepted")

    original_qdrant = retriever_unified.qdrant
    try:
        good = FakeQdrant([hit(payload)])
        retriever_unified.qdrant = good
        rows = retriever_unified.retrieve_personal_memory(
            OWNER,
            "What was the synthetic fact?",
            query_vector=[0.1, 0.2],
        )
        assert len(rows) == 1
        conditions = good.filters[0].must
        assert len(conditions) >= 1
        assert conditions[0].key == "owner_user_id"
        assert conditions[0].match.value == OWNER

        missing = FakeQdrant([hit({"user_id": OWNER, "text": "unsafe"})])
        retriever_unified.qdrant = missing
        assert retriever_unified.retrieve_personal_memory(
            OWNER,
            "unsafe",
            query_vector=[0.1, 0.2],
        ) == []

        wrong = FakeQdrant([
            hit({"owner_user_id": OTHER, "user_id": OTHER, "text": "foreign"})
        ])
        retriever_unified.qdrant = wrong
        assert retriever_unified.retrieve_personal_memory(
            OWNER,
            "foreign",
            query_vector=[0.1, 0.2],
        ) == []

        invalid = FakeQdrant([])
        retriever_unified.qdrant = invalid
        assert retriever_unified.retrieve_personal_memory(
            "guest",
            "anything",
            query_vector=[0.1, 0.2],
        ) == []
        assert invalid.filters == []
    finally:
        retriever_unified.qdrant = original_qdrant

    print("OK: canonical raw-memory ownership is mandatory and fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
