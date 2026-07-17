#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_trace import (
    build_v5_shadow_trace,
    classify_v5_shadow_context,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"


class FixtureIndex:
    collection_name = "memory_claim_v1_clone_fixture"

    def search_claims(self, actor, vector, *, limit):
        assert vector == [1.0, 0.0, 0.5]
        assert limit == 24
        return [{"claim_id": CLAIM, "semantic_score": 0.91}]


async def main() -> int:
    dsn = os.environ["POSTGRES_DSN"]
    context = classify_v5_shadow_context(
        "What kind of work do I do?",
        "SPECIFIC_RECALL",
    )
    assert context["eligible"] is True
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        selected = await build_v5_shadow_trace(
            conn,
            FixtureIndex(),
            OWNER,
            query="What kind of work do I do?",
            query_vector=[1.0, 0.0, 0.5],
            context=context,
            request_id="clone-owner-a",
            thread_id="clone-thread-a",
        )
        isolated = await build_v5_shadow_trace(
            conn,
            FixtureIndex(),
            OTHER,
            query="What kind of work do I do?",
            query_vector=[1.0, 0.0, 0.5],
            context=context,
            request_id="clone-owner-b",
            thread_id="clone-thread-b",
        )
    finally:
        await conn.close()

    assert selected["selected_count"] == 1
    assert selected["visible_candidate_count"] == 1
    assert selected["rejected_counts"] == {}
    assert isolated["selected_count"] == 0
    assert isolated["visible_candidate_count"] == 0
    assert isolated["rejected_counts"] == {"not_visible": 1}
    for trace in (selected, isolated):
        assert trace["database_transaction"] == "read_only"
        assert trace["database_writes"] == 0
        assert trace["qdrant_writes"] == 0
        assert trace["trace_writes"] == 0
        assert trace["prompt_injection"] is False
        assert trace["answer_model_exposure"] is False
        assert trace["retrieval_activation"] is False
        serialized = json.dumps(trace, sort_keys=True)
        assert "personal trainer" not in serialized.casefold()
        assert '"claims"' not in serialized

    print(
        json.dumps(
            {
                "owner_selected_count": selected["selected_count"],
                "other_owner_selected_count": isolated["selected_count"],
                "other_owner_rejected_counts": isolated["rejected_counts"],
                "owner_request_binding_sha256": selected[
                    "request_binding_sha256"
                ],
                "other_owner_request_binding_sha256": isolated[
                    "request_binding_sha256"
                ],
                "prompt_influence": False,
            },
            sort_keys=True,
        )
    )
    print("memory_v1_v5_shadow_trace_clone: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
