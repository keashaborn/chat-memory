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


def canonical_subjects_for_text(text: str) -> List[Dict[str, str]]:
    low = text.lower()
    subjects: List[Dict[str, str]] = []

    def add(subject: str, relation: str, name: str = "") -> None:
        item = {"subject": subject, "relation_to_user": relation}
        if name:
            item["known_name"] = name
        if item not in subjects:
            subjects.append(item)

    if re.search(r"\b(my )?(mother|mom|mum)\b", low):
        add("mother", "mother", "DeeDee" if re.search(r"\bdeedee\b", text, re.I) else "")
    if re.search(r"\b(my )?(father|dad)\b", low):
        add("father", "father", "Jerry" if re.search(r"\bjerry\b", text, re.I) else "")
    if re.search(r"\bmonika\b|\bmy wife\b", text, re.I):
        add("Monika", "wife", "Monika")
    for pet in ("Dahlia", "Helsing", "Neko", "Nemo", "Nyx", "Арктика"):
        if re.search(rf"\b{re.escape(pet)}\b", text, re.I):
            add(pet, "pet", pet)

    return subjects


def canonical_event_for_rule(event_type: str, text: str) -> str:
    low = text.lower()
    if event_type == "death_loss":
        return "died"
    if event_type == "pet_death_loss":
        if "put her to sleep" in low or "put him to sleep" in low or "put to sleep" in low:
            return "euthanized_or_put_to_sleep"
        if "rare blood cancer" in low:
            return "died_of_rare_blood_cancer"
        return "died_or_lost"
    if event_type == "caretaking_burden":
        if "psychotic break" in low:
            return "ongoing_caretaking_after_wife_psychotic_break"
        if "assisted living" in low or "memory lasts" in low:
            return "father_assisted_living_memory_decline"
        return "caretaking_load"
    if event_type == "relationship_anchor":
        return "important_relationship_anchor"
    return event_type


def time_reference_for_text(text: str, created_at: str | None) -> str:
    low = text.lower()
    m = re.search(r"\babout\s+([a-z0-9 ]{1,30}?)\s+ago\b", low)
    if m:
        return f"about {m.group(1).strip()} ago from source date {created_at or 'unknown'}"
    m = re.search(r"\blast year\b", low)
    if m:
        return f"last year relative to source date {created_at or 'unknown'}"
    m = re.search(r"\btwo years ago\b", low)
    if m:
        return f"two years ago relative to source date {created_at or 'unknown'}"
    return f"source date {created_at or 'unknown'}"


def choose_primary_subject(
    *,
    event_type: str,
    event: str,
    text: str,
    subjects: List[Dict[str, str]],
) -> Dict[str, str]:
    low = text.lower()

    def first_subject_with_relation(relation: str) -> Dict[str, str] | None:
        for item in subjects:
            if item.get("relation_to_user") == relation:
                return item
        return None

    def subject_named(name: str) -> Dict[str, str] | None:
        for item in subjects:
            if (item.get("known_name") or item.get("subject") or "").lower() == name.lower():
                return item
        return None

    if event_type == "death_loss":
        if re.search(r"\b(my )?(mother|mom|mum)\b.{0,100}\b(died|death|passed away)\b", low, re.S):
            return subject_named("DeeDee") or first_subject_with_relation("mother") or {"subject": "mother", "relation_to_user": "mother"}
        if re.search(r"\b(my )?(father|dad)\b.{0,100}\b(died|death|passed away)\b", low, re.S):
            return subject_named("Jerry") or first_subject_with_relation("father") or {"subject": "father", "relation_to_user": "father"}

    if event_type == "pet_death_loss":
        pet_subjects = [s for s in subjects if s.get("relation_to_user") == "pet"]
        loss_mentions = 0
        loss_mentions += len(re.findall(r"\bput (?:her|him|them)?\s*(?:to sleep|down)\b", text, re.I))
        loss_mentions += len(re.findall(r"\b(died|death|rare blood cancer|lost)\b", text, re.I))
        if len(pet_subjects) > 1 and loss_mentions > 1:
            return {"subject": "multiple_pets", "relation_to_user": "pets"}

        if re.search(r"\bdahlia\b.{0,160}\b(put .*sleep|put .*down|euthanized|died|death|lost)\b", text, re.I | re.S):
            return subject_named("Dahlia") or {"subject": "Dahlia", "relation_to_user": "pet", "known_name": "Dahlia"}
        if re.search(r"\bhelsing\b.{0,160}\b(died|death|rare blood cancer|lost)\b", text, re.I | re.S):
            return subject_named("Helsing") or {"subject": "Helsing", "relation_to_user": "pet", "known_name": "Helsing"}
        if re.search(r"\bneko\b.{0,160}\b(died|death|lost|after neko dies)\b", text, re.I | re.S):
            return subject_named("Neko") or {"subject": "Neko", "relation_to_user": "pet", "known_name": "Neko"}
        if re.search(r"\bnemo\b.{0,160}\b(died|death|lost|put .*sleep)\b", text, re.I | re.S):
            return subject_named("Nemo") or {"subject": "Nemo", "relation_to_user": "pet", "known_name": "Nemo"}
        pet = first_subject_with_relation("pet")
        if pet:
            return pet

    if event_type == "caretaking_burden":
        if "psychotic break" in low or "wife" in low or "monika" in low:
            return subject_named("Monika") or {"subject": "Monika", "relation_to_user": "wife", "known_name": "Monika"}
        if "assisted living" in low or "memory lasts" in low or "dad" in low or "jerry" in low:
            return subject_named("Jerry") or first_subject_with_relation("father") or {"subject": "father", "relation_to_user": "father", "known_name": "Jerry"}

    if event_type == "relationship_anchor":
        for name in ("Dahlia", "Helsing", "Neko", "Nyx", "Арктика", "DeeDee", "Jerry", "Monika"):
            hit = subject_named(name)
            if hit:
                return hit

    return subjects[0] if subjects else {"subject": "unknown", "relation_to_user": "unknown"}


