#!/usr/bin/env python3
"""
Promote a tiny reviewed subset of personal memory card previews into vantage_card.

Default mode is DRY RUN. Use --apply to write.

This intentionally promotes only an explicit allowlist:
- DeeDee death card
- Monika caretaking-context card
- Nemo -> Neko correction card
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List

import asyncpg

from personal_event_inventory import (
    build_promotion_mapping_preview,
    build_review_decisions,
    find_candidates,
    merge_card_candidates,
    scroll_points,
)

APPROVED_TOPIC_KEYS = {
    "user/1240822d-ac9a-4096-95aa-e2b24d36ef50/personal_event/death_loss/deedee/died",
    "user/1240822d-ac9a-4096-95aa-e2b24d36ef50/life_context/caretaking_burden/monika/ongoing_caretaking_after_wife_psychotic_break",
    "user/1240822d-ac9a-4096-95aa-e2b24d36ef50/correction/name_alias_correction/nemo_to_neko",
}


def pg_dsn() -> str:
    dsn = os.getenv("POSTGRES_DSN") or "postgresql://sage:strongpassword@127.0.0.1:5432/memory"
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    return dsn


def build_rows() -> List[Dict[str, Any]]:
    points = scroll_points()
    candidates = find_candidates(points)
    raw_corrections = [c.get("correction_candidate") for c in candidates if c.get("correction_candidate")]
    corrections = []
    correction_keys = set()
    for correction in raw_corrections:
        key = (
            correction.get("correction_type"),
            correction.get("incorrect_value"),
            correction.get("canonical_value"),
            correction.get("source_point_id"),
        )
        if key in correction_keys:
            continue
        correction_keys.add(key)
        corrections.append(correction)

    merged_cards = merge_card_candidates(candidates)
    review_decisions = build_review_decisions(merged_cards, corrections)
    promotion = build_promotion_mapping_preview(
        merged_cards=merged_cards,
        review_decisions=review_decisions,
        correction_candidates=corrections,
    )

    rows = list(promotion.get("card_head_rows") or []) + list(promotion.get("correction_card_head_rows") or [])
    approved = [r for r in rows if r.get("topic_key") in APPROVED_TOPIC_KEYS]

    # Mark these as explicitly reviewed/approved for durable promotion.
    for r in approved:
        payload = dict(r.get("payload") or {})
        payload["mode"] = "approved_personal_memory_promotion_v1"
        payload["review_status"] = "approved"
        payload["approved_by"] = "manual_allowlist"
        payload["write_intent"] = "durable_card_write"
        payload["needs_review"] = False
        r["payload"] = payload
        r["status"] = "active"

    return approved


async def existing_topic_keys(conn: asyncpg.Connection, topic_keys: List[str]) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT topic_key
        FROM vantage_card.card_head
        WHERE vantage_id='user_global'
          AND topic_key = ANY($1::text[])
        """,
        topic_keys,
    )
    return {str(r["topic_key"]) for r in rows}


async def insert_card(conn: asyncpg.Connection, row: Dict[str, Any]) -> int:
    payload_json = json.dumps(row.get("payload") or {}, ensure_ascii=False)
    card_id = await conn.fetchval(
        """
        INSERT INTO vantage_card.card_head(
          vantage_id, kind, topic_key, status, strength, confidence, summary, payload
        )
        VALUES($1, $2, $3, $4::vantage_card.card_status, $5, $6, $7, $8::jsonb)
        RETURNING card_id
        """,
        row["vantage_id"],
        row["kind"],
        row["topic_key"],
        row["status"],
        row["strength"],
        row["confidence"],
        row["summary"],
        payload_json,
    )

    await conn.execute(
        """
        INSERT INTO vantage_card.card_revision(card_id, summary, payload, reason, delta)
        VALUES($1, $2, $3::jsonb, $4, $5::jsonb)
        """,
        card_id,
        row["summary"],
        payload_json,
        "approved_personal_memory_promotion_v1",
        json.dumps({"mode": "initial_approved_write"}, ensure_ascii=False),
    )

    payload = row.get("payload") or {}
    for source_point_id in payload.get("source_point_ids") or []:
        await conn.execute(
            """
            INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
            VALUES($1, 'qdrant_point', $2, 'memory_raw source point')
            """,
            card_id,
            str(source_point_id),
        )

    for thread_id in payload.get("source_thread_ids") or []:
        await conn.execute(
            """
            INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
            VALUES($1, 'thread', $2, 'source thread')
            """,
            card_id,
            str(thread_id),
        )

    if payload.get("source_point_id"):
        await conn.execute(
            """
            INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
            VALUES($1, 'qdrant_point', $2, 'memory_raw source point')
            """,
            card_id,
            str(payload["source_point_id"]),
        )

    if payload.get("source_thread_id"):
        await conn.execute(
            """
            INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
            VALUES($1, 'thread', $2, 'source thread')
            """,
            card_id,
            str(payload["source_thread_id"]),
        )

    return int(card_id)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually write approved cards")
    args = ap.parse_args()

    rows = build_rows()
    topic_keys = [r["topic_key"] for r in rows]

    print("=== approved personal memory promotion ===")
    print("mode:", "APPLY" if args.apply else "DRY_RUN")
    print("approved_count:", len(rows))
    for i, r in enumerate(rows, 1):
        print(f"{i}. {r['kind']} | {r['topic_key']} | confidence={r['confidence']} | {r['summary']}")

    conn = await asyncpg.connect(pg_dsn())
    try:
        existing = await existing_topic_keys(conn, topic_keys)
        print("existing_count:", len(existing))
        if existing:
            for k in sorted(existing):
                print("EXISTS:", k)

        to_insert = [r for r in rows if r["topic_key"] not in existing]
        print("to_insert_count:", len(to_insert))

        if not args.apply:
            print("dry_run_no_writes")
            return 0

        inserted: List[int] = []
        async with conn.transaction():
            for r in to_insert:
                inserted.append(await insert_card(conn, r))

        print("inserted_count:", len(inserted))
        print("inserted_card_ids:", inserted)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
