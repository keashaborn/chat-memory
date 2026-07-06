#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import asyncpg


def _dsn() -> str:
    dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("ERROR: POSTGRES_DSN/DATABASE_URL missing")
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    return dsn


def _j(v: Any) -> str:
    return json.dumps(v, indent=2, ensure_ascii=False, sort_keys=True)


async def main() -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        total = await conn.fetchval(
            """
            select count(*)
            from vantage_card.card_signal
            """
        )

        injected_total = await conn.fetchval(
            """
            select count(*)
            from vantage_card.card_signal
            where signal_type='injected_into_prompt'
            """
        )

        by_topic = await conn.fetch(
            """
            select
              topic_key,
              kind,
              count(*) as n,
              max(created_at) as last_seen
            from vantage_card.card_signal
            where signal_type='injected_into_prompt'
            group by topic_key, kind
            order by n desc, last_seen desc
            limit 25
            """
        )

        by_turn_intent = await conn.fetch(
            """
            select
              coalesce(metadata->>'turn_intent', '') as turn_intent,
              count(*) as n
            from vantage_card.card_signal
            where signal_type='injected_into_prompt'
            group by 1
            order by n desc, turn_intent
            """
        )

        recent = await conn.fetch(
            """
            select
              signal_id,
              created_at,
              vantage_id,
              kind,
              topic_key,
              signal_type,
              magnitude,
              metadata
            from vantage_card.card_signal
            order by created_at desc
            limit 20
            """
        )

        print("=== durable card signal audit ===")
        print(f"total_signals: {int(total or 0)}")
        print(f"injected_into_prompt_signals: {int(injected_total or 0)}")
        print()

        print("=== counts by durable card topic ===")
        if not by_topic:
            print("(none)")
        for r in by_topic:
            print(
                f"- n={int(r['n'])} kind={r['kind']} "
                f"last_seen={r['last_seen']} topic_key={r['topic_key']}"
            )
        print()

        print("=== counts by turn_intent ===")
        if not by_turn_intent:
            print("(none)")
        for r in by_turn_intent:
            print(f"- {r['turn_intent'] or '(blank)'}: {int(r['n'])}")
        print()

        print("=== recent signals ===")
        if not recent:
            print("(none)")
        for r in recent:
            md: Dict[str, Any] = dict(r["metadata"] or {})
            compact = {
                "signal_id": r["signal_id"],
                "created_at": str(r["created_at"]),
                "vantage_id": r["vantage_id"],
                "kind": r["kind"],
                "topic_key": r["topic_key"],
                "signal_type": r["signal_type"],
                "magnitude": float(r["magnitude"]) if r["magnitude"] is not None else None,
                "answer_id": md.get("answer_id"),
                "thread_id": md.get("thread_id"),
                "turn_intent": md.get("turn_intent"),
                "card_id": md.get("card_id"),
                "use_scope": md.get("use_scope"),
                "surface_policy": md.get("surface_policy"),
                "selected_reasons": md.get("selected_reasons"),
            }
            print(_j(compact))

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
