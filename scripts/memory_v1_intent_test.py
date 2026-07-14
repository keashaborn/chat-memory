#!/usr/bin/env python3
from __future__ import annotations

import inspect

from rag_engine.memory_v1_intent import (
    apply_legacy_personal_memory_gate,
    classify_legacy_personal_memory_access,
    classify_memory_intent,
    resolve_legacy_personal_memory_mode,
)


def expect(
    message: str,
    request_classification: str,
    *,
    memory_intent: str,
    domains: list[str],
    specialized: bool,
    governed: bool | None = None,
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
    if governed is not None and bool(result["routes"]["governed_claims"]) != governed:
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
        "What do you actually remember about my music preferences?",
        "GENERAL",
        memory_intent="preference_recall",
        domains=["music"],
        specialized=True,
    )
    expect(
        "If you had a guess, what do you think my favorite artist would be?",
        "SPECIFIC_RECALL",
        memory_intent="preference_recall",
        domains=["music"],
        specialized=True,
    )
    expect(
        "I've got this song stuck in my head, but I'm not really sure who sings it. It kind of goes: I've been around for you. I've been up and down for you, but I just can't get any release. Do you know what song that is and who sang it?",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
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
        "So far, I don't have Internet access integrated into the app. Do you think that would be a good thing to integrate?",
        "GENERAL",
        memory_intent="project_planning",
        domains=["project"],
        specialized=True,
    )
    expect(
        "I am worried about the app or website cost if ChatGPT usage grows, so I may need to build a cost structure.",
        "TECH",
        memory_intent="project_planning",
        domains=["project"],
        specialized=True,
    )
    expect(
        "Can you tell me more about adaptive learning, and how that would fit into a memory system?",
        "GENERAL",
        memory_intent="project_planning",
        domains=["memory_architecture"],
        specialized=True,
    )
    expect(
        "I'm thinking about turning the website that I'm creating into an Apple app.",
        "GENERAL",
        memory_intent="project_planning",
        domains=["project"],
        specialized=True,
    )
    expect(
        "Could we add APIs to the Verbal Sage app that connect to expert nutrition and weightlifting guidance databases so the system can keep learning?",
        "GENERAL",
        memory_intent="project_planning",
        domains=["project"],
        specialized=True,
    )
    expect(
        "What do you know about my pets?",
        "PROFILE_SUMMARY",
        memory_intent="personal_recall",
        domains=["pet_loss"],
        specialized=False,
        governed=True,
    )
    expect(
        "My mother died back in March. She was about 87 years old.",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
        governed=False,
    )
    expect(
        "After Neko died, I got a white male Maine coon cat.",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
        governed=False,
    )
    expect(
        "I married Monika in my early 30s. She had a three year old son named Justin.",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
        governed=False,
    )
    expect(
        "Can you recommend a guitar practice app?",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
    )
    expect(
        "Do you think the apple is ripe?",
        "GENERAL",
        memory_intent="none",
        domains=[],
        specialized=False,
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

    song_gate = classify_legacy_personal_memory_access(
        "I've got this song stuck in my head, but I'm not really sure who sings it. I've been around for you. I've been up and down for you, but I just can't get any release. Do you know what song that is and who sang it?",
        request_classification="GENERAL",
        mode="on",
    )
    if song_gate["allowed"] or song_gate["reason"] != "song_identification_uses_live_context_only":
        raise AssertionError(song_gate)

    favorite_gate = classify_legacy_personal_memory_access(
        "Guess what my favorite artist would be.",
        request_classification="SPECIFIC_RECALL",
        mode="specific_recall_only",
    )
    if favorite_gate["allowed"] or favorite_gate["reason"] != "curated_memory_route_required":
        raise AssertionError(favorite_gate)

    claim_gate = classify_legacy_personal_memory_access(
        "What happened with my mom?",
        request_classification="GENERAL",
        mode="specific_recall_only",
    )
    if (
        not claim_gate["allowed"]
        or claim_gate["reason"] != "narrow_personal_recall_not_yet_replaced"
    ):
        raise AssertionError(claim_gate)

    named_claim_gate = classify_legacy_personal_memory_access(
        "What happened to DeeDee?",
        request_classification="GENERAL",
        mode="specific_recall_only",
    )
    if (
        not named_claim_gate["allowed"]
        or named_claim_gate["reason"] != "narrow_personal_recall_not_yet_replaced"
    ):
        raise AssertionError(named_claim_gate)

    general_gate = classify_legacy_personal_memory_access(
        "What should I cook tonight?",
        request_classification="GENERAL",
        mode="specific_recall_only",
    )
    if general_gate["allowed"] or general_gate["reason"] != "not_narrow_personal_recall":
        raise AssertionError(general_gate)

    actor = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
    if resolve_legacy_personal_memory_mode(
        actor,
        default_mode="on",
        safe_user_ids=actor,
    ) != "specific_recall_only":
        raise AssertionError("safe-user override failed")
    if resolve_legacy_personal_memory_mode(
        actor,
        default_mode="on",
        safe_user_ids=actor,
        off_user_ids=actor,
    ) != "off":
        raise AssertionError("off-user override must win")
    if resolve_legacy_personal_memory_mode(
        actor,
        default_mode="invalid",
    ) != "off":
        raise AssertionError("invalid legacy mode did not fail closed")

    gated_plan, gate_audit = apply_legacy_personal_memory_gate(
        {
            "k_personal": 10,
            "k_corpus": 5,
            "personal_archive_enabled": True,
            "corpus_enabled": True,
        },
        song_gate,
    )
    if gated_plan["k_personal"] != 0 or gated_plan["personal_archive_enabled"]:
        raise AssertionError(gated_plan)
    if gated_plan["k_corpus"] != 0 or gated_plan["corpus_enabled"]:
        raise AssertionError(gated_plan)
    if gate_audit["requested_k_personal"] != 10 or gate_audit["effective_k_personal"] != 0:
        raise AssertionError(gate_audit)
    if gate_audit["requested_k_corpus"] != 5 or gate_audit["effective_k_corpus"] != 0:
        raise AssertionError(gate_audit)

    print("memory_v1_intent: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
