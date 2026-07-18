#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid

from rag_engine.memory_v1_v5_project_shadow_trace import (
    run_memory_v1_v5_project_shadow_trace,
)
from rag_engine.memory_v1_v5_project_shadow_trace_store import (
    persist_memory_v1_v5_project_shadow_trace,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one sanitized, no-prompt project shadow probe"
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-selected", required=True, type=int)
    parser.add_argument(
        "--query",
        default="What is the current state of the Memory V1 architecture?",
    )
    return parser.parse_args()


def main() -> int:
    args = arguments()
    owner = str(uuid.UUID(args.owner_user_id))
    thread = str(uuid.UUID(args.thread_id))
    uuid.UUID(args.request_id)
    trace = run_memory_v1_v5_project_shadow_trace(
        owner,
        query=args.query,
        request_classification="MEMORY_ARCHITECTURE",
        request_id=args.request_id,
        thread_id=thread,
    )
    if trace.get("status") != "ok":
        raise RuntimeError(f"project shadow probe failed: {trace.get('error_type')}")
    if int(trace.get("selected_count") or 0) != args.expected_selected:
        raise RuntimeError("project shadow selected count changed")
    if any(
        trace.get(field) is not False
        for field in ("prompt_injection", "answer_model_exposure", "retrieval_activation")
    ):
        raise RuntimeError("project shadow unexpectedly influenced the answer path")
    persistence = persist_memory_v1_v5_project_shadow_trace(owner, trace)
    if persistence.get("status") != "ok":
        raise RuntimeError(
            f"project trace persistence failed: {persistence.get('error_type')}"
        )
    output = {
        "version": trace["version"],
        "status": trace["status"],
        "outcome_code": trace["outcome_code"],
        "request_id_sha256": trace["request_id_sha256"],
        "thread_id_sha256": trace["thread_id_sha256"],
        "request_binding_sha256": trace["request_binding_sha256"],
        "query_sha256": trace["query_sha256"],
        "project_scope_sha256": trace["project_scope_sha256"],
        "candidate_set_sha256": trace["candidate_set_sha256"],
        "selection_set_sha256": trace["selection_set_sha256"],
        "candidate_count": trace["candidate_count"],
        "selected_count": trace["selected_count"],
        "token_estimate": trace["token_estimate"],
        "rejected_counts": trace["rejected_counts"],
        "persistence": persistence,
        "database_writes": trace["database_writes"],
        "qdrant_reads": trace["qdrant_reads"],
        "qdrant_writes": trace["qdrant_writes"],
        "external_model_calls": trace["external_model_calls"],
        "prompt_injection": trace["prompt_injection"],
        "answer_model_exposure": trace["answer_model_exposure"],
        "retrieval_activation": trace["retrieval_activation"],
    }
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
