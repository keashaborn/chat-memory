import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg


def _jsonb(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


async def _canonicalize_user_id(conn: asyncpg.Connection, vantage_id: str, user_id: str) -> str:
    """Resolve user_id aliases to a canonical id (best-effort)."""
    uid = str(user_id or "").strip()
    if not uid:
        return "unknown"
    try:
        row = await conn.fetchrow(
            """
            SELECT canonical_user_id
            FROM vantage_identity.user_alias
            WHERE vantage_id=$1 AND alias_user_id=$2
            """,
            vantage_id,
            uid,
        )
        if row and row["canonical_user_id"]:
            return str(row["canonical_user_id"])
    except Exception:
        pass
    return uid


async def _get_or_create_card(conn: asyncpg.Connection, vantage_id: str, kind: str, topic_key: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT card_id
        FROM vantage_card.card_head
        WHERE vantage_id=$1 AND kind=$2 AND topic_key=$3
        LIMIT 1
        """,
        vantage_id, kind, topic_key
    )
    if row:
        return int(row["card_id"])

    cid = await conn.fetchval(
        """
        INSERT INTO vantage_card.card_head(vantage_id, kind, topic_key, summary, payload)
        VALUES ($1,$2,$3,'', '{}'::jsonb)
        RETURNING card_id
        """,
        vantage_id, kind, topic_key
    )
    return int(cid)


async def _write_revision(
    conn: asyncpg.Connection,
    card_id: int,
    summary: str,
    payload: Dict[str, Any],
    reason: str,
    delta: Optional[Dict[str, Any]] = None,
) -> int:
    prev = await conn.fetchval(
        "SELECT revision_id FROM vantage_card.card_revision WHERE card_id=$1 ORDER BY revision_id DESC LIMIT 1",
        card_id
    )

    rid = await conn.fetchval(
        """
        INSERT INTO vantage_card.card_revision(card_id, prev_revision_id, summary, payload, reason, delta)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)
        RETURNING revision_id
        """,
        card_id, prev, summary, _jsonb(payload), reason, _jsonb(delta or {})
    )
    await conn.execute(
        """
        UPDATE vantage_card.card_head
           SET updated_at=now(),
               summary=$2,
               payload=$3::jsonb
         WHERE card_id=$1
        """,
        card_id, summary, _jsonb(payload)
    )
    return int(rid)


async def card_consolidate_from_kv_once(
    conn: asyncpg.Connection,
    vantage_id: str,
    limit_sources: int = 10,
) -> Dict[str, Any]:
    """
    v2: For newest DONE sources (chat_log-derived), update stable per-user topic cards keyed by predicate.
    This turns "one message => one card" into "many messages over time => one evolving card".

    Mapping:
      - predicate 'attr.audit' -> kind='audit'
      - all other attr.* -> kind='pref'
      - topic_key = f"user/{user_id}/{kind}/{attr_key}"
    """
    limit_sources = int(limit_sources)
    if limit_sources <= 0:
        return {"ok": True, "updated_cards": 0, "limit_sources": limit_sources}

    cursor_card_id = await _get_or_create_card(conn, vantage_id, "system", "consolidate_kv_v2_cursor")

    rows = await conn.fetch(
        """
        SELECT s.source_id,
               s.external_id,
               s.title,
               s.metadata,
               s.created_at
        FROM vantage_fact.source s
        LEFT JOIN vantage_card.card_link l
          ON l.card_id=$2 AND l.link_type='source' AND l.ref_id=s.source_id::text
        WHERE s.status='done'
          AND s.source_type='chat_log'
          AND l.card_id IS NULL
        ORDER BY s.source_id DESC
        LIMIT $1
        """,
        limit_sources,
        cursor_card_id,
    )

    updated = 0
    touched_cards = []

    async with conn.transaction():
        for r in rows:
            source_id = int(r["source_id"])
            md = r["metadata"] or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = {}

            chat_log_id = md.get("chat_log_id")
            alias_user_id = str(md.get("user_id") or "unknown")
            user_id = await _canonicalize_user_id(conn, vantage_id, alias_user_id)

            title = (r["title"] or f"source:{source_id}").strip()
            doc_row = await conn.fetchrow(
                """
                SELECT entity_id
                FROM vantage_fact.entity
                WHERE entity_type='document' AND canonical_name=$1
                ORDER BY entity_id DESC
                LIMIT 1
                """,
                title
            )
            if not doc_row:
                await conn.execute(
                    """
                    INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                    VALUES ($1,'source',$2,$3)
                    ON CONFLICT DO NOTHING
                    """,
                    cursor_card_id, str(source_id), 'skip:no_doc_entity'
                )
                continue
            doc_eid = int(doc_row["entity_id"])

            claims = await conn.fetch(
                """
                SELECT claim_id, predicate, object_literal
                FROM vantage_fact.claim
                WHERE subject_entity_id=$1
                  AND status='active'
                  AND predicate LIKE 'attr.%'
                ORDER BY predicate ASC, claim_id ASC
                """,
                doc_eid
            )
            if not claims:
                await conn.execute(
                    """
                    INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                    VALUES ($1,'source',$2,$3)
                    ON CONFLICT DO NOTHING
                    """,
                    cursor_card_id, str(source_id), 'skip:no_attr_claims'
                )
                continue

            # mark source processed on the cursor card; distinguish ignored-only sources
            ignored_keys = {"return_exactly","say_exactly","seedmemory","seed_note","threadctx","audit"}
            has_effective = False
            for _c in claims:
                _pred = str(_c["predicate"])
                _attr_key = _pred.replace("attr.", "", 1)
                if _attr_key not in ignored_keys:
                    has_effective = True
                    break
            note = "ok" if has_effective else "skip:ignored_attr_keys"
            await conn.execute(
                """
                INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                VALUES ($1,'source',$2,$3)
                ON CONFLICT DO NOTHING
                """,
                cursor_card_id, str(source_id), note
            )

            for c in claims:
                claim_id = int(c["claim_id"])
                pred = str(c["predicate"])
                attr_key = pred.replace("attr.", "", 1)

                # ignore harness/test attributes so they don't pollute preference cards
                if attr_key in {"return_exactly","say_exactly","seedmemory","seed_note","threadctx","audit"}:
                    continue

                obj = c["object_literal"]
                if isinstance(obj, str):
                    try:
                        obj = json.loads(obj)
                    except Exception:
                        obj = {"v": obj}
                val = (obj or {}).get("v")
                if val is None:
                    continue
                val = str(val).strip()

                kind = "audit" if attr_key == "audit" else "pref"
                topic_key = f"user/{user_id}/{kind}/{attr_key}"

                card_id = await _get_or_create_card(conn, vantage_id, kind, topic_key)
                head = await conn.fetchrow(
                    "SELECT payload, strength, confidence FROM vantage_card.card_head WHERE card_id=$1",
                    card_id
                )
                payload = head["payload"] if head else {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                payload = payload or {}
                cur_strength = float(head["strength"]) if head and head["strength"] is not None else 0.5
                cur_confidence = float(head["confidence"]) if head and head["confidence"] is not None else 0.5
                prev_value = payload.get("current_value")


                counts = payload.get("value_counts") or {}
                if not isinstance(counts, dict):
                    counts = {}

                counts[val] = int(counts.get(val, 0)) + 1

                payload.update({
                    "mode": "card_consolidate_kv_v2",
                    "source_id_last": source_id,
                    "chat_log_id_last": chat_log_id,
                    "user_id": user_id,
                    "user_id_alias": alias_user_id,
                    "attr_key": attr_key,
                    "current_value": val,
                    "value_counts": counts,
                    "last_seen_at": str(r["created_at"]),
                })

                top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                hist = ", ".join([f"{k}×{n}" for k,n in top])
                summary = f"{kind}/{attr_key}: {val}\nseen: {hist}"

                await _write_revision(conn, card_id, summary, payload, reason="consolidate_kv_v2")

                # update strength/confidence based on evidence counts (v1)
                total_n = 0
                top_n = 0
                for _v in (counts or {}).values():
                    try:
                        _iv = int(_v)
                    except Exception:
                        continue
                    if _iv < 0:
                        continue
                    total_n += _iv
                    if _iv > top_n:
                        top_n = _iv
                if total_n <= 0:
                    total_n = 1
                    top_n = 1
                p_top = float(top_n) / float(total_n) if total_n else 1.0

                strength_target = _clamp01(0.50 + 0.35 * min(1.0, max(0.0, (total_n - 1) / 10.0)))
                new_strength = max(cur_strength, strength_target)

                conf_target = _clamp01(0.30 + 0.40 * p_top + 0.30 * min(1.0, max(0.0, (total_n - 1) / 5.0)))
                new_confidence = _clamp01(0.7 * cur_confidence + 0.3 * conf_target)

                if prev_value is not None and str(prev_value).strip() != val:
                    new_confidence = _clamp01(min(new_confidence, cur_confidence * 0.85))

                if abs(new_strength - cur_strength) > 1e-6 or abs(new_confidence - cur_confidence) > 1e-6:
                    await conn.execute(
                        """
                        UPDATE vantage_card.card_head
                           SET strength=$2,
                               confidence=$3
                         WHERE card_id=$1
                        """,
                        card_id,
                        new_strength,
                        new_confidence,
                    )

                await conn.execute(
                    """
                    INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                    VALUES ($1,'source',$2,'vantage_fact.source')
                    ON CONFLICT DO NOTHING
                    """,
                    card_id, str(source_id)
                )
                if chat_log_id:
                    await conn.execute(
                        """
                        INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                        VALUES ($1,'chat_log',$2,'public.chat_log')
                        ON CONFLICT DO NOTHING
                        """,
                        card_id, str(chat_log_id)
                    )
                await conn.execute(
                    """
                    INSERT INTO vantage_card.card_link(card_id, link_type, ref_id, note)
                    VALUES ($1,'claim',$2,'vantage_fact.claim')
                    ON CONFLICT DO NOTHING
                    """,
                    card_id, str(claim_id)
                )

                updated += 1
                touched_cards.append(card_id)


    # --- cursor observability (only when new sources processed) ---
    if rows:
        max_source_id = max(int(r["source_id"]) for r in rows)

        note_rows = await conn.fetch(
            """
            SELECT note, count(*) AS n
            FROM vantage_card.card_link
            WHERE card_id=$1 AND link_type='source'
            GROUP BY 1
            ORDER BY n DESC, note ASC
            """,
            cursor_card_id,
        )
        note_counts = {str(rr["note"]): int(rr["n"]) for rr in note_rows}
        total_links = int(sum(note_counts.values()))
        ok_n = int(note_counts.get("ok", 0))
        skip_n = int(total_links - ok_n)

        cursor_now = await conn.fetchval("SELECT now()::text")
        done_n = await conn.fetchval(
            "SELECT count(*) FROM vantage_fact.source WHERE source_type='chat_log' AND status='done'"
        )
        done_n = int(done_n or 0)

        cur_head = await conn.fetchrow("SELECT payload FROM vantage_card.card_head WHERE card_id=$1", cursor_card_id)
        cur_payload = (cur_head["payload"] if cur_head else {}) or {}
        if isinstance(cur_payload, str):
            try:
                cur_payload = json.loads(cur_payload)
            except Exception:
                cur_payload = {}
        cur_payload = cur_payload or {}

        cur_payload.update({
            "mode": "consolidate_kv_v2_cursor",
            "cursor_updated_at": str(cursor_now or ""),
            "cursor_done_chatlog_sources": done_n,
            "cursor_link_sources": total_links,
            "cursor_note_counts": note_counts,
            "cursor_last_batch": {
                "processed": len(rows),
                "max_source_id": max_source_id,
                "limit_sources": limit_sources,
            },
        })

        cur_summary = f"cursor: done={done_n} linked={total_links} ok={ok_n} skip={skip_n} last_source_id={max_source_id}"
        await conn.execute(
            """
            UPDATE vantage_card.card_head
               SET updated_at=now(),
                   summary=$2,
                   payload=$3::jsonb
             WHERE card_id=$1
            """,
            cursor_card_id,
            cur_summary,
            _jsonb(cur_payload),
        )

    return {"ok": True, "updated_cards": updated, "card_ids": touched_cards[:50], "limit_sources": limit_sources}

def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


async def card_decay_once(
    conn: asyncpg.Connection,
    vantage_id: str,
    limit_cards: int = 50,
    half_life_days: float = 45.0,
    signal_window_days: int = 180,
    min_interval_minutes: int = 60,
) -> Dict[str, Any]:
    """
    v2: Incremental decay based on payload.last_decay_at (NOT card_head.updated_at).

    - Stores payload.last_decay_at (ISO string via Postgres now()::text)
    - Applies decay using dt since last_decay_at
    - Applies signals only once by summing signals since last_decay_at (bounded by signal_window_days)
    - Does NOT touch card_head.updated_at (content revision time)
    """
    limit_cards = int(limit_cards)
    if limit_cards <= 0:
        return {"ok": True, "updated": 0, "limit_cards": limit_cards}

    half_life_days = float(half_life_days)
    if half_life_days <= 0.0:
        half_life_days = 45.0

    signal_window_days = int(signal_window_days)
    if signal_window_days <= 0:
        signal_window_days = 180

    min_interval_minutes = int(min_interval_minutes)
    if min_interval_minutes < 0:
        min_interval_minutes = 0
    min_interval_days = float(min_interval_minutes) / 1440.0

    cards = await conn.fetch(
        """
        SELECT card_id, kind, topic_key, strength, confidence, updated_at, payload
        FROM vantage_card.card_head
        WHERE vantage_id=$1 AND status='active'::vantage_card.card_status AND kind<>'system'
        ORDER BY updated_at ASC
        LIMIT $2
        """,
        vantage_id,
        limit_cards,
    )

    updated = 0
    touched = []

    async with conn.transaction():
        for c in cards:
            card_id = int(c["card_id"])
            kind = str(c["kind"])
            topic_key = str(c["topic_key"])
            strength = float(c["strength"])
            confidence = float(c["confidence"])

            payload = c["payload"] or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            payload = payload or {}

            def _parse_ts(x):
                if isinstance(x, datetime):
                    return x
                if x is None:
                    return None
                if isinstance(x, str):
                    # stored as 'YYYY-MM-DD HH:MM:SS.ssssss+00' or ISO; normalize 'Z' if present
                    try:
                        return datetime.fromisoformat(x.replace("Z", "+00:00"))
                    except Exception:
                        return None
                return None

            last_ref = _parse_ts(payload.get("last_decay_at")) or c["updated_at"]

            # dt since last_decay_at (or updated_at if missing)
            dt_days = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (now() - $1::timestamptz))/86400.0",
                last_ref,
            )
            dt_days = float(dt_days or 0.0)
            if dt_days < 0.0:
                dt_days = 0.0

            # signals since last_ref (bounded by signal_window_days)
            sig = await conn.fetchrow(
                """
                SELECT
                  COALESCE(sum(CASE WHEN signal_type='reward' THEN magnitude ELSE 0 END),0) AS reward,
                  COALESCE(sum(CASE WHEN signal_type IN ('punish','correction') THEN magnitude ELSE 0 END),0) AS punish,
                  COALESCE(sum(CASE WHEN signal_type='use' THEN magnitude ELSE 0 END),0) AS use
                FROM vantage_card.card_signal
                WHERE vantage_id=$1 AND kind=$2 AND topic_key=$3
                  AND created_at > $4::timestamptz
                  AND created_at >= now() - ($5::int * interval '1 day')
                """,
                vantage_id,
                kind,
                topic_key,
                last_ref,
                signal_window_days,
            )
            reward = float(sig["reward"])
            punish = float(sig["punish"])
            use = float(sig["use"])
            any_signals = (reward + punish + use) > 0.0

            # if nothing new and too soon, skip entirely (avoids minute-loop churn)
            if (not any_signals) and (dt_days < min_interval_days):
                continue

            factor = 0.5 ** (dt_days / half_life_days)

            # bounded deltas (same as v1, but only on *new* signals since last_ref)
            delta = 0.0
            delta += min(0.20, 0.02 * use)
            delta += min(0.20, 0.05 * reward)
            delta -= min(0.30, 0.07 * punish)

            new_strength = _clamp01(strength * factor + delta)

            # confidence decays slower; also only reacts weakly to signals
            conf_half_life = max(180.0, half_life_days * 4.0)
            conf_factor = 0.5 ** (dt_days / conf_half_life)
            new_confidence = _clamp01(
                confidence * conf_factor
                + min(0.10, 0.01 * reward)
                - min(0.15, 0.02 * punish)
            )

            # match numeric(4,3) storage; avoid micro-updates
            new_strength = round(new_strength, 3)
            new_confidence = round(new_confidence, 3)
            old_strength = round(strength, 3)
            old_confidence = round(confidence, 3)

            if new_strength != old_strength or new_confidence != old_confidence or any_signals or dt_days >= min_interval_days:
                await conn.execute(
                    """
                    UPDATE vantage_card.card_head
                       SET strength=$2,
                           confidence=$3,
                           payload=jsonb_set(payload,'{last_decay_at}', to_jsonb(now()::text), true)
                     WHERE card_id=$1
                    """,
                    card_id,
                    new_strength,
                    new_confidence,
                )
                updated += 1
                touched.append(card_id)

    return {
        "ok": True,
        "job": "card_decay_v1",
        "updated": updated,
        "touched_card_ids": touched[:50],
        "limit_cards": limit_cards,
        "half_life_days": half_life_days,
        "signal_window_days": signal_window_days,
        "min_interval_minutes": min_interval_minutes,
    }