def build_correction_candidate(candidate: Dict[str, Any]) -> Dict[str, Any] | None:
    text = str(candidate.get("text_preview") or "")

    # V0: deterministic correction pattern for user-authored corrections.
    # Example: "It was Neko; spell check changed it accidentally to Nemo."
    if re.search(r"\bit was\s+neko\b", text, re.I) and re.search(r"\bnemo\b", text, re.I):
        return {
            "card_schema": "memory_correction_candidate_v0",
            "correction_type": "name_alias_correction",
            "canonical_value": "Neko",
            "incorrect_value": "Nemo",
            "entity_type": "pet",
            "claim": "User corrected a prior transcription/spellcheck error: the pet name should be Neko, not Nemo.",
            "applies_to_domains": ["pets", "relationship_anchor", "pet_death_loss"],
            "confidence": 0.92,
            "use_scope": "MEMORY_NORMALIZATION",
            "surface_policy": "do_not_surface_as_content_unless_asked",
            "source_point_id": candidate.get("source_point_id"),
            "source_thread_id": candidate.get("thread_id"),
            "source_vantage_id": candidate.get("vantage_id"),
            "source_created_at": candidate.get("created_at"),
            "needs_review": True,
            "write_intent": "none_preview_only",
        }

    return None


def build_card_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    text = str(candidate.get("text_preview") or "")
    event_type = str(candidate.get("event_type") or "")
    created_at = candidate.get("created_at")

    subjects = canonical_subjects_for_text(text)
    event = canonical_event_for_rule(event_type, text)
    primary_subject = choose_primary_subject(
        event_type=event_type,
        event=event,
        text=text,
        subjects=subjects,
    )

    card_type = "personal_life_event"
    if event_type == "relationship_anchor":
        card_type = "relationship_anchor"
    elif event_type == "caretaking_burden":
        card_type = "life_context"

    time_reference = time_reference_for_text(text, created_at)

    claim_parts = []
    subject_label = primary_subject.get("known_name") or primary_subject.get("subject") or "Unknown subject"
    relation = primary_subject.get("relation_to_user") or "unknown relation"

    if event_type == "death_loss":
        claim_parts.append(f"User's {relation} {subject_label} died; time reference: {time_reference}.")
    elif event_type == "pet_death_loss":
        if primary_subject.get("subject") == "multiple_pets":
            claim_parts.append(f"User experienced multiple pet losses; event: {event}; time reference: {time_reference}.")
        else:
            claim_parts.append(f"User experienced a pet loss involving {subject_label}; event: {event}; time reference: {time_reference}.")
    elif event_type == "caretaking_burden":
        claim_parts.append(f"User has a relevant caretaking/life-context burden involving {subject_label}; event: {event}.")
    elif event_type == "relationship_anchor":
        claim_parts.append(f"{subject_label} is a relevant relationship anchor for the user.")
    else:
        claim_parts.append(f"Candidate personal memory event: {event_type} involving {subject_label}.")

    return {
        "card_schema": "personal_memory_card_candidate_v0",
        "card_type": card_type,
        "event_type": event_type,
        "subject": primary_subject,
        "related_subjects": subjects,
        "event": event,
        "time_reference": time_reference,
        "claim": " ".join(claim_parts),
        "domain": candidate.get("domain") or [],
        "salience": candidate.get("salience"),
        "confidence": 0.82 if candidate.get("matched_pattern_count", 0) >= 2 else 0.74,
        "use_scope": candidate.get("use_scope"),
        "surface_policy": candidate.get("surface_policy"),
        "source_point_id": candidate.get("source_point_id"),
        "source_thread_id": candidate.get("thread_id"),
        "source_vantage_id": candidate.get("vantage_id"),
        "source_created_at": created_at,
        "needs_review": True,
        "write_intent": "none_preview_only",
    }


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
            candidate["card_candidate"] = build_card_candidate(candidate)
            correction_candidate = build_correction_candidate(candidate)
            if correction_candidate:
                candidate["correction_candidate"] = correction_candidate
            candidates.append(candidate)

    def sort_key(c: Dict[str, Any]):
        salience_rank = {"high": 0, "medium_high": 1, "medium": 2}.get(c.get("salience"), 9)
        return (salience_rank, str(c.get("created_at") or ""))

    return sorted(candidates, key=sort_key)


