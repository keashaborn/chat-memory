#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import asyncpg


def _load_dotenv_if_needed() -> None:
    """
    Minimal .env loader for standalone script usage.

    systemd services already receive POSTGRES_DSN, but an interactive shell may not.
    This keeps the audit script runnable from /opt/chat-memory without exporting env vars.
    """
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
        if os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL"):
            return


def _dsn() -> str:
    _load_dotenv_if_needed()
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

        by_policy = await conn.fetch(
            """
            select
              coalesce(metadata->>'use_scope', '') as use_scope,
              coalesce(metadata->>'surface_policy', '') as surface_policy,
              count(*) as n,
              max(created_at) as last_seen
            from vantage_card.card_signal
            where signal_type='injected_into_prompt'
            group by 1, 2
            order by n desc, last_seen desc
            """
        )

        by_card_id = await conn.fetch(
            """
            select
              coalesce(metadata->>'card_id', '') as card_id,
              kind,
              topic_key,
              count(*) as n,
              max(created_at) as last_seen
            from vantage_card.card_signal
            where signal_type='injected_into_prompt'
            group by 1, 2, 3
            order by card_id
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

        print("=== counts by policy ===")
        if not by_policy:
            print("(none)")
        for r in by_policy:
            print(
                f"- n={int(r['n'])} use_scope={r['use_scope'] or '(blank)'} "
                f"surface_policy={r['surface_policy'] or '(blank)'} "
                f"last_seen={r['last_seen']}"
            )
        print()

        print("=== counts by card_id ===")
        if not by_card_id:
            print("(none)")
        for r in by_card_id:
            print(
                f"- card_id={r['card_id'] or '(blank)'} n={int(r['n'])} "
                f"kind={r['kind']} last_seen={r['last_seen']} topic_key={r['topic_key']}"
            )
        print()

        print("=== recent signals ===")
        if not recent:
            print("(none)")
        for r in recent:
            raw_md = r["metadata"] or {}
            if isinstance(raw_md, str):
                try:
                    md = json.loads(raw_md)
                except Exception:
                    md = {"_raw_metadata": raw_md}
            elif isinstance(raw_md, dict):
                md = dict(raw_md)
            else:
                try:
                    md = dict(raw_md)
                except Exception:
                    md = {"_raw_metadata": str(raw_md)}

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
