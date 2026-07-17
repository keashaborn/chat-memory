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

from rag_engine import memory_v1_v5_shadow_trace_store as store


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def trace() -> dict:
    return {
        "version": "memory_v1_v5_shadow_trace_v1",
        "status": "ok",
        "outcome_code": "evaluated",
        "owner_user_id_sha256": "0" * 64,
        "request_id_sha256": "1" * 64,
        "thread_id_sha256": "2" * 64,
        "request_binding_sha256": "3" * 64,
        "query_sha256": "4" * 64,
        "persistable": True,
        "intent": "personal_recall",
        "domain": "profile",
        "candidate_set_sha256": "5" * 64,
        "selection_set_sha256": "6" * 64,
        "candidate_count": 1,
        "visible_candidate_count": 0,
        "selected_count": 0,
        "token_estimate": 0,
        "rejected_counts": {"not_visible": 1},
        "candidate_limit": 24,
        "max_claims": 4,
        "max_tokens": 500,
        "max_sensitivity": "medium",
        "database_transaction": "read_only",
        "database_writes": 0,
        "qdrant_writes": 0,
        "trace_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


def main() -> None:
    payload = store._payload(OWNER, trace(), embedding_provider_calls=1)
    assert str(payload["actor"]) == OWNER
    assert payload["rejected_counts"] == {"not_visible": 1}

    invalid = trace()
    invalid["query"] = "raw query must not persist"
    try:
        store._payload(OWNER, invalid, embedding_provider_calls=1)
    except store.V5ShadowTraceStoreError as exc:
        assert "forbidden field query" in str(exc)
    else:
        raise AssertionError("raw query field was accepted")

    invalid = trace()
    invalid["rejected_counts"] = {}
    try:
        store._payload(OWNER, invalid, embedding_provider_calls=1)
    except store.V5ShadowTraceStoreError as exc:
        assert "do not reconcile" in str(exc)
    else:
        raise AssertionError("unreconciled rejection counts were accepted")

    try:
        store._payload(OWNER, trace(), embedding_provider_calls=2)
    except store.V5ShadowTraceStoreError as exc:
        assert "embedding_provider_calls" in str(exc)
    else:
        raise AssertionError("duplicate provider calls were accepted")

    original_record = store._record
    original = dict(os.environ)
    captured = {}

    async def fake_record(dsn, value):
        captured["dsn"] = dsn
        captured["payload"] = value
        return {
            "trace_event_id": "ae91696e-5430-463d-8738-6e710af92ef9",
            "outcome": "applied",
            "trace_manifest_sha256": "7" * 64,
            "rows_written": 1,
        }

    try:
        store._record = fake_record
        os.environ["MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE"] = "1"
        os.environ["POSTGRES_DSN"] = "postgresql://unit-test"
        result = store.persist_memory_v1_v5_shadow_trace(
            OWNER,
            trace(),
            embedding_provider_calls=1,
        )
        assert result["status"] == "ok"
        assert result["outcome"] == "applied"
        assert result["rows_written"] == 1
        assert captured["dsn"] == "postgresql://unit-test"
        serialized = json.dumps(result, sort_keys=True)
        assert "raw query" not in serialized
        assert "claims" not in serialized
    finally:
        store._record = original_record
        os.environ.clear()
        os.environ.update(original)

    print("memory_v1_v5_shadow_trace_store: PASS")


if __name__ == "__main__":
    main()
