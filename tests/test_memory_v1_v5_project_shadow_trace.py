from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from rag_engine.memory_v1_v5_project_shadow_trace import (
    build_v5_project_shadow_trace,
    run_memory_v1_v5_project_shadow_trace,
)
from rag_engine.memory_v1_v5_project_shadow_trace_store import (
    V5ProjectShadowTraceStoreError,
    _payload,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
THREAD = "d776c8ef-7f3d-45b2-8820-4be87b7ca19d"


def record(*, suffix: str = "1", text: str = "Memory V1 is the governed memory system.") -> dict:
    knowledge = f"00000000-0000-4000-8000-00000000000{suffix}"
    revision = f"10000000-0000-4000-8000-00000000000{suffix}"
    return {
        "owner_user_id": OWNER,
        "thread_id": THREAD,
        "project_id": "08cd6a8a-5599-43d5-8d5c-b59401df8ccc",
        "project_key": "verbal-sage",
        "component_id": "1d3436df-cc4d-4b70-a6b9-730d91055b24",
        "component_key": "memory-v1",
        "knowledge_id": knowledge,
        "revision_id": revision,
        "content_sha256": suffix * 64,
        "canonical_text": text,
    }


class ProjectShadowTraceTests(unittest.TestCase):
    def test_exact_project_record_is_hashed_and_never_exposed(self) -> None:
        source = record()
        with patch(
            "rag_engine.memory_v1_v5_project_shadow_trace.load_v5_shadow_project_knowledge",
            new=AsyncMock(return_value=[source]),
        ):
            trace = asyncio.run(
                build_v5_project_shadow_trace(
                    object(),
                    OWNER,
                    query="What is the status of the memory system?",
                    request_classification="MEMORY_ARCHITECTURE",
                    request_id="request-1",
                    thread_id=THREAD,
                )
            )
        encoded = json.dumps(trace, sort_keys=True)
        self.assertEqual(trace["memory_lane"], "project_knowledge")
        self.assertEqual(trace["candidate_count"], 1)
        self.assertEqual(trace["selected_count"], 1)
        self.assertNotIn(source["canonical_text"], encoded)
        self.assertNotIn("canonical_text", trace)
        self.assertFalse(trace["prompt_injection"])
        self.assertFalse(trace["answer_model_exposure"])
        self.assertFalse(trace["retrieval_activation"])
        self.assertEqual(trace["qdrant_reads"], 0)

    def test_token_budget_rejection_reconciles(self) -> None:
        records = [
            record(suffix="1", text="short"),
            record(suffix="2", text="x" * 200),
        ]
        with patch(
            "rag_engine.memory_v1_v5_project_shadow_trace.load_v5_shadow_project_knowledge",
            new=AsyncMock(return_value=records),
        ):
            trace = asyncio.run(
                build_v5_project_shadow_trace(
                    object(),
                    OWNER,
                    query="Where are we with Memory V1?",
                    request_classification="MEMORY_ARCHITECTURE",
                    request_id="request-2",
                    thread_id=THREAD,
                    max_items=4,
                    max_tokens=10,
                )
            )
        self.assertEqual(trace["candidate_count"], 2)
        self.assertEqual(trace["selected_count"], 1)
        self.assertEqual(trace["rejected_counts"], {"token_budget": 1})

    def test_nonproject_turn_stops_before_database_access(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_V5_PROJECT_SHADOW": "1",
                "MEMORY_V1_V5_PROJECT_SHADOW_USER_IDS": OWNER,
            },
            clear=False,
        ):
            result = run_memory_v1_v5_project_shadow_trace(
                OWNER,
                query="How do I restart my Mac?",
                request_classification="TECH",
                request_id="request-3",
                thread_id=THREAD,
            )
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["persistable"])

    def test_trace_store_rejects_raw_project_text(self) -> None:
        source = record()
        with patch(
            "rag_engine.memory_v1_v5_project_shadow_trace.load_v5_shadow_project_knowledge",
            new=AsyncMock(return_value=[source]),
        ):
            trace = asyncio.run(
                build_v5_project_shadow_trace(
                    object(),
                    OWNER,
                    query="Summarize the memory project status.",
                    request_classification="MEMORY_ARCHITECTURE",
                    request_id="request-4",
                    thread_id=THREAD,
                )
            )
        payload = _payload(OWNER, trace)
        self.assertEqual(payload["candidate_count"], 1)
        unsafe = dict(trace)
        unsafe["canonical_text"] = source["canonical_text"]
        with self.assertRaises(V5ProjectShadowTraceStoreError):
            _payload(OWNER, unsafe)


if __name__ == "__main__":
    unittest.main()
