#!/usr/bin/env python3
"""
Preview durable personal-event candidates from Qdrant memory_raw.

Read-only. No writes.
Goal: inspect whether raw conversational memory contains facts that should become
structured memory cards rather than relying on exact vector recall.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import requests

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memory_raw"
USER_ID = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


EVENT_RULES = [
    {
        "event_type": "death_loss",
        "domain": ["family", "grief", "life_event"],
        "salience": "high",
        "patterns": [
            r"\b(my )?(mother|mom|mum)\b.{0,80}\b(died|death|passed away)\b",
            r"\b(died|death|passed away)\b.{0,80}\b(my )?(mother|mom|mum)\b",
            r"\b(my )?(father|dad)\b.{0,80}\b(died|death|passed away)\b",
            r"\b(died|death|passed away)\b.{0,80}\b(my )?(father|dad)\b",
        ],
        "use_scope": "DIRECT_RECALL_OR_RELEVANT_SUPPORT",
        "surface_policy": "mention_when_directly_relevant",
    },
    {
        "event_type": "pet_death_loss",
        "domain": ["pets", "grief", "life_event"],
        "salience": "high",
        "patterns": [
            r"\b(dog|dogs|cat|cats|pet|pets|german shepherd|maine coon)\b.{0,120}\b(died|death|put .*sleep|put .*down|euthanized|lost)\b",
            r"\b(died|death|put .*sleep|put .*down|euthanized|lost)\b.{0,120}\b(dog|dogs|cat|cats|pet|pets|german shepherd|maine coon)\b",
            r"\b(Dahlia|Helsing|Neko|Nemo)\b.{0,120}\b(died|death|put .*sleep|put .*down|euthanized|lost)\b",
        ],
        "use_scope": "DIRECT_RECALL_OR_RELEVANT_SUPPORT",
        "surface_policy": "mention_when_directly_relevant",
    },
    {
        "event_type": "caretaking_burden",
        "domain": ["family", "caretaking", "life_context"],
        "salience": "medium_high",
        "patterns": [
            r"\b(caring for|taking care of|caretaking|caregiving)\b.{0,120}\b(wife|mother|mom|father|dad|Monika|Jerry)\b",
            r"\b(wife|Monika)\b.{0,160}\b(psychotic break|psychosis|not been the same)\b",
            r"\b(dad|father|Jerry)\b.{0,160}\b(assisted living|memory lasts|memory)\b",
        ],
        "use_scope": "STYLE_AND_RELEVANT_SUPPORT",
        "surface_policy": "influence_when_relevant",
    },
    {
        "event_type": "relationship_anchor",
        "domain": ["family", "pets", "identity_context"],
        "salience": "medium",
        "patterns": [
            r"\b(my mom'?s name|mother'?s name)\b.{0,80}\bDeeDee\b",
            r"\b(my dad'?s name|father'?s name)\b.{0,80}\bJerry\b",
            r"\b(Dahlia|Helsing|Neko|Nyx|Арктика)\b",
        ],
        "use_scope": "DIRECT_RECALL",
        "surface_policy": "mention_when_asked",
    },
]


def scroll_points() -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    offset = None

    for _ in range(50):
        body: Dict[str, Any] = {
            "limit": 100,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset

        r = requests.post(
            f"{QDRANT}/collections/{COLLECTION}/points/scroll",
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        result = r.json().get("result") or {}
        batch = result.get("points") or []
        offset = result.get("next_page_offset")

        points.extend(batch)

        if offset is None:
            break

    return points


def text_for_payload(payload: Dict[str, Any]) -> str:
    return " ".join(str(payload.get("text") or "").split()).strip()


def user_matches(payload: Dict[str, Any]) -> bool:
    return (
        payload.get("user_id") == USER_ID
        or payload.get("user_id_alias") == USER_ID
    )


def source_is_user(payload: Dict[str, Any]) -> bool:
    src = str(payload.get("source") or "")
    tags = payload.get("tags") or []
    return src.endswith(":user") or "user" in tags


def looks_like_memory_system_checkpoint(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    checkpoint_cues = (
        "verbal sage memory system checkpoint",
        "target memory system",
        "current memory stores",
        "memory architecture",
        "user-owned memory pool",
        "qdrant episodic/vector archive",
        "use_scope",
        "storage should be broad",
        "surfacing should be disciplined",
    )
    return any(cue in low for cue in checkpoint_cues)


def find_candidates(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    for pt in points:
        payload = pt.get("payload") or {}
        if not user_matches(payload):
            continue
        if not source_is_user(payload):
            continue

        text = text_for_payload(payload)
        if not text:
            continue
        if looks_like_memory_system_checkpoint(text):
            continue

        for rule in EVENT_RULES:
            matched_patterns = []
            for pat in rule["patterns"]:
                if re.search(pat, text, re.I | re.S):
                    matched_patterns.append(pat)

            if not matched_patterns:
                continue

            candidate = {
                "candidate_id": f"personal_event:{pt.get('id')}:{rule['event_type']}",
                "source_point_id": pt.get("id"),
                "source": payload.get("source"),
                "thread_id": payload.get("thread_id"),
                "vantage_id": payload.get("vantage_id"),
                "created_at": payload.get("created_at"),
                "event_type": rule["event_type"],
                "domain": rule["domain"],
                "salience": rule["salience"],
                "use_scope": rule["use_scope"],
                "surface_policy": rule["surface_policy"],
                "matched_pattern_count": len(matched_patterns),
                "matched_patterns": matched_patterns[:3],
                "text_preview": text[:1200],
            }
            candidates.append(candidate)

    def sort_key(c: Dict[str, Any]):
        salience_rank = {"high": 0, "medium_high": 1, "medium": 2}.get(c.get("salience"), 9)
        return (salience_rank, str(c.get("created_at") or ""))

    return sorted(candidates, key=sort_key)


def main() -> int:
    points = scroll_points()
    candidates = find_candidates(points)

    print("=== personal event inventory preview ===")
    print("mode: read_only")
    print("collection:", COLLECTION)
    print("user_id:", USER_ID)
    print("points_scanned:", len(points))
    print("candidate_count:", len(candidates))

    counts: Dict[str, int] = {}
    for c in candidates:
        counts[c["event_type"]] = counts.get(c["event_type"], 0) + 1

    print("event_type_counts:", json.dumps(counts, ensure_ascii=False, sort_keys=True))

    for i, c in enumerate(candidates[:40], 1):
        print(f"\n--- candidate {i} ---")
        print(json.dumps(c, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
