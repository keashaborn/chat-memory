#!/usr/bin/env python3
from __future__ import annotations

from rag_engine.memory_v1_shadow import classify_shadow_context


def expect(message: str, turn_intent: str, *, domain: str, intent: str) -> None:
    result = classify_shadow_context(message, turn_intent)
    if not result["eligible"]:
        raise AssertionError(f"expected eligible context: {result}")
    if result["domain"] != domain or result["intent"] != intent:
        raise AssertionError(
            f"expected {domain}/{intent}, got {result['domain']}/{result['intent']}"
        )


def main() -> int:
    expect(
        "Was the correct spelling Nemo or Neko?",
        "SPECIFIC_RECALL",
        domain="name_correction",
        intent="personal_recall",
    )
    expect(
        "What do you remember about my mother dying?",
        "SPECIFIC_RECALL",
        domain="family_death",
        intent="personal_recall",
    )
    expect(
        "Do you remember when I lost my pet?",
        "SPECIFIC_RECALL",
        domain="pet_loss",
        intent="personal_recall",
    )
    expect(
        "I am struggling with caregiving for my wife.",
        "GENERAL",
        domain="life_context",
        intent="relevant_support",
    )

    technical = classify_shadow_context(
        "How do I create a PostgreSQL index?", "TECH"
    )
    if technical != {"eligible": False, "reason": "turn_intent:tech"}:
        raise AssertionError(f"technical query was not suppressed: {technical}")

    unrelated = classify_shadow_context("What should I eat today?", "GENERAL")
    if unrelated != {"eligible": False, "reason": "unclassified_domain"}:
        raise AssertionError(f"unclassified query was not suppressed: {unrelated}")

    print("memory_v1_shadow_policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
