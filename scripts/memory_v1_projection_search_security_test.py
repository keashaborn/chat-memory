#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_projection import ClaimVectorIndex, ProjectionError

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.request = None

    def search(self, **kwargs):
        self.request = kwargs
        return [SimpleNamespace(id=CLAIM, score=0.75, payload=self.payload)]


def payload(owner: str = OWNER) -> dict:
    return {
        "owner_user_id": owner,
        "claim_id": CLAIM,
        "status": "supported",
        "schema_version": "memory_claim_projection_v1",
    }


def main() -> None:
    client = FakeClient(payload())
    index = ClaimVectorIndex(client, vector_size=3)
    result = index.search_claims(OWNER, [1.0, 0.0, 0.5], limit=4)
    assert result == [{"claim_id": CLAIM, "semantic_score": 0.75}]
    assert client.request["with_payload"] == [
        "owner_user_id",
        "claim_id",
        "status",
        "schema_version",
    ]
    assert client.request["with_vectors"] is False
    filters = {condition.key: condition for condition in client.request["query_filter"].must}
    assert filters["owner_user_id"].match.value == OWNER
    assert set(filters["status"].match.any) == {"supported", "uncertain", "disputed"}

    try:
        ClaimVectorIndex(FakeClient(payload(OTHER)), vector_size=3).search_claims(
            OWNER, [1.0, 0.0, 0.5]
        )
    except ProjectionError as exc:
        assert "cross-owner" in str(exc)
    else:
        raise AssertionError("cross-owner Qdrant hit was accepted")

    wrong_claim = payload()
    wrong_claim["claim_id"] = "3c50247c-e600-46f1-9ba3-afd2ec3f4dfe"
    try:
        ClaimVectorIndex(FakeClient(wrong_claim), vector_size=3).search_claims(
            OWNER, [1.0, 0.0, 0.5]
        )
    except ProjectionError as exc:
        assert "differ" in str(exc)
    else:
        raise AssertionError("mismatched Qdrant claim ID was accepted")

    print("memory_v1_projection_search_security: PASS")


if __name__ == "__main__":
    main()
