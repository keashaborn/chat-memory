#!/usr/bin/env python3
"""
Preview durable profile/background/project fact candidates from Qdrant memory_raw.

Read-only. No writes.
Goal: identify stable user facts that may deserve reviewed durable cards:
education, work history, company history, professional identity, project context.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import requests

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memory_raw"
USER_ID = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


PROFILE_RULES = [
    {
        "fact_type": "professional_identity",
        "kind": "background",
        "domain": ["professional", "identity_context"],
        "salience": "medium_high",
        "patterns": [
            r"\bI('?m| am) a clinical psychologist\b",
            r"\bI('?m| am) a psychologist\b",
            r"\bclinical psychologist\b",
            r"\bBCBA\b|\bBCBA-D\b",
            r"\bbehavior analyst\b",
        ],
        "use_scope": "CONTENT_OK",
        "surface_policy": "mention_when_relevant",
    },
    {
        "fact_type": "education_training",
        "kind": "background",
        "domain": ["education", "professional"],
        "salience": "medium",
        "patterns": [
            r"\bdoctorate\b",
            r"\bPh\.?D\b|\bPsy\.?D\b",
            r"\bpost[- ]doctoral\b",
            r"\bmaster'?s\b.{0,80}\bclinical psychopharmacology\b",
            r"\bclinical psychopharmacology\b",
            r"\bNASM\b|\bpersonal training certificate\b|\bphysique coaching\b",
        ],
        "use_scope": "CONTENT_OK",
        "surface_policy": "mention_when_relevant",
    },
    {
        "fact_type": "company_history",
        "kind": "background",
        "domain": ["business", "career", "company_history"],
        "salience": "medium_high",
        "patterns": [
            r"\bfounded\b.{0,120}\bCaravel\b",
            r"\bCaravel Autism Health\b",
            r"\bstarted\b.{0,120}\bCaravel\b",
            r"\bsold\b.{0,120}\bCaravel\b",
        ],
        "use_scope": "CONTENT_OK",
        "surface_policy": "mention_when_relevant",
    },
    {
        "fact_type": "work_history",
        "kind": "background",
        "domain": ["career", "work_history"],
        "salience": "medium",
        "patterns": [
            r"\bprison system\b",
            r"\bpsychiatric hospital\b",
            r"\bprivate practice\b",
            r"\bFederal Prison Camp Alderson\b",
            r"\bAlderson\b",
            r"\bWisconsin Early Autism Project\b|\bWEAP\b",
        ],
        "use_scope": "CONTENT_OK",
        "surface_policy": "mention_when_relevant",
    },
    {
        "fact_type": "current_project",
        "kind": "project",
        "domain": ["project", "product", "software"],
        "salience": "medium_high",
        "patterns": [
            r"\bVerbal Sage\b",
            r"\bVerbalSage\b",
            r"\bLifeSwitch\b",
            r"\bSeeBX\b",
            r"\bSeeBx\b",
        ],
        "use_scope": "CONTENT_OK",
        "surface_policy": "mention_when_relevant",
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
    return payload.get("user_id") == USER_ID or payload.get("user_id_alias") == USER_ID


def source_is_user(payload: Dict[str, Any]) -> bool:
    src = str(payload.get("source") or "")
    tags = payload.get("tags") or []
    return src.endswith(":user") or "user" in tags


def looks_like_system_or_code(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    cues = (
        "```",
        "systemctl",
        "docker exec",
        "psql -u",
        "git status",
        "verbal sage memory system checkpoint",
        "target memory system",
        "current memory stores",
        "use_scope",
        "surface_policy",
        "vantage_card.card_head",
        "memory architecture",
    )
    return any(cue in low for cue in cues)


def looks_like_question_only(text: str) -> bool:
    t = " ".join(str(text or "").strip().split())
    if not t:
        return False

    low = t.lower()
    question_starts = (
        "where did ",
        "what did ",
        "what was ",
        "who was ",
        "how did ",
        "do you remember",
        "can you remember",
        "what do you know",
    )

    if t.endswith("?") and low.startswith(question_starts):
        return True

    # Very short question-like probes should not become profile facts.
    if len(t) < 120 and low.startswith(question_starts):
        return True

    return False


def canonical_value_for_rule(fact_type: str, text: str) -> str:
    t = text

    if fact_type == "professional_identity":
        values = []
        if re.search(r"\bclinical psychologist\b", t, re.I):
            values.append("clinical psychologist")
        if re.search(r"\bBCBA(?:-D)?\b", t, re.I):
            values.append("BCBA/BCBA-D")
        if re.search(r"\bbehavior analyst\b", t, re.I):
            values.append("behavior analyst")
        return ", ".join(values) or "professional identity"

    if fact_type == "education_training":
        values = []
        if re.search(r"\bdoctorate\b|\bPh\.?D\b|\bPsy\.?D\b", t, re.I):
            values.append("doctorate")
        if re.search(r"\bclinical psychopharmacology\b", t, re.I):
            values.append("post-doctoral clinical psychopharmacology")
        if re.search(r"\bNASM\b|\bpersonal training certificate\b", t, re.I):
            values.append("NASM/personal training education")
        if re.search(r"\bphysique coaching\b|\bbodybuilding\b", t, re.I):
            values.append("physique coaching/bodybuilding education")
        return ", ".join(values) or "education/training"

    if fact_type == "company_history":
        if re.search(r"\bCaravel Autism Health\b", t, re.I):
            return "Caravel Autism Health"
        if re.search(r"\bCaravel\b", t, re.I):
            return "Caravel"
        return "company history"

    if fact_type == "work_history":
        values = []
        if re.search(r"\bprison system\b", t, re.I):
            values.append("prison system")
        if re.search(r"\bpsychiatric hospital\b", t, re.I):
            values.append("psychiatric hospital")
        if re.search(r"\bprivate practice\b", t, re.I):
            values.append("private practice")
        if re.search(r"\bFederal Prison Camp Alderson\b|\bAlderson\b", t, re.I):
            values.append("Federal Prison Camp Alderson")
        if re.search(r"\bWisconsin Early Autism Project\b|\bWEAP\b", t, re.I):
            values.append("Wisconsin Early Autism Project/WEAP")
        return ", ".join(values) or "work history"

    if fact_type == "current_project":
        values = []
        for name in ("Verbal Sage", "VerbalSage", "LifeSwitch", "SeeBX", "SeeBx"):
            if re.search(rf"\b{re.escape(name)}\b", t, re.I):
                norm = "Verbal Sage" if name == "VerbalSage" else "SeeBX" if name == "SeeBx" else name
                if norm not in values:
                    values.append(norm)
        return ", ".join(values) or "current project"

    return fact_type


def claim_for_candidate(fact_type: str, value: str) -> str:
    if fact_type == "professional_identity":
        return f"User has professional identity/background: {value}."
    if fact_type == "education_training":
        return f"User has education/training background: {value}."
    if fact_type == "company_history":
        return f"User has company/founder history involving {value}."
    if fact_type == "work_history":
        return f"User has work-history context involving {value}."
    if fact_type == "current_project":
        return f"User has current project/company context involving {value}."
    return f"User profile fact: {value}."


def find_candidates(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    for pt in points:
        payload = pt.get("payload") or {}
        if not user_matches(payload):
            continue
        if not source_is_user(payload):
            continue

        text = text_for_payload(payload)
        if not text or looks_like_system_or_code(text) or looks_like_question_only(text):
            continue

        for rule in PROFILE_RULES:
            matched_patterns = []
            for pat in rule["patterns"]:
                if re.search(pat, text, re.I | re.S):
                    matched_patterns.append(pat)

            if not matched_patterns:
                continue

            value = canonical_value_for_rule(rule["fact_type"], text)

            # Do not emit vague placeholder values as durable-card candidates.
            if value in {"professional identity", "education/training", "company history", "work history", "current project"}:
                continue

            confidence = 0.82 if len(matched_patterns) >= 2 else 0.74

            candidates.append({
                "candidate_schema": "profile_fact_candidate_v0",
                "candidate_id": f"profile_fact:{pt.get('id')}:{rule['fact_type']}",
                "source_point_id": pt.get("id"),
                "source": payload.get("source"),
                "thread_id": payload.get("thread_id"),
                "vantage_id": payload.get("vantage_id"),
                "created_at": payload.get("created_at"),
                "kind": rule["kind"],
                "fact_type": rule["fact_type"],
                "value": value,
                "claim": claim_for_candidate(rule["fact_type"], value),
                "domain": rule["domain"],
                "salience": rule["salience"],
                "confidence": confidence,
                "use_scope": rule["use_scope"],
                "surface_policy": rule["surface_policy"],
                "matched_pattern_count": len(matched_patterns),
                "matched_patterns": matched_patterns[:3],
                "text_preview": text[:900],
                "needs_review": True,
                "write_intent": "none_preview_only",
            })

    return sorted(
        candidates,
        key=lambda c: (
            str(c.get("fact_type") or ""),
            str(c.get("value") or ""),
            str(c.get("created_at") or ""),
        ),
    )


def merge_key(c: Dict[str, Any]) -> tuple:
    return (
        str(c.get("kind") or ""),
        str(c.get("fact_type") or ""),
        str(c.get("value") or "").lower(),
    )


def merge_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[tuple, Dict[str, Any]] = {}

    for c in candidates:
        key = merge_key(c)
        if key not in merged:
            merged[key] = {
                "candidate_schema": "merged_profile_fact_candidate_v0",
                "merge_key": list(key),
                "kind": c.get("kind"),
                "fact_type": c.get("fact_type"),
                "value": c.get("value"),
                "claim": c.get("claim"),
                "domain": list(c.get("domain") or []),
                "salience": c.get("salience"),
                "confidence": float(c.get("confidence") or 0.0),
                "use_scope": c.get("use_scope"),
                "surface_policy": c.get("surface_policy"),
                "source_point_ids": [],
                "source_thread_ids": [],
                "source_created_ats": [],
                "needs_review": True,
                "write_intent": "none_preview_only",
            }

        item = merged[key]
        item["confidence"] = max(float(item.get("confidence") or 0.0), float(c.get("confidence") or 0.0))
        for field, value in [
            ("source_point_ids", c.get("source_point_id")),
            ("source_thread_ids", c.get("thread_id")),
            ("source_created_ats", c.get("created_at")),
        ]:
            if value and value not in item[field]:
                item[field].append(value)
        for d in c.get("domain") or []:
            if d not in item["domain"]:
                item["domain"].append(d)

    return sorted(
        merged.values(),
        key=lambda c: (
            str(c.get("kind") or ""),
            str(c.get("fact_type") or ""),
            str(c.get("value") or ""),
        ),
    )


def review_decision(c: Dict[str, Any]) -> Dict[str, Any]:
    claim = " ".join(str(c.get("claim") or "").split()).strip()
    value = " ".join(str(c.get("value") or "").split()).strip()
    confidence = float(c.get("confidence") or 0.0)
    salience = str(c.get("salience") or "")

    blockers = []
    flags = []

    if not claim:
        blockers.append("missing_claim")
    if not value:
        blockers.append("missing_value")
    if confidence < 0.80:
        flags.append("low_confidence_needs_review")
    if salience not in ("medium_high", "high"):
        flags.append("lower_salience_needs_review")

    eligible = not blockers and confidence >= 0.80

    return {
        "decision_schema": "profile_fact_review_decision_v0",
        "merge_key": c.get("merge_key"),
        "suggested_action": "auto_block" if blockers else "candidate_for_review" if eligible and not flags else "needs_review",
        "eligible": bool(eligible and not blockers),
        "needs_review": True,
        "blockers": blockers,
        "review_flags": flags,
        "confidence": confidence,
        "salience": salience,
        "claim": claim,
        "write_intent": "none_preview_only",
    }


def slugify(value: str) -> str:
    v = " ".join(str(value or "").strip().lower().split())
    for old, new in {
        " ": "_", "/": "_", "\\": "_", ":": "_", ";": "_", ",": "_",
        ".": "", "'": "", '"': "", "’": "",
    }.items():
        v = v.replace(old, new)
    v = re.sub(r"[^a-z0-9_\-]+", "", v)
    v = re.sub(r"_+", "_", v).strip("_")
    return v or "unknown"


def card_head_preview(c: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(c.get("kind") or "background")
    fact_type = slugify(str(c.get("fact_type") or "profile_fact"))
    value = slugify(str(c.get("value") or "unknown"))

    payload = {
        "mode": "profile_fact_inventory_v0",
        "user_id": USER_ID,
        "candidate_schema": c.get("candidate_schema"),
        "fact_type": c.get("fact_type"),
        "value": c.get("value"),
        "domains": c.get("domain") or [],
        "salience": c.get("salience"),
        "use_scope": c.get("use_scope"),
        "surface_policy": c.get("surface_policy"),
        "source_point_ids": c.get("source_point_ids") or [],
        "source_thread_ids": c.get("source_thread_ids") or [],
        "source_created_ats": c.get("source_created_ats") or [],
        "review_decision": decision,
        "needs_review": True,
        "write_intent": "none_preview_only",
    }

    return {
        "table": "vantage_card.card_head",
        "vantage_id": "user_global",
        "kind": kind,
        "topic_key": f"user/{USER_ID}/{kind}/{fact_type}/{value}",
        "status": "active_after_review",
        "strength": 0.65 if c.get("salience") == "medium_high" else 0.50,
        "confidence": float(c.get("confidence") or 0.0),
        "summary": c.get("claim") or "",
        "payload": payload,
    }


def main() -> int:
    points = scroll_points()
    candidates = find_candidates(points)
    merged = merge_candidates(candidates)
    decisions = [review_decision(c) for c in merged]

    review_counts: Dict[str, int] = {}
    for d in decisions:
        action = str(d.get("suggested_action") or "unknown")
        review_counts[action] = review_counts.get(action, 0) + 1

    rows = [
        card_head_preview(c, d)
        for c, d in zip(merged, decisions)
        if d.get("suggested_action") in ("candidate_for_review", "needs_review")
    ]

    print("=== profile fact inventory preview ===")
    print("mode: read_only")
    print("collection:", COLLECTION)
    print("user_id:", USER_ID)
    print("points_scanned:", len(points))
    print("candidate_count:", len(candidates))
    print("merged_candidate_count:", len(merged))

    counts: Dict[str, int] = {}
    for c in candidates:
        ft = str(c.get("fact_type") or "unknown")
        counts[ft] = counts.get(ft, 0) + 1
    print("fact_type_counts:", json.dumps(counts, ensure_ascii=False, sort_keys=True))

    print()
    print("=== merged candidate summary ===")
    for i, c in enumerate(merged[:40], 1):
        print(f"{i}. {c.get('kind')} | {c.get('fact_type')} | {c.get('value')} | sources={len(c.get('source_point_ids') or [])} | confidence={c.get('confidence')} | {c.get('claim')}")

    print()
    print("=== review decision preview ===")
    print("review_decision_count:", len(decisions))
    print("review_action_counts:", json.dumps(review_counts, ensure_ascii=False, sort_keys=True))
    for i, d in enumerate(decisions[:40], 1):
        print(f"{i}. {d.get('suggested_action')} | eligible={d.get('eligible')} | confidence={d.get('confidence')} | flags={d.get('review_flags')} | blockers={d.get('blockers')} | {d.get('claim')}")

    print()
    print("=== card head preview ===")
    print("schema: profile_fact_promotion_mapping_preview_v0")
    print("mode: preview_only_no_writes")
    print("card_head_count:", len(rows))
    for i, row in enumerate(rows[:40], 1):
        print(f"{i}. {row.get('table')} | {row.get('vantage_id')} | {row.get('kind')} | {row.get('topic_key')} | confidence={row.get('confidence')} | {row.get('summary')}")

    print()
    print("=== raw candidates sample ===")
    for i, c in enumerate(candidates[:20], 1):
        print(f"--- candidate {i} ---")
        print(json.dumps(c, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
