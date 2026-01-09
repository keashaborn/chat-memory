import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg


_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _\-/]{0,64})\s*:\s*(.{1,500})\s*$")


def _jsonb(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _norm_key(k: str) -> str:
    k = k.strip().lower()
    k = re.sub(r"[^a-z0-9]+", "_", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k[:64] if k else "unknown"


def parse_kv_facts(content: str, max_facts: int = 50) -> List[Dict[str, Any]]:
    """
    Deterministic bootstrap extractor: parses 'Key: Value' lines.
    Emits facts as: predicate, value_str, span_start, span_end, snippet.
    """
    facts: List[Dict[str, Any]] = []
    if not content:
        return facts

    # Track offsets so we can attach evidence spans.
    offset = 0
    for line in content.splitlines():
        m = _KV_RE.match(line)
        if m:
            key = _norm_key(m.group(1))
            val = m.group(2).strip()
            pred = f"attr.{key}"
            span_start = content.find(line, offset)
            if span_start < 0:
                span_start = None
                span_end = None
            else:
                span_end = span_start + len(line)
            facts.append(
                {
                    "predicate": pred,
                    "value": val,
                    "span_start": span_start,
                    "span_end": span_end,
                    "snippet": line[:400],
                }
            )
            if len(facts) >= max_facts:
                break
        offset += len(line) + 1
    return facts


async def ensure_predicate(conn: asyncpg.Connection, predicate: str, cardinality: str = "one", description: str = "") -> None:
    arg_schema = {"cardinality": cardinality}
    await conn.execute(
        """
        INSERT INTO vantage_fact.predicate(predicate, arg_schema, description)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (predicate) DO NOTHING
        """,
        predicate,
        _jsonb(arg_schema),
        description or None,
    )


async def get_or_create_entity(conn: asyncpg.Connection, entity_type: str, canonical_name: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT entity_id
        FROM vantage_fact.entity
        WHERE entity_type=$1 AND canonical_name=$2
        ORDER BY entity_id ASC
        LIMIT 1
        """,
        entity_type,
        canonical_name,
    )
    if row:
        return int(row["entity_id"])

    eid = await conn.fetchval(
        """
        INSERT INTO vantage_fact.entity(entity_type, canonical_name)
        VALUES ($1, $2)
        RETURNING entity_id
        """,
        entity_type,
        canonical_name,
    )
    return int(eid)


async def upsert_claim_literal(
    conn: asyncpg.Connection,
    subject_entity_id: int,
    predicate: str,
    value_str: str,
    qualifiers: Optional[Dict[str, Any]] = None,
    confidence: float = 0.55,
) -> int:
    qualifiers = qualifiers or {}
    obj = {"type": "str", "v": value_str}
    canonical_key = _sha256_hex(
        f"s={subject_entity_id}|p={predicate}|ol={_jsonb(obj)}|q={_jsonb(qualifiers)}"
    )

    claim_id = await conn.fetchval(
        """
        INSERT INTO vantage_fact.claim(
            subject_entity_id, predicate, object_literal, qualifiers, confidence, status, canonical_key
        )
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, 'active'::vantage_fact.claim_status, $6)
        ON CONFLICT (canonical_key) DO UPDATE
            SET updated_at=now(),
                confidence=GREATEST(vantage_fact.claim.confidence, EXCLUDED.confidence)
        RETURNING claim_id
        """,
        int(subject_entity_id),
        predicate,
        _jsonb(obj),
        _jsonb(qualifiers),
        float(confidence),
        canonical_key,
    )
    return int(claim_id)


async def add_evidence(
    conn: asyncpg.Connection,
    claim_id: int,
    source_id: int,
    span_start: Optional[int],
    span_end: Optional[int],
    snippet: Optional[str],
    extractor: str,
    extractor_version: str,
    extraction_confidence: float,
) -> None:
    await conn.execute(
        """
        INSERT INTO vantage_fact.evidence(
            claim_id, source_id, span_start, span_end, snippet, extractor, extractor_version, extraction_confidence
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        int(claim_id),
        int(source_id),
        span_start,
        span_end,
        snippet,
        extractor,
        extractor_version,
        float(extraction_confidence),
    )


async def compute_fact_drives(conn: asyncpg.Connection) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM vantage_fact.source WHERE status='pending')      AS pending_sources,
          (SELECT count(*) FROM vantage_fact.source WHERE status='processing')   AS processing_sources,
          (SELECT count(*) FROM vantage_fact.source WHERE status='error')        AS error_sources,
          (SELECT count(*) FROM vantage_fact.entity)                             AS entities,
          (SELECT count(*) FROM vantage_fact.claim WHERE status='active')        AS active_claims,
          (SELECT count(*) FROM vantage_fact.claim WHERE status='active' AND confidence < 0.50) AS low_conf_claims,
          (SELECT count(*) FROM vantage_fact.contradiction WHERE status='open')  AS open_contradictions
        """
    )
    return {
        "mode": "fact_drives_v1",
        "ts_unix": time.time(),
        "pending_sources": int(row["pending_sources"]),
        "processing_sources": int(row["processing_sources"]),
        "error_sources": int(row["error_sources"]),
        "entities": int(row["entities"]),
        "active_claims": int(row["active_claims"]),
        "low_conf_claims": int(row["low_conf_claims"]),
        "open_contradictions": int(row["open_contradictions"]),
    }


async def fact_extract_once(conn: asyncpg.Connection, max_facts: int = 50) -> Dict[str, Any]:
    """
    Claims ONE pending source, marks processing, extracts deterministic KV facts,
    writes entities/claims/evidence, marks source done.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            WITH c AS (
              SELECT source_id, title, content
              FROM vantage_fact.source
              WHERE status='pending'
              ORDER BY source_id ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE vantage_fact.source s
               SET status='processing'::vantage_fact.source_status,
                   updated_at=now()
              FROM c
             WHERE s.source_id=c.source_id
            RETURNING s.source_id, c.title, c.content
            """
        )
        if not row:
            return {"ok": True, "processed_source_id": None, "claims_upserted": 0, "facts_found": 0}

        source_id = int(row["source_id"])
        title = (row["title"] or "").strip()
        content = row["content"] or ""

        # Set content hash
        content_sha = _sha256_hex(content)
        await conn.execute(
            """
            UPDATE vantage_fact.source
               SET content_sha256=$2,
                   updated_at=now()
             WHERE source_id=$1
            """,
            source_id,
            content_sha,
        )

        doc_name = title if title else f"source:{source_id}"
        doc_eid = await get_or_create_entity(conn, "document", doc_name)

        # Always record doc.content_sha256 as a claim
        await ensure_predicate(conn, "doc.content_sha256", cardinality="one", description="sha256 of source content")
        c0 = await upsert_claim_literal(conn, doc_eid, "doc.content_sha256", content_sha, confidence=0.90)
        await add_evidence(conn, c0, source_id, None, None, None, "kv_extractor", "v1", 0.90)

        facts = parse_kv_facts(content, max_facts=max_facts)

        claims_upserted = 1
        for f in facts:
            pred = f["predicate"]
            val = f["value"]
            await ensure_predicate(conn, pred, cardinality="one", description="key-value attribute from source")
            cid = await upsert_claim_literal(conn, doc_eid, pred, val, confidence=0.60)
            await add_evidence(
                conn,
                cid,
                source_id,
                f.get("span_start"),
                f.get("span_end"),
                f.get("snippet"),
                "kv_extractor",
                "v1",
                0.60,
            )
            claims_upserted += 1

        await conn.execute(
            """
            UPDATE vantage_fact.source
               SET status='done'::vantage_fact.source_status,
                   processed_at=now(),
                   updated_at=now(),
                   error=NULL
             WHERE source_id=$1
            """,
            source_id,
        )

        return {
            "ok": True,
            "processed_source_id": source_id,
            "doc_entity_id": doc_eid,
            "facts_found": len(facts),
            "claims_upserted": claims_upserted,
        }


async def fact_contradiction_scan_once(conn: asyncpg.Connection, max_groups: int = 10) -> Dict[str, Any]:
    """
    Creates contradiction objects for cardinality=one predicates where a subject has >1 distinct active value.
    """
    max_groups = int(max_groups)
    created = 0
    groups_scanned = 0

    rows = await conn.fetch(
        """
        WITH single_preds AS (
          SELECT predicate
          FROM vantage_fact.predicate
          WHERE (arg_schema->>'cardinality')='one'
        ),
        g AS (
          SELECT
            c.subject_entity_id,
            c.predicate,
            count(*) AS n,
            count(distinct coalesce(c.object_entity_id::text, c.object_literal::text)) AS distinct_n,
            array_agg(c.claim_id ORDER BY c.claim_id) AS claim_ids
          FROM vantage_fact.claim c
          WHERE c.status='active'
            AND c.predicate IN (SELECT predicate FROM single_preds)
          GROUP BY c.subject_entity_id, c.predicate
          HAVING count(distinct coalesce(c.object_entity_id::text, c.object_literal::text)) > 1
          ORDER BY distinct_n DESC, n DESC
          LIMIT $1
        )
        SELECT subject_entity_id, predicate, n, distinct_n, claim_ids
        FROM g
        """,
        max_groups,
    )

    async with conn.transaction():
        for r in rows:
            groups_scanned += 1
            subject_entity_id = int(r["subject_entity_id"])
            predicate = str(r["predicate"])
            claim_ids = list(r["claim_ids"] or [])
            qualifier_key = ""  # v1: no qualifier bucketing yet

            cid = await conn.fetchval(
                """
                SELECT contradiction_id
                FROM vantage_fact.contradiction
                WHERE subject_entity_id=$1 AND predicate=$2 AND qualifier_key=$3 AND status='open'
                ORDER BY contradiction_id DESC
                LIMIT 1
                """,
                subject_entity_id,
                predicate,
                qualifier_key,
            )
            if not cid:
                cid = await conn.fetchval(
                    """
                    INSERT INTO vantage_fact.contradiction(subject_entity_id, predicate, qualifier_key, status, description)
                    VALUES ($1,$2,$3,'open'::vantage_fact.contradiction_status,$4)
                    RETURNING contradiction_id
                    """,
                    subject_entity_id,
                    predicate,
                    qualifier_key,
                    "cardinality=one but multiple distinct active values",
                )
                created += 1

            for claim_id in claim_ids:
                await conn.execute(
                    """
                    INSERT INTO vantage_fact.contradiction_member(contradiction_id, claim_id)
                    VALUES ($1,$2)
                    ON CONFLICT DO NOTHING
                    """,
                    int(cid),
                    int(claim_id),
                )

            await conn.execute(
                """
                UPDATE vantage_fact.contradiction
                   SET updated_at=now()
                 WHERE contradiction_id=$1
                """,
                int(cid),
            )

    return {"ok": True, "groups_scanned": groups_scanned, "contradictions_created": created, "max_groups": max_groups}

async def fact_seed_from_chat_log_once(conn: asyncpg.Connection, vantage_id: str, limit: int = 10) -> Dict[str, Any]:
    """Insert up to `limit` new user chat_log rows as pending sources (deduped by external_id).

    v1 policy:
      - only ingest rows that contain at least one 'Key: Value' line (matches kv_extractor)
      - newest-first so it tracks ongoing work rather than backfilling ancient history
      - hard cap on row length to avoid pathological inserts
    """
    limit = int(limit)
    if limit <= 0:
        return {"ok": True, "inserted": 0, "limit": limit, "vantage_id": vantage_id}

    row = await conn.fetchrow(
        """
        WITH candidates AS (
          SELECT cl.id, cl.user_id, cl.thread_id, cl.vantage_id, cl.created_at, cl.text
          FROM public.chat_log cl
          LEFT JOIN vantage_fact.source s
            ON s.external_id = ('chat_log:' || cl.id::text)
          WHERE cl.source = 'frontend/chat:user'
            AND cl.text IS NOT NULL
            AND length(cl.text) > 0
            AND length(cl.text) <= 8000
            -- must contain at least one KV-ish line, otherwise kv_extractor yields nothing
            AND cl.text ~ '(^|\n)[[:space:]]*[A-Za-z][A-Za-z0-9 _]*[[:space:]]*:[[:space:]]*[^\n]+'
            AND s.source_id IS NULL
            AND (
              ($1 = 'default' AND (cl.vantage_id IS NULL OR cl.vantage_id = 'default'))
              OR (cl.vantage_id = $1)
            )
          ORDER BY cl.created_at DESC
          LIMIT $2
        ),
        ins AS (
          INSERT INTO vantage_fact.source(source_type, external_id, title, content, metadata, status)
          SELECT
            'chat_log'::text,
            'chat_log:' || id::text,
            'chat_log:user:' || COALESCE(vantage_id, '<NULL>') || ':' || id::text,
            text,
            jsonb_build_object(
              'origin','public.chat_log',
              'chat_log_id', id::text,
              'role','user',
              'user_id', user_id,
              'thread_id', CASE WHEN thread_id IS NULL THEN NULL ELSE thread_id::text END,
              'vantage_id', CASE WHEN vantage_id IS NULL THEN NULL ELSE vantage_id END,
              'created_at', created_at
            ),
            'pending'::vantage_fact.source_status
          FROM candidates
          RETURNING source_id
        )
        SELECT count(*) AS inserted FROM ins
        """,
        vantage_id,
        limit,
    )
    inserted = int(row["inserted"] or 0)
    return {"ok": True, "inserted": inserted, "limit": limit, "vantage_id": vantage_id}
