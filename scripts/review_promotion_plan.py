#!/usr/bin/env python3
"""
Dry-run review promotion planner for durable memory cards.

V0 scope:
- Uses personal_event_inventory preview functions.
- Does not write to the database.
- Classifies candidate card_head rows into:
  skip_duplicate
  create_new_card
  needs_manual_review

No --apply mode yet.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List


def _load_personal_event_inventory_module():
    module_path = Path(__file__).resolve().parent / "personal_event_inventory.py"
    spec = importlib.util.spec_from_file_location("personal_event_inventory", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pei = _load_personal_event_inventory_module()


def classify_action(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    comparison = row.get("existing_card_comparison") or payload.get("existing_card_comparison") or {}
    status = str(comparison.get("status") or "")
    kind = str(row.get("kind") or "")
    confidence = float(row.get("confidence") or 0.0)
    summary = str(row.get("summary") or "")
    review_decision = payload.get("review_decision") or {}
    review_flags = list(review_decision.get("review_flags") or [])
    blockers = list(review_decision.get("blockers") or [])

    reasons: List[str] = []

    if blockers:
        reasons.append("has_blockers")
        return {
            "action": "needs_manual_review",
            "reasons": reasons + blockers,
        }

    if status == "already_covered_or_duplicate":
        reasons.append("existing_card_comparison_already_covered_or_duplicate")
        return {
            "action": "skip_duplicate",
            "reasons": reasons,
        }

    if status in ("similar_existing_card", "weak_existing_overlap"):
        reasons.append(f"existing_card_comparison_{status}")
        return {
            "action": "needs_manual_review",
            "reasons": reasons,
        }

    if status == "new_candidate":
        reasons.append("existing_card_comparison_new_candidate")
    else:
        reasons.append(f"existing_card_comparison_unknown:{status or 'blank'}")
        return {
            "action": "needs_manual_review",
            "reasons": reasons,
        }

    if confidence < 0.80:
        reasons.append("confidence_below_create_threshold")
        return {
            "action": "needs_manual_review",
            "reasons": reasons,
        }

    if review_flags:
        reasons.append("review_flags_present")
        return {
            "action": "needs_manual_review",
            "reasons": reasons + review_flags,
        }

    if kind not in ("personal_event", "life_context", "correction"):
        reasons.append(f"kind_not_auto_creatable_in_v0:{kind}")
        return {
            "action": "needs_manual_review",
            "reasons": reasons,
        }

    # V0 allows only clean, high-confidence new personal_event/life_context/correction rows
    # to be proposed for creation. It still does not write anything.
    reasons.append("clean_high_confidence_new_candidate")
    return {
        "action": "create_new_card",
        "reasons": reasons,
    }


def build_personal_event_promotion_preview() -> Dict[str, Any]:
    points = pei.scroll_points()
    candidates = pei.find_candidates(points)
    existing_cards = __import__("asyncio").run(pei.load_existing_personal_cards())

    raw_correction_candidates = [
        c.get("correction_candidate")
        for c in candidates
        if c.get("correction_candidate")
    ]

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

    merged_cards = pei.merge_card_candidates(candidates)
    review_decisions = pei.build_review_decisions(merged_cards, correction_candidates)
    promotion_preview = pei.build_promotion_mapping_preview(
        merged_cards=merged_cards,
        review_decisions=review_decisions,
        correction_candidates=correction_candidates,
    )

    rows = list(promotion_preview.get("card_head_rows") or [])
    rows.extend(list(promotion_preview.get("correction_card_head_rows") or []))

    for row in rows:
        comparison = pei.compare_to_existing_card(row, existing_cards)
        row["existing_card_comparison"] = comparison
        payload = row.get("payload") or {}
        payload["existing_card_comparison"] = comparison
        row["payload"] = payload

    plan_rows = []
    action_counts: Dict[str, int] = {}

    for row in rows:
        decision = classify_action(row)
        action = decision["action"]
        action_counts[action] = action_counts.get(action, 0) + 1
        comp = row.get("existing_card_comparison") or {}
        best = comp.get("best_match") or {}

        plan_row = {
            "table": row.get("table"),
            "kind": row.get("kind"),
            "topic_key": row.get("topic_key"),
            "summary": row.get("summary"),
            "strength": row.get("strength"),
            "confidence": row.get("confidence"),
            "payload": row.get("payload") or {},
            "comparison_status": comp.get("status"),
            "best_existing_card_id": best.get("card_id"),
            "action": action,
            "action_reasons": decision.get("reasons") or [],
            "write_intent": "none_dry_run_only",
        }
        if action == "create_new_card":
            plan_row["sql_dry_run"] = build_sql_dry_run_for_create(plan_row)
        plan_rows.append(plan_row)

    return {
        "schema": "review_promotion_plan_v0",
        "mode": "dry_run_no_writes",
        "source": "personal_event_inventory",
        "points_scanned": len(points),
        "raw_candidate_count": len(candidates),
        "merged_card_candidate_count": len(merged_cards),
        "existing_card_count": len(existing_cards),
        "plan_row_count": len(plan_rows),
        "action_counts": action_counts,
        "plan_rows": plan_rows,
    }




def sql_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def sql_numeric(value: Any) -> str:
    if value is None:
        return "null"
    try:
        return str(float(value))
    except Exception:
        return "null"


def build_sql_dry_run_for_create(row: Dict[str, Any]) -> List[str]:
    """
    Return SQL text that documents the insert shape for a create_new_card row.
    This is intentionally not executed by this script.
    """
    payload = row.get("payload") or {}
    topic_key = row.get("topic_key")
    kind = row.get("kind")
    summary = row.get("summary")
    confidence = row.get("confidence")
    strength = row.get("strength")

    source_point_ids = payload.get("source_point_ids") or []
    source_thread_ids = payload.get("source_thread_ids") or []

    lines: List[str] = []
    lines.append("-- DRY RUN ONLY: create_new_card")
    lines.append("-- topic_key: " + str(topic_key))
    lines.append("with inserted_card as (")
    lines.append("  insert into vantage_card.card_head (")
    lines.append("    vantage_id, kind, topic_key, status, strength, confidence, summary, payload")
    lines.append("  ) values (")
    lines.append(
        "    'user_global', "
        + sql_literal(kind)
        + ", "
        + sql_literal(topic_key)
        + ", 'active', "
        + sql_numeric(strength)
        + ", "
        + sql_numeric(confidence)
        + ", "
        + sql_literal(summary)
        + ", "
        + sql_literal(payload)
        + "::jsonb"
    )
    lines.append("  )")
    lines.append("  returning card_id")
    lines.append("), inserted_revision as (")
    lines.append("  insert into vantage_card.card_revision (card_id, summary, payload, reason, delta)")
    lines.append("  select")
    lines.append("    card_id,")
    lines.append("    " + sql_literal(summary) + ",")
    lines.append("    " + sql_literal(payload) + "::jsonb,")
    lines.append("    'review_promotion_plan_create_new_card',")
    lines.append("    " + sql_literal({"mode": "dry_run_shape", "source": "scripts/review_promotion_plan.py"}) + "::jsonb")
    lines.append("  from inserted_card")
    lines.append("  returning revision_id")
    lines.append("), inserted_links as (")
    lines.append("  insert into vantage_card.card_link (card_id, link_type, ref_id, note)")

    link_selects: List[str] = []
    for ref_id in source_point_ids:
        link_selects.append(
            "  select card_id, 'qdrant_point', "
            + sql_literal(str(ref_id))
            + ", 'memory_raw source point' from inserted_card"
        )
    for ref_id in source_thread_ids:
        link_selects.append(
            "  select card_id, 'thread', "
            + sql_literal(str(ref_id))
            + ", 'source thread' from inserted_card"
        )

    if link_selects:
        lines.append("\n  union all\n".join(link_selects))
    else:
        lines.append("  select card_id, 'none', 'none', 'no source links' from inserted_card where false")

    lines.append("  on conflict (card_id, link_type, ref_id) do nothing")
    lines.append("  returning card_id, link_type, ref_id")
    lines.append(")")
    lines.append("select")
    lines.append("  (select card_id from inserted_card) as card_id,")
    lines.append("  (select count(*) from inserted_links) as link_count;")

    return lines


def main() -> int:
    plan = build_personal_event_promotion_preview()

    print("=== review promotion plan ===")
    print("schema:", plan["schema"])
    print("mode:", plan["mode"])
    print("source:", plan["source"])
    print("points_scanned:", plan["points_scanned"])
    print("raw_candidate_count:", plan["raw_candidate_count"])
    print("merged_card_candidate_count:", plan["merged_card_candidate_count"])
    print("existing_card_count:", plan["existing_card_count"])
    print("plan_row_count:", plan["plan_row_count"])
    print("action_counts:", json.dumps(plan["action_counts"], sort_keys=True))

    print()
    print("=== plan rows ===")
    for i, row in enumerate(plan["plan_rows"], 1):
        print(
            f"{i}. action={row['action']} | kind={row['kind']} | "
            f"comparison={row['comparison_status']} | best_existing={row['best_existing_card_id']} | "
            f"confidence={row['confidence']} | topic_key={row['topic_key']}"
        )
        print(f"   summary={row['summary']}")
        print(f"   reasons={row['action_reasons']}")

    print()
    print("=== sql dry-run for create_new_card rows ===")
    create_rows = [r for r in plan["plan_rows"] if r.get("action") == "create_new_card"]
    if not create_rows:
        print("(none)")
    for i, row in enumerate(create_rows, 1):
        print(f"-- create row {i}: {row.get('topic_key')}")
        for line in row.get("sql_dry_run") or []:
            print(line)
        print()

    print()
    print("=== raw json ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