def merge_key_for_card(card: Dict[str, Any]) -> tuple:
    card_type = str(card.get("card_type") or "")
    event_type = str(card.get("event_type") or "")
    subject = card.get("subject") or {}
    subject_name = (
        subject.get("known_name")
        or subject.get("subject")
        or "unknown"
    )
    event = str(card.get("event") or "")

    if card_type == "relationship_anchor":
        return (card_type, subject_name)

    if event_type in ("death_loss", "pet_death_loss"):
        return (card_type, event_type, subject_name, event)

    if event_type == "caretaking_burden":
        return (card_type, event_type, subject_name, event)

    return (card_type, event_type, subject_name, event)


def merge_card_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[tuple, Dict[str, Any]] = {}

    for candidate in candidates:
        card = candidate.get("card_candidate") or {}
        if not card:
            continue

        key = merge_key_for_card(card)

        if key not in merged:
            merged[key] = {
                "card_schema": "merged_personal_memory_card_candidate_v0",
                "merge_key": list(key),
                "card_type": card.get("card_type"),
                "event_type": card.get("event_type"),
                "subject": card.get("subject"),
                "event": card.get("event"),
                "claim": card.get("claim"),
                "domain": list(card.get("domain") or []),
                "salience": card.get("salience"),
                "confidence": float(card.get("confidence") or 0.0),
                "use_scope": card.get("use_scope"),
                "surface_policy": card.get("surface_policy"),
                "source_point_ids": [],
                "source_thread_ids": [],
                "source_created_ats": [],
                "related_subjects": [],
                "needs_review": True,
                "write_intent": "none_preview_only",
            }

        item = merged[key]

        for field, value in [
            ("source_point_ids", card.get("source_point_id")),
            ("source_thread_ids", card.get("source_thread_id")),
            ("source_created_ats", card.get("source_created_at")),
        ]:
            if value and value not in item[field]:
                item[field].append(value)

        for subj in card.get("related_subjects") or []:
            if subj not in item["related_subjects"]:
                item["related_subjects"].append(subj)

        try:
            item["confidence"] = max(float(item.get("confidence") or 0.0), float(card.get("confidence") or 0.0))
        except Exception:
            pass

        for d in card.get("domain") or []:
            if d not in item["domain"]:
                item["domain"].append(d)

    return sorted(
        merged.values(),
        key=lambda x: (
            {"high": 0, "medium_high": 1, "medium": 2}.get(str(x.get("salience")), 9),
            str(x.get("card_type") or ""),
            str((x.get("subject") or {}).get("known_name") or (x.get("subject") or {}).get("subject") or ""),
        ),
    )


