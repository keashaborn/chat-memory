#!/usr/bin/env python3
from __future__ import annotations

import inspect

from rag_engine.memory_v1_intent import classify_memory_intent


def expect(
    message: str,
    request_classification: str,
    *,
    memory_intent: str,
    domains: list[str],
    specialized: bool,
) -> None:
    result = classify_memory_intent(
        message,
        request_classification=request_classification,
    )
    if result["memory_intent"] != memory_intent:
        raise AssertionError(f"{message!r}: {result}")
    if result["domains"] != domains:
        raise AssertionError(f"{message!r}: {result}")
    if bool(result["routes"]["specialized"]) != specialized:
        raise AssertionError(f"{message!r}: {result}")


def main() -> int:
    if "response_mode" in inspect.signature(classify_memory_intent).parameters:
        raise AssertionError("response mode entered the memory-intent contract")

    expect(
        "Recommend music with strong vocals and layered meaning.",
        "GENERAL",
        memory_intent="recommendation",
        domains=["music"],
        specialized=True,
    )
    expect(
        "What kind of music do I prefer?",
        "SPECIFIC_RECALL",
        memory_intent="preference_recall",
        domains=["music"],
        specialized=True,
    )
    expect(
        "Explain the physics of musical harmonics.",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
    )
    expect(
        "Plan the Fractal Monism memory feedback loop.",
        "MEMORY_ARCHITECTURE",
        memory_intent="project_planning",
        domains=["memory_architecture"],
        specialized=True,
    )
    expect(
        "Which backend branch contains my current worktree?",
        "TECH",
        memory_intent="project_status",
        domains=["project"],
        specialized=True,
    )
    expect(
        "How was the website built through AI-assisted coding?",
        "TECH",
        memory_intent="project_recall",
        domains=["project_history"],
        specialized=True,
    )
    expect(
        "How can Fractal Monism say reality is one while distinctions still matter?",
        "FM_CONCEPTUAL",
        memory_intent="none",
        domains=[],
        specialized=False,
    )
    expect(
        "What happened with my mom?",
        "SPECIFIC_RECALL",
        memory_intent="personal_recall",
        domains=["family_death"],
        specialized=False,
    )
    expect(
        "What happened to DeeDee?",
        "SPECIFIC_RECALL",
        memory_intent="personal_recall",
        domains=["family_death"],
        specialized=True,
    )
    expect(
        "How do I stop popups when my Mac restarts?",
        "TECH",
        memory_intent="none",
        domains=[],
        specialized=False,
    )

    first = classify_memory_intent(
        "What is the current memory system status?",
        request_classification="MEMORY_ARCHITECTURE",
    )
    second = classify_memory_intent(
        "What is the current memory system status?",
        request_classification="MEMORY_ARCHITECTURE",
    )
    if first != second:
        raise AssertionError("memory-intent classification is not deterministic")
    if "vantage_id" in str(first).casefold() or "response_mode" in str(first).casefold():
        raise AssertionError("persona or response mode leaked into memory intent")

    print("memory_v1_intent: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
