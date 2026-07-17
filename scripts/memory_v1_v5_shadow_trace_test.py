#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_retrieval import V5ShadowRetrievalError
from rag_engine.memory_v1_v5_shadow_trace import (
    build_v5_shadow_trace,
    classify_v5_shadow_context,
    run_memory_v1_v5_shadow_trace,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"
CANONICAL_TEXT = "The user works as a personal trainer."


class Transaction:
    def __init__(self, connection, readonly: bool) -> None:
        self.connection = connection
        self.readonly = readonly

    async def __aenter__(self):
        self.connection.transactions.append(self.readonly)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.transactions: list[bool] = []
        self.actor = None
        self.requested = None

    def transaction(self, *, readonly: bool = False):
        return Transaction(self, readonly)

    async def execute(self, statement, actor):
        assert "set_config('app.user_id'" in statement
        self.actor = actor

    async def fetch(self, statement, requested):
        assert "memory.read_v5_shadow_claims" in statement
        self.requested = requested
        return self.rows


class FakeIndex:
    collection_name = "memory_claim_v1"

    def search_claims(self, actor, vector, *, limit):
        assert str(actor) == OWNER
        assert vector == [1.0, 0.0, 0.5]
        assert limit == 24
        return [{"claim_id": CLAIM, "semantic_score": 0.91}]


def record(owner: str = OWNER) -> dict:
    return {
        "owner_user_id": owner,
        "claim_id": CLAIM,
        "canonical_key": "v5:" + "a" * 64,
        "canonical_text": CANONICAL_TEXT,
        "predicate": "occupation.works_as",
        "status": "supported",
        "sensitivity": "medium",
        "importance": 0.6,
        "salience": 0.5,
        "valid_from": "2026-07-14T17:15:20+00:00",
        "valid_to": None,
        "metadata": {"memory_contract": "memory_projection_v5"},
        "retrieval_policy": {
            "surface_policy": "direct_or_relevant",
            "projection_class": "direct_claim",
        },
        "projection_review_decision": "authorized",
        "projection_apply_outcome": "applied",
        "evidence_by_stance": {
            "supports": ["fca9e5dc-83c2-4456-8db8-1fe6102eb74d"],
            "opposes": [],
            "qualifies": [],
            "context": [],
        },
        "observation_ids": ["9bf1e6b2-1840-4524-98dc-142567ebe013"],
        "project_key": None,
    }


def main() -> None:
    context = classify_v5_shadow_context(
        "What kind of work do I do?",
        "SPECIFIC_RECALL",
    )
    assert context["eligible"] is True
    assert context["domain"] == "profile"
    assert "occupation.works_as" in context["allowed_predicate_prefixes"]

    assert classify_v5_shadow_context(
        "How do I add a PostgreSQL index?", "TECH"
    ) == {"eligible": False, "reason": "turn_intent:tech"}
    assert classify_v5_shadow_context(
        "What kind of music do I like?", "SPECIFIC_RECALL"
    ) == {"eligible": False, "reason": "specialized_memory_route"}

    connection = FakeConnection([record()])
    trace = asyncio.run(
        build_v5_shadow_trace(
            connection,
            FakeIndex(),
            OWNER,
            query="What kind of work do I do?",
            query_vector=[1.0, 0.0, 0.5],
            context=context,
            request_id="request-1",
            thread_id="thread-1",
        )
    )
    assert connection.transactions == [True, False]
    assert connection.actor == OWNER
    assert [str(value) for value in connection.requested] == [CLAIM]
    assert trace["selected_count"] == 1
    assert trace["visible_candidate_count"] == 1
    assert trace["database_transaction"] == "read_only"
    assert trace["database_writes"] == 0
    assert trace["qdrant_writes"] == 0
    assert trace["trace_writes"] == 0
    assert trace["prompt_injection"] is False
    assert trace["answer_model_exposure"] is False
    assert trace["retrieval_activation"] is False
    serialized = json.dumps(trace, sort_keys=True)
    assert CANONICAL_TEXT not in serialized
    assert '"claims"' not in serialized

    absent = asyncio.run(
        build_v5_shadow_trace(
            FakeConnection([]),
            FakeIndex(),
            OWNER,
            query="What kind of work do I do?",
            query_vector=[1.0, 0.0, 0.5],
            context=context,
        )
    )
    assert absent["selected_count"] == 0
    assert absent["rejected_counts"] == {"not_visible": 1}

    try:
        asyncio.run(
            build_v5_shadow_trace(
                FakeConnection([record(OTHER)]),
                FakeIndex(),
                OWNER,
                query="What kind of work do I do?",
                query_vector=[1.0, 0.0, 0.5],
                context=context,
            )
        )
    except V5ShadowRetrievalError as exc:
        assert "cross-owner" in str(exc)
    else:
        raise AssertionError("cross-owner V5 reader row was accepted")

    original = dict(os.environ)
    try:
        os.environ.pop("MEMORY_V1_V5_SHADOW", None)
        assert run_memory_v1_v5_shadow_trace(
            OWNER,
            query="What kind of work do I do?",
            request_classification="SPECIFIC_RECALL",
        ) == {
            "version": "memory_v1_v5_shadow_trace_v1",
            "status": "disabled",
        }
    finally:
        os.environ.clear()
        os.environ.update(original)

    print("memory_v1_v5_shadow_trace: PASS")


if __name__ == "__main__":
    main()
