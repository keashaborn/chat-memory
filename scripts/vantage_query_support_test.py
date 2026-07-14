#!/usr/bin/env python3
from __future__ import annotations

from rag_engine.vantage_query_support import (
    build_meta_explanation,
    is_pure_reentry_greeting,
)


def main() -> int:
    assert is_pure_reentry_greeting("Hi")
    assert is_pure_reentry_greeting("I'm back")
    assert not is_pure_reentry_greeting("Hi, help me plan dinner")
    assert not is_pure_reentry_greeting("Explain this")

    chunks = [
        {
            "collection": "memory_raw",
            "payload": {
                "feedback": {"positive_signals": 2, "negative_signals": 1},
                "tags": ["topic:memory", "format:prose"],
            },
        }
    ]
    meta = build_meta_explanation("owner", "hello", chunks)

    assert meta["feedback_summary"] == {"positive": 2, "negative": 1}
    assert meta["topic_tags"] == ["memory"]
    assert meta["consistency"]["historical_format"] == "prose-leaning"
    assert "gravity" not in meta
    assert "temporal" not in meta
    print("vantage_query_support_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
