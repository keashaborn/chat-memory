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

Default mode is dry-run. --apply is guarded and requires exact confirmation args.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import asyncpg


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




def _load_dotenv_if_needed() -> None:
    if os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL"):
        return

    for path in ("/opt/chat-memory/.env", ".env"):
        fp = Path(path)
        if not fp.exists():
            continue
        for line in fp.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in {"POSTGRES_DSN", "DATABASE_URL"} and v:
                os.environ.setdefault(k, v)


def pg_dsn() -> str:
    _load_dotenv_if_needed()
    dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL") or "postgresql://sage:strongpassword@127.0.0.1:5432/memory"
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    return dsn


async def topic_key_exists(conn: asyncpg.Connection, topic_key: str) -> bool:
    found = await conn.fetchval(
        """
        select 1
        from vantage_card.card_head
        where vantage_id='user_global'
          and topic_key=$1
        limit 1
        """,
        topic_key,
    )
    return bool(found)


async def apply_create_new_card(row: Dict[str, Any]) -> Dict[str, Any]:
    topic_key = str(row.get("topic_key") or "")
    if not topic_key:
        raise RuntimeError("missing topic_key")

    payload = dict(row.get("payload") or {})
    payload["mode"] = "review_promotion_plan_v1"
    payload["review_status"] = "approved"
    payload["approved_by"] = "guarded_cli_confirmation"
    payload["write_intent"] = "durable_card_write"
    payload["needs_review"] = False

    # Keep policy metadata explicit for audits.
    payload.setdefault("domains", payload.get("domain") or payload.get("domains") or ["personal_memory"])
    payload.setdefault("sensitivity", "medium")

    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    conn = await asyncpg.connect(pg_dsn())
    try:
        async with conn.transaction():
            if await topic_key_exists(conn, topic_key):
                raise RuntimeError(f"topic_key already exists: {topic_key}")

            card_id = await conn.fetchval(
                """
                insert into vantage_card.card_head(
                  vantage_id, kind, topic_key, status, strength, confidence, summary, payload
                )
                values($1, $2, $3, $4::vantage_card.card_status, $5, $6, $7, $8::jsonb)
                returning card_id
                """,
                "user_global",
                row["kind"],
                topic_key,
                "active",
                row.get("strength") if row.get("strength") is not None else 0.50,
                row.get("confidence") if row.get("confidence") is not None else 0.50,
                row.get("summary") or "",
                payload_json,
            )

            revision_id = await conn.fetchval(
                """
                insert into vantage_card.card_revision(card_id, summary, payload, reason, delta)
                values($1, $2, $3::jsonb, $4, $5::jsonb)
                returning revision_id
                """,
                card_id,
                row.get("summary") or "",
                payload_json,
                "review_promotion_plan_create_new_card",
                json.dumps(
                    {
                        "mode": "guarded_apply",
                        "source": "scripts/review_promotion_plan.py",
                        "action": "create_new_card",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            link_count = 0
            for source_point_id in payload.get("source_point_ids") or []:
                result = await conn.execute(
                    """
                    insert into vantage_card.card_link(card_id, link_type, ref_id, note)
                    values($1, 'qdrant_point', $2, 'memory_raw source point')
                    on conflict (card_id, link_type, ref_id) do nothing
                    """,
                    card_id,
                    str(source_point_id),
                )
                if result.endswith(" 1"):
                    link_count += 1

            for thread_id in payload.get("source_thread_ids") or []:
                result = await conn.execute(
                    """
                    insert into vantage_card.card_link(card_id, link_type, ref_id, note)
                    values($1, 'thread', $2, 'source thread')
                    on conflict (card_id, link_type, ref_id) do nothing
                    """,
                    card_id,
                    str(thread_id),
                )
                if result.endswith(" 1"):
                    link_count += 1

        return {
            "card_id": int(card_id),
            "revision_id": int(revision_id),
            "link_count": int(link_count),
            "topic_key": topic_key,
        }
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually apply one confirmed create_new_card action")
    ap.add_argument("--confirm-action", default="", help="Required with --apply; must equal create_new_card")
    ap.add_argument("--confirm-topic-key", default="", help="Required with --apply; exact topic_key to apply")
    return ap.parse_args()


def validate_apply_request(args: argparse.Namespace, plan: Dict[str, Any]) -> Dict[str, Any] | None:
    if not args.apply:
        return None

    if args.confirm_action != "create_new_card":
        raise SystemExit("REFUSING APPLY: --confirm-action must be exactly create_new_card")

    if not args.confirm_topic_key:
        raise SystemExit("REFUSING APPLY: --confirm-topic-key is required")

    matches = [
        row for row in plan.get("plan_rows") or []
        if row.get("topic_key") == args.confirm_topic_key
    ]

    if len(matches) != 1:
        raise SystemExit(f"REFUSING APPLY: expected exactly one matching topic_key, got {len(matches)}")

    row = matches[0]

    if row.get("action") != "create_new_card":
        raise SystemExit(f"REFUSING APPLY: matching row action is {row.get('action')}, not create_new_card")

    if row.get("comparison_status") != "new_candidate":
        raise SystemExit(f"REFUSING APPLY: comparison_status is {row.get('comparison_status')}, not new_candidate")

    return row


def main() -> int:
    args = parse_args()
    plan = build_personal_event_promotion_preview()
    apply_row = validate_apply_request(args, plan)

    print("=== review promotion plan ===")
    print("schema:", plan["schema"])
    print("mode:", "apply_requested_guarded" if args.apply else plan["mode"])
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
    print("=== apply status ===")
    if not args.apply:
        print("dry_run_no_writes")
    else:
        print("apply_requested")
        print("confirmed_action:", args.confirm_action)
        print("confirmed_topic_key:", args.confirm_topic_key)
        print("validated_row_action:", apply_row.get("action") if apply_row else None)
        print("validated_row_topic_key:", apply_row.get("topic_key") if apply_row else None)

        result = asyncio.run(apply_create_new_card(apply_row))
        print("apply_result:", json.dumps(result, ensure_ascii=False, sort_keys=True))
        print("apply_complete")

    print()
    print("=== raw json ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
