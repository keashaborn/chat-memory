#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_trace_store import (
    persist_memory_v1_v5_shadow_trace,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def trace() -> dict:
    return {
        "version": "memory_v1_v5_shadow_trace_v1",
        "status": "skipped",
        "outcome_code": "no_governed_claim_route",
        "owner_user_id_sha256": "0" * 64,
        "request_id_sha256": "b" * 64,
        "thread_id_sha256": "c" * 64,
        "request_binding_sha256": "d" * 64,
        "query_sha256": "e" * 64,
        "persistable": True,
        "intent": None,
        "domain": None,
        "candidate_set_sha256": "f" * 64,
        "selection_set_sha256": "f" * 64,
        "candidate_count": 0,
        "visible_candidate_count": 0,
        "selected_count": 0,
        "token_estimate": 0,
        "rejected_counts": {},
        "candidate_limit": 24,
        "max_claims": 4,
        "max_tokens": 500,
        "max_sensitivity": "medium",
        "database_transaction": "none",
        "database_writes": 0,
        "qdrant_writes": 0,
        "trace_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


def main() -> None:
    if not os.getenv("POSTGRES_DSN"):
        raise SystemExit("POSTGRES_DSN is required")
    os.environ["MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE"] = "1"
    applied = persist_memory_v1_v5_shadow_trace(
        OWNER, trace(), embedding_provider_calls=0
    )
    replayed = persist_memory_v1_v5_shadow_trace(
        OWNER, trace(), embedding_provider_calls=0
    )
    assert applied["status"] == "ok" and applied["outcome"] == "applied"
    assert applied["rows_written"] == 1
    assert replayed["status"] == "ok" and replayed["outcome"] == "replayed"
    assert replayed["rows_written"] == 0
    assert applied["trace_event_id_sha256"] == replayed["trace_event_id_sha256"]
    assert applied["trace_manifest_sha256"] == replayed["trace_manifest_sha256"]
    print("memory_v1_v5_shadow_trace_store_clone: PASS")


if __name__ == "__main__":
    main()