def unique_correction_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in candidates:
        correction = c.get("correction_candidate")
        if not correction:
            continue
        key = (
            correction.get("correction_type"),
            correction.get("incorrect_value"),
            correction.get("canonical_value"),
            correction.get("source_point_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(correction)
    return out


def review_decision_for_card(
    card: Dict[str, Any],
    correction_candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    subject = card.get("subject") or {}
    subject_value = str(subject.get("known_name") or subject.get("subject") or "").strip()
    use_scope = str(card.get("use_scope") or "").strip()
    claim = " ".join(str(card.get("claim") or "").split()).strip()
    salience = str(card.get("salience") or "").strip()

    try:
        confidence = float(card.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0

    recognized_use_scopes = {
        "DIRECT_RECALL",
        "DIRECT_RECALL_OR_RELEVANT_SUPPORT",
        "STYLE_AND_RELEVANT_SUPPORT",
        "MEMORY_NORMALIZATION",
    }

    blockers: List[str] = []
    review_flags: List[str] = []

    if not claim:
        blockers.append("missing_claim")
    if not subject_value or subject_value == "unknown":
        blockers.append("unknown_subject")
    if use_scope not in recognized_use_scopes:
        blockers.append("unsupported_use_scope")

    if subject_value == "multiple_pets":
        review_flags.append("multi_subject_card_needs_split_or_merge_review")
    if confidence < 0.80:
        review_flags.append("low_confidence_needs_review")
    if salience not in ("high", "medium_high"):
        review_flags.append("lower_salience_needs_review")

    for correction in correction_candidates:
        incorrect = str(correction.get("incorrect_value") or "")
        canonical = str(correction.get("canonical_value") or "")
        related = correction.get("applies_to_domains") or []
        domains = card.get("domain") or []
        if incorrect and (
            incorrect == subject_value
            or incorrect in claim
            or any(d in related for d in domains)
        ):
            review_flags.append(f"correction_candidate_present:{incorrect}->{canonical}")

    eligible = not blockers and confidence >= 0.80 and salience in ("high", "medium_high")
    if blockers:
        suggested_action = "auto_block"
    elif review_flags:
        suggested_action = "needs_review"
    elif eligible:
        suggested_action = "candidate_for_review"
    else:
        suggested_action = "needs_review"

    return {
        "decision_schema": "personal_memory_review_decision_v0",
        "merge_key": card.get("merge_key"),
        "suggested_action": suggested_action,
        "eligible": bool(eligible and not blockers),
        "needs_review": True,
        "blockers": blockers,
        "review_flags": review_flags,
        "confidence": confidence,
        "salience": salience,
        "use_scope": use_scope,
        "claim": claim,
        "write_intent": "none_preview_only",
    }


def build_review_decisions(
    merged_cards: List[Dict[str, Any]],
    correction_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        review_decision_for_card(card, correction_candidates)
        for card in merged_cards
    ]


def slugify_topic_part(value: str) -> str:
    v = " ".join(str(value or "").strip().lower().split())
    replacements = {
        " ": "_",
        "/": "_",
        "\\": "_",
        ":": "_",
        ";": "_",
        ",": "_",
        ".": "",
        "'": "",
        '"': "",
        "’": "",
        "“": "",
        "”": "",
    }
    for old, new in replacements.items():
        v = v.replace(old, new)
    v = re.sub(r"[^a-z0-9_\-\u0400-\u04FF]+", "", v)
    v = re.sub(r"_+", "_", v).strip("_")
    return v or "unknown"


def kind_for_card(card: Dict[str, Any]) -> str:
    card_type = str(card.get("card_type") or "")
    if card_type == "personal_life_event":
        return "personal_event"
    if card_type == "life_context":
        return "life_context"
    if card_type == "relationship_anchor":
        return "relationship_anchor"
    return "personal_memory"


def topic_key_for_card(card: Dict[str, Any]) -> str:
    user_id = USER_ID
    kind = kind_for_card(card)
    event_type = slugify_topic_part(str(card.get("event_type") or "general"))
    subject = card.get("subject") or {}
    subject_value = slugify_topic_part(str(subject.get("known_name") or subject.get("subject") or "unknown"))
    event = slugify_topic_part(str(card.get("event") or ""))
    if event and event != event_type:
        return f"user/{user_id}/{kind}/{event_type}/{subject_value}/{event}"
    return f"user/{user_id}/{kind}/{event_type}/{subject_value}"


def topic_key_for_correction(correction: Dict[str, Any]) -> str:
    user_id = USER_ID
    ctype = slugify_topic_part(str(correction.get("correction_type") or "correction"))
    incorrect = slugify_topic_part(str(correction.get("incorrect_value") or "unknown"))
    canonical = slugify_topic_part(str(correction.get("canonical_value") or "unknown"))
    return f"user/{user_id}/correction/{ctype}/{incorrect}_to_{canonical}"


def build_card_head_preview(card: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    kind = kind_for_card(card)
    topic_key = topic_key_for_card(card)
    confidence = float(card.get("confidence") or 0.0)
    salience = str(card.get("salience") or "")
    strength = 0.80 if salience == "high" else 0.65 if salience == "medium_high" else 0.50

    payload = {
        "mode": "personal_event_inventory_v0",
        "user_id": USER_ID,
        "card_schema": card.get("card_schema"),
        "card_type": card.get("card_type"),
        "event_type": card.get("event_type"),
        "subject": card.get("subject"),
        "related_subjects": card.get("related_subjects") or [],
        "event": card.get("event"),
        "domains": card.get("domain") or [],
        "salience": salience,
        "use_scope": card.get("use_scope"),
        "surface_policy": card.get("surface_policy"),
        "source_point_ids": card.get("source_point_ids") or [],
        "source_thread_ids": card.get("source_thread_ids") or [],
        "source_created_ats": card.get("source_created_ats") or [],
        "review_decision": decision,
        "needs_review": True,
        "write_intent": "none_preview_only",
    }

    return {
        "table": "vantage_card.card_head",
        "vantage_id": "user_global",
        "kind": kind,
        "topic_key": topic_key,
        "status": "active_after_review",
        "strength": strength,
        "confidence": confidence,
        "summary": card.get("claim") or "",
        "payload": payload,
    }


def build_card_revision_preview(card_head: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "table": "vantage_card.card_revision",
        "card_id": "<new_or_existing_card_id>",
        "summary": card_head.get("summary") or "",
        "payload": card_head.get("payload") or {},
        "reason": "personal_event_inventory_review_promotion",
        "delta": {
            "mode": "preview_only",
            "source": "scripts/personal_event_inventory.py",
        },
    }


def build_card_link_previews(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    for source_point_id in card.get("source_point_ids") or []:
        links.append({
            "table": "vantage_card.card_link",
            "card_id": "<new_or_existing_card_id>",
            "link_type": "qdrant_point",
            "ref_id": str(source_point_id),
            "note": "memory_raw source point",
        })
    for thread_id in card.get("source_thread_ids") or []:
        links.append({
            "table": "vantage_card.card_link",
            "card_id": "<new_or_existing_card_id>",
            "link_type": "thread",
            "ref_id": str(thread_id),
            "note": "source thread",
        })
    return links


def build_correction_card_head_preview(correction: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "mode": "personal_event_inventory_v0",
        "user_id": USER_ID,
        "card_schema": correction.get("card_schema"),
        "correction_type": correction.get("correction_type"),
        "canonical_value": correction.get("canonical_value"),
        "incorrect_value": correction.get("incorrect_value"),
        "entity_type": correction.get("entity_type"),
        "applies_to_domains": correction.get("applies_to_domains") or [],
        "use_scope": correction.get("use_scope"),
        "surface_policy": correction.get("surface_policy"),
        "source_point_id": correction.get("source_point_id"),
        "source_thread_id": correction.get("source_thread_id"),
        "source_created_at": correction.get("source_created_at"),
        "needs_review": True,
        "write_intent": "none_preview_only",
    }
    return {
        "table": "vantage_card.card_head",
        "vantage_id": "user_global",
        "kind": "correction",
        "topic_key": topic_key_for_correction(correction),
        "status": "active_after_review",
        "strength": 0.70,
        "confidence": float(correction.get("confidence") or 0.0),
        "summary": correction.get("claim") or "",
        "payload": payload,
    }


def build_promotion_mapping_preview(
    merged_cards: List[Dict[str, Any]],
    review_decisions: List[Dict[str, Any]],
    correction_candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    decision_by_key = {
        tuple(d.get("merge_key") or []): d
        for d in review_decisions
    }

    card_rows: List[Dict[str, Any]] = []
    revision_rows: List[Dict[str, Any]] = []
    link_rows: List[Dict[str, Any]] = []

    for card in merged_cards:
        key = tuple(card.get("merge_key") or [])
        decision = decision_by_key.get(key) or {}
        if decision.get("suggested_action") not in ("candidate_for_review", "needs_review"):
            continue

        head = build_card_head_preview(card, decision)
        card_rows.append(head)
        revision_rows.append(build_card_revision_preview(head))
        link_rows.extend(build_card_link_previews(card))

    correction_rows = [
        build_correction_card_head_preview(c)
        for c in correction_candidates
    ]

    return {
        "schema": "personal_memory_promotion_mapping_preview_v0",
        "mode": "preview_only_no_writes",
        "target_store": "vantage_card",
        "card_head_count": len(card_rows),
        "correction_card_head_count": len(correction_rows),
        "card_revision_count": len(revision_rows),
        "card_link_count": len(link_rows),
        "card_head_rows": card_rows,
        "correction_card_head_rows": correction_rows,
        "card_revision_rows": revision_rows,
        "card_link_rows": link_rows,
    }


def policy_retrieval_preview_for_question(
    *,
    question: str,
    promotion_preview: Dict[str, Any],
) -> Dict[str, Any]:
    q = " ".join(str(question or "").lower().split())
    rows = list(promotion_preview.get("card_head_rows") or []) + list(promotion_preview.get("correction_card_head_rows") or [])

    selected: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    wants_family_death = (
        ("death" in q or "died" in q or "passed away" in q)
        and ("family" in q or "mother" in q or "mom" in q or "dad" in q or "father" in q)
    )
    wants_pet_loss = (
        ("death" in q or "died" in q or "lost" in q or "passed away" in q)
        and ("pet" in q or "dog" in q or "cat" in q or "dahlia" in q or "helsing" in q or "neko" in q)
    )
    wants_relationship_names = (
        ("name" in q or "who" in q)
        and ("dog" in q or "cat" in q or "pet" in q or "mom" in q or "dad" in q or "mother" in q or "father" in q)
    )

    for row in rows:
        payload = row.get("payload") or {}
        kind = str(row.get("kind") or "")
        summary = str(row.get("summary") or "")
        topic_key = str(row.get("topic_key") or "")
        use_scope = str(payload.get("use_scope") or "")
        event_type = str(payload.get("event_type") or "")
        domains = payload.get("domains") or payload.get("applies_to_domains") or []

        reasons: List[str] = []
        allowed = False

        if use_scope in ("DIRECT_RECALL_OR_RELEVANT_SUPPORT", "DIRECT_RECALL", "MEMORY_NORMALIZATION"):
            reasons.append("use_scope_allows_direct_recall")
        else:
            reasons.append("use_scope_not_direct_recall")

        if wants_family_death and event_type == "death_loss" and "family" in domains:
            allowed = True
            reasons.append("matches_family_death_question")
        elif wants_pet_loss and event_type == "pet_death_loss":
            allowed = True
            reasons.append("matches_pet_loss_question")
        elif wants_relationship_names and kind == "relationship_anchor":
            allowed = True
            reasons.append("matches_relationship_name_question")
        elif kind == "correction" and ("nemo" in q or "neko" in q):
            allowed = True
            reasons.append("matches_correction_alias_question")
        else:
            reasons.append("no_policy_match_for_question")

        item = {
            "topic_key": topic_key,
            "kind": kind,
            "event_type": event_type,
            "summary": summary,
            "use_scope": use_scope,
            "confidence": row.get("confidence"),
            "reasons": reasons,
        }

        if allowed:
            selected.append(item)
        else:
            rejected.append(item)

    return {
        "schema": "personal_memory_policy_retrieval_preview_v0",
        "mode": "preview_only_no_live_db_reads",
        "question": question,
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "selected": selected,
        "rejected_sample": rejected[:10],
    }


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

    print("\n=== canonical card candidate summary ===")
    for i, c in enumerate(candidates[:20], 1):
        card = c.get("card_candidate") or {}
        print(f"{i}. {card.get('card_type')} | {card.get('event_type')} | {card.get('claim')} | confidence={card.get('confidence')} | use_scope={card.get('use_scope')}")

    raw_correction_candidates = [c.get("correction_candidate") for c in candidates if c.get("correction_candidate")]
    correction_candidates = []
    correction_keys = set()
    for correction in raw_correction_candidates:
        key = (
            correction.get("correction_type"),
            correction.get("incorrect_value"),
            correction.get("canonical_value"),
            correction.get("source_point_id"),
        )
        if key in correction_keys:
            continue
        correction_keys.add(key)
        correction_candidates.append(correction)

    print("\n=== correction candidate summary ===")
    print("raw_correction_candidate_count:", len(raw_correction_candidates))
    print("unique_correction_candidate_count:", len(correction_candidates))
    for i, correction in enumerate(correction_candidates[:20], 1):
        print(f"{i}. {correction.get('correction_type')} | {correction.get('incorrect_value')} -> {correction.get('canonical_value')} | confidence={correction.get('confidence')} | source={correction.get('source_point_id')}")

    merged_cards = merge_card_candidates(candidates)
    print("\n=== merged card candidate summary ===")
    print("raw_card_candidate_count:", len([c for c in candidates if c.get("card_candidate")]))
    print("merged_card_candidate_count:", len(merged_cards))
    for i, card in enumerate(merged_cards[:30], 1):
        subject = card.get("subject") or {}
        subject_name = subject.get("known_name") or subject.get("subject") or "unknown"
        print(f"{i}. {card.get('card_type')} | {card.get('event_type')} | {subject_name} | {card.get('claim')} | sources={len(card.get('source_point_ids') or [])} | confidence={card.get('confidence')}")

    review_decisions = build_review_decisions(merged_cards, correction_candidates)
    review_counts: Dict[str, int] = {}
    for d in review_decisions:
        action = str(d.get("suggested_action") or "unknown")
        review_counts[action] = review_counts.get(action, 0) + 1

    print("\n=== review decision preview ===")
    print("review_decision_count:", len(review_decisions))
    print("review_action_counts:", json.dumps(review_counts, ensure_ascii=False, sort_keys=True))
    for i, decision in enumerate(review_decisions[:30], 1):
        print(f"{i}. {decision.get('suggested_action')} | eligible={decision.get('eligible')} | confidence={decision.get('confidence')} | flags={decision.get('review_flags')} | blockers={decision.get('blockers')} | {decision.get('claim')}")

    promotion_preview = build_promotion_mapping_preview(
        merged_cards=merged_cards,
        review_decisions=review_decisions,
        correction_candidates=correction_candidates,
    )
    print("\n=== promotion mapping preview ===")
    print("schema:", promotion_preview.get("schema"))
    print("mode:", promotion_preview.get("mode"))
    print("target_store:", promotion_preview.get("target_store"))
    print("card_head_count:", promotion_preview.get("card_head_count"))
    print("correction_card_head_count:", promotion_preview.get("correction_card_head_count"))
    print("card_revision_count:", promotion_preview.get("card_revision_count"))
    print("card_link_count:", promotion_preview.get("card_link_count"))
    for i, row in enumerate((promotion_preview.get("card_head_rows") or [])[:20], 1):
        print(f"{i}. {row.get('table')} | {row.get('vantage_id')} | {row.get('kind')} | {row.get('topic_key')} | confidence={row.get('confidence')} | {row.get('summary')}")
    for i, row in enumerate((promotion_preview.get("correction_card_head_rows") or [])[:10], 1):
        print(f"correction {i}. {row.get('table')} | {row.get('topic_key')} | confidence={row.get('confidence')} | {row.get('summary')}")

    policy_preview = policy_retrieval_preview_for_question(
        question="Have I had any deaths in my family recently?",
        promotion_preview=promotion_preview,
    )
    print("\n=== policy retrieval preview ===")
    print("schema:", policy_preview.get("schema"))
    print("mode:", policy_preview.get("mode"))
    print("question:", policy_preview.get("question"))
    print("selected_count:", policy_preview.get("selected_count"))
    print("rejected_count:", policy_preview.get("rejected_count"))
    for i, item in enumerate(policy_preview.get("selected") or [], 1):
        print(f"{i}. SELECT | {item.get('kind')} | {item.get('event_type')} | {item.get('topic_key')} | confidence={item.get('confidence')} | reasons={item.get('reasons')} | {item.get('summary')}")

    for i, c in enumerate(candidates[:40], 1):
        print(f"\n--- candidate {i} ---")
        print(json.dumps(c, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
