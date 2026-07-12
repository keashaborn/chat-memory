#!/usr/bin/env python3
from __future__ import annotations

from rag_engine.memory_v1_seed import build_seed_mapping
from rag_engine.memory_v1_store import ProposalValidationError


OWNER = "11111111-1111-4111-8111-111111111111"
SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def card(card_id: int, kind: str, payload: dict, summary: str = "Summary") -> dict:
    return {
        "card_id": card_id,
        "kind": kind,
        "topic_key": f"user/{OWNER}/{kind}/test",
        "status": "active",
        "strength": 0.8,
        "confidence": 0.9,
        "summary": summary,
        "payload": {
            "user_id": OWNER,
            "review_status": "approved",
            "sensitivity": "medium",
            "source_point_ids": [SOURCE],
            **payload,
        },
    }


def main() -> int:
    family = build_seed_mapping(
        card(
            104,
            "personal_event",
            {
                "event_type": "death_loss",
                "event": "died",
                "subject": {
                    "known_name": "DeeDee",
                    "relation_to_user": "mother",
                },
                "domains": ["family", "grief"],
            },
        )
    )
    if family["proposal"]["retrieval_policy"]["domains"] != ["family_death"]:
        raise AssertionError("family event domain mapping failed")

    context = build_seed_mapping(
        card(
            105,
            "life_context",
            {
                "event_type": "caretaking_burden",
                "event": "ongoing",
                "subject": {
                    "known_name": "Monika",
                    "relation_to_user": "wife",
                },
                "domains": ["family", "caretaking"],
            },
        )
    )
    if context["proposal"]["retrieval_policy"]["surface"] != "support":
        raise AssertionError("life-context surface mapping failed")

    correction = build_seed_mapping(
        card(
            106,
            "correction",
            {
                "correction_type": "name_alias_correction",
                "entity_type": "pet",
                "incorrect_value": "Nemo",
                "canonical_value": "Neko",
            },
        )
    )
    if correction["proposal"]["object_literal"]["canonical_value"] != "Neko":
        raise AssertionError("correction canonical value mapping failed")

    pet = build_seed_mapping(
        card(
            107,
            "personal_event",
            {
                "event_type": "pet_death_loss",
                "event": "died_or_lost",
                "subject": {
                    "known_name": "Helsing",
                    "relation_to_user": "pet",
                },
                "domains": ["pets", "grief"],
            },
        )
    )
    if pet["proposal"]["retrieval_policy"]["domains"] != ["pet_loss"]:
        raise AssertionError("pet event domain mapping failed")

    broken = card(
        999,
        "correction",
        {
            "correction_type": "name_alias_correction",
            "entity_type": "pet",
            "incorrect_value": "A",
            "canonical_value": "B",
        },
    )
    broken["payload"]["user_id"] = "22222222-2222-4222-8222-222222222222"
    try:
        build_seed_mapping(broken)
    except ProposalValidationError:
        pass
    else:
        raise AssertionError("owner mismatch was accepted")

    print("memory_v1_seed_mapping: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
