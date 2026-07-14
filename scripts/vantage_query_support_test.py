#!/usr/bin/env python3
from __future__ import annotations

from unittest.mock import patch

from rag_engine.vantage_query_support import (
    build_meta_explanation,
    is_pure_reentry_greeting,
)


class TemporalResponse:
    ok = True

    @staticmethod
    def json() -> dict:
        return {"seconds_since_last_user_message": 12.0, "bucket": "recent"}


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
    with patch(
        "rag_engine.vantage_query_support.load_gravity_profile",
        return_value={},
    ), patch(
        "rag_engine.vantage_query_support.requests.get",
        return_value=TemporalResponse(),
    ):
        meta = build_meta_explanation("owner", "hello", chunks)

    assert meta["feedback_summary"] == {"positive": 2, "negative": 1}
    assert meta["topic_tags"] == ["memory"]
    assert meta["consistency"]["historical_format"] == "prose-leaning"
    assert meta["temporal"] == {
        "seconds_since_last_user_message": 12.0,
        "bucket": "recent",
    }
    print("vantage_query_support_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
