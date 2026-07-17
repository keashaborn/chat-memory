#!/usr/bin/env python3
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_candidate import (
    V5ShadowCandidateError,
    discover_v5_shadow_candidates,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
CLAIM_A = "50ebf1af-b072-4bf9-badc-2df7585f12c6"
CLAIM_B = "3c50247c-e600-46f1-9ba3-afd2ec3f4dfe"


class FakeIndex:
    collection_name = "memory_claim_v1"

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.actor = None
        self.limit = None

    def search_claims(self, actor, vector, *, limit):
        self.actor = actor
        self.limit = limit
        assert len(vector) == 3
        return self.hits


def must_fail(hits: list[dict], message: str) -> None:
    try:
        discover_v5_shadow_candidates(
            FakeIndex(hits), OWNER, [1.0, 0.0, 0.5], limit=2
        )
    except V5ShadowCandidateError as exc:
        assert message in str(exc)
    else:
        raise AssertionError(f"candidate error was not raised: {message}")


def main() -> None:
    hits = [
        {"claim_id": CLAIM_A, "semantic_score": 0.9},
        {"claim_id": CLAIM_B, "semantic_score": 0.4},
    ]
    index = FakeIndex(hits)
    first = discover_v5_shadow_candidates(
        index, OWNER, [1.0, 0.0, 0.5], limit=2
    )
    second = discover_v5_shadow_candidates(
        FakeIndex(hits), OWNER, [1.0, 0.0, 0.5], limit=2
    )
    assert index.actor == uuid.UUID(OWNER)
    assert index.limit == 2
    assert first == second
    assert first["candidate_count"] == 2
    assert [hit["rank"] for hit in first["candidate_hits"]] == [1, 2]
    assert len(first["query_vector_sha256"]) == 64
    assert len(first["candidate_set_sha256"]) == 64
    assert first["prompt_injection"] is False
    assert first["answer_model_exposure"] is False
    assert first["retrieval_activation"] is False

    changed = discover_v5_shadow_candidates(
        FakeIndex(hits), OWNER, [1.0, 0.0, 0.5000001], limit=2
    )
    assert changed["query_vector_sha256"] != first["query_vector_sha256"]
    assert changed["candidate_set_sha256"] != first["candidate_set_sha256"]

    must_fail(
        [
            {"claim_id": CLAIM_A, "semantic_score": 0.9},
            {"claim_id": CLAIM_A, "semantic_score": 0.4},
        ],
        "duplicate claim",
    )
    must_fail(
        [
            {"claim_id": CLAIM_A, "semantic_score": 0.4},
            {"claim_id": CLAIM_B, "semantic_score": 0.9},
        ],
        "not rank ordered",
    )
    must_fail(
        [{"claim_id": CLAIM_A, "semantic_score": 1.1}],
        "semantic score",
    )
    print("memory_v1_v5_shadow_candidate: PASS")


if __name__ == "__main__":
    main()
