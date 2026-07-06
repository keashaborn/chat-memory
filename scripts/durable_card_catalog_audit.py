#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

import asyncpg


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


def _jsonish(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        try:
            x = json.loads(v)
            return x if isinstance(x, dict) else {}
        except Exception:
            return {}
    try:
        return dict(v or {})
    except Exception:
        return {}


async def main() -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        totals = await conn.fetchrow(
            """
            select
              count(*) as total,
              count(*) filter (where status='active') as active,
              count(*) filter (where status<>'active') as inactive
            from vantage_card.card_head
            """
        )

        by_kind = await conn.fetch(
            """
            select
              kind,
              status::text as status,
              count(*) as n
            from vantage_card.card_head
            group by kind, status::text
            order by kind, status::text
            """
        )

        by_policy = await conn.fetch(
            """
            select
              kind,
              coalesce(payload->>'review_status','') as review_status,
              coalesce(payload->>'use_scope','') as use_scope,
              coalesce(payload->>'surface_policy','') as surface_policy,
              coalesce(payload->>'sensitivity','') as sensitivity,
              count(*) as n
            from vantage_card.card_head
            group by 1,2,3,4,5
            order by kind, review_status, use_scope, surface_policy, sensitivity
            """
        )

        recent = await conn.fetch(
            """
            select
              card_id,
              vantage_id,
              kind,
              topic_key,
              status::text as status,
              summary,
              payload,
              confidence,
              updated_at
            from vantage_card.card_head
            order by updated_at desc nulls last, card_id desc
            limit 30
            """
        )

        approved_personal = await conn.fetch(
            """
            select
              card_id,
              vantage_id,
              kind,
              topic_key,
              status::text as status,
              summary,
              payload,
              confidence,
              updated_at
            from vantage_card.card_head
            where status='active'
              and vantage_id='user_global'
              and topic_key like 'user/%'
              and coalesce(payload->>'review_status','')='approved'
            order by card_id
            """
        )

        missing_policy = await conn.fetch(
            """
            select card_id, vantage_id, kind, topic_key, status::text as status,
                   summary, payload, confidence, updated_at
            from vantage_card.card_head
            where status='active'
              and (
                coalesce(payload->>'use_scope','')=''
                or coalesce(payload->>'surface_policy','')=''
                or coalesce(payload->>'sensitivity','')=''
              )
            order by kind, card_id
            """
        )

        active_without_review_status = await conn.fetch(
            """
            select card_id, vantage_id, kind, topic_key, status::text as status,
                   summary, payload, confidence, updated_at
            from vantage_card.card_head
            where status='active'
              and coalesce(payload->>'review_status','')=''
              and kind not in ('pref','background','identity','project','system')
            order by kind, card_id
            """
        )

        content_ok_cards = await conn.fetch(
            """
            select card_id, vantage_id, kind, topic_key, status::text as status,
                   summary, payload, confidence, updated_at
            from vantage_card.card_head
            where status='active'
              and coalesce(payload->>'use_scope','')='CONTENT_OK'
            order by kind, card_id
            """
        )

        possible_artifacts = await conn.fetch(
            """
            select card_id, vantage_id, kind, topic_key, status::text as status,
                   summary, payload, confidence, updated_at
            from vantage_card.card_head
            where status='active'
              and (
                topic_key ilike '%cursor%'
                or summary ilike '%cursor:%'
                or summary ilike '%true/false%'
                or summary ilike '%allowed_memory_scopes%'
                or summary ilike '%turn_intent:%'
              )
            order by kind, card_id
            """
        )

        print("=== durable card catalog audit ===")
        print(f"total_card_head: {int(totals['total'] or 0)}")
        print(f"active_card_head: {int(totals['active'] or 0)}")
        print(f"inactive_card_head: {int(totals['inactive'] or 0)}")
        print()

        print("=== counts by kind/status ===")
        if not by_kind:
            print("(none)")
        for r in by_kind:
            print(f"- kind={r['kind']} status={r['status']} n={int(r['n'])}")
        print()

        print("=== counts by kind/policy ===")
        if not by_policy:
            print("(none)")
        for r in by_policy:
            print(
                f"- n={int(r['n'])} kind={r['kind']} "
                f"review_status={r['review_status'] or '(blank)'} "
                f"use_scope={r['use_scope'] or '(blank)'} "
                f"surface_policy={r['surface_policy'] or '(blank)'} "
                f"sensitivity={r['sensitivity'] or '(blank)'}"
            )
        print()

        print("=== approved user_global personal cards ===")
        if not approved_personal:
            print("(none)")
        for r in approved_personal:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} "
                f"use_scope={p.get('use_scope','')} "
                f"surface_policy={p.get('surface_policy','')} "
                f"sensitivity={p.get('sensitivity','')} "
                f"topic_key={r['topic_key']}"
            )
            print(f"  summary={r['summary']}")
        print()

        print("=== risk check: active cards missing required policy fields ===")
        if not missing_policy:
            print("(none)")
        for r in missing_policy:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} status={r['status']} "
                f"use_scope={p.get('use_scope','')} surface_policy={p.get('surface_policy','')} "
                f"sensitivity={p.get('sensitivity','')} topic_key={r['topic_key']}"
            )
            print(f"  summary={r['summary']}")
        print()

        print("=== risk check: active non-legacy cards without review_status ===")
        if not active_without_review_status:
            print("(none)")
        for r in active_without_review_status:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} "
                f"use_scope={p.get('use_scope','')} surface_policy={p.get('surface_policy','')} "
                f"sensitivity={p.get('sensitivity','')} topic_key={r['topic_key']}"
            )
            print(f"  summary={r['summary']}")
        print()

        print("=== review queue: active CONTENT_OK cards ===")
        if not content_ok_cards:
            print("(none)")
        for r in content_ok_cards:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} sensitivity={p.get('sensitivity','')} "
                f"surface_policy={p.get('surface_policy','')} topic_key={r['topic_key']}"
            )
            print(f"  summary={r['summary']}")
        print()

        print("=== risk check: possible schema/control artifacts ===")
        if not possible_artifacts:
            print("(none)")
        for r in possible_artifacts:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} status={r['status']} "
                f"use_scope={p.get('use_scope','')} surface_policy={p.get('surface_policy','')} "
                f"topic_key={r['topic_key']}"
            )
            print(f"  summary={r['summary']}")
        print()

        print("=== recent card_head rows ===")
        if not recent:
            print("(none)")
        for r in recent:
            p = _jsonish(r["payload"])
            print(
                f"- card_id={r['card_id']} kind={r['kind']} status={r['status']} "
                f"vantage_id={r['vantage_id']} confidence={r['confidence']} updated_at={r['updated_at']}"
            )
            print(
                f"  policy: review_status={p.get('review_status','')} "
                f"use_scope={p.get('use_scope','')} "
                f"surface_policy={p.get('surface_policy','')} "
                f"sensitivity={p.get('sensitivity','')}"
            )
            print(f"  topic_key={r['topic_key']}")
            print(f"  summary={r['summary']}")

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
