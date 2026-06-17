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


_NL_FEEDBACK_PATTERNS = [
    # predicate, value, regex, description
    (
        "attr.response_length",
        "concise",
        r"\b(prefer|like|liked|want|try|keep|kept|make|give)\b.{0,80}\b(short|shorter|concise|brief)\b",
        "user preference for concise responses",
    ),
    (
        "attr.response_length",
        "avoid_long_responses",
        r"\b(don'?t like|do not like|avoid|too much|too long|long responses|really long)\b.{0,80}\b(long|lengthy|responses?|answers?)\b",
        "user preference against long responses",
    ),
    (
        "attr.format",
        "no_bullet_points",
        r"\b(no bullet points|don'?t like bullet points|do not like bullet points|prefer no bullet points)\b",
        "user preference against bullet points",
    ),
    (
        "attr.format",
        "prose_over_bullets",
        r"\b(prefer|like|want)\b.{0,80}\b(prose)\b.{0,80}\b(over|instead of|rather than)\b.{0,80}\b(bullet|numbered)\b",
        "user preference for prose over bullets",
    ),
    (
        "attr.format",
        "avoid_numbered_answers",
        r"\b(don'?t like|do not like|avoid)\b.{0,80}\b(numbered answers|numbered lists|numbered)\b",
        "user preference against numbered answers",
    ),
    (
        "attr.style",
        "direct",
        r"\b(prefer|like|want|make|be)\b.{0,80}\b(direct|straightforward)\b",
        "user preference for direct style",
    ),
    (
        "attr.style",
        "poetic_straightforward",
        r"\b(poetic)\b.{0,80}\b(straightforward|direct)\b|\b(straightforward|direct)\b.{0,80}\b(poetic)\b",
        "user preference for poetic but straightforward style",
    ),
    (
        "attr.specificity",
        "less_generic",
        r"\b(too generic|not specific enough|more specific|less generic)\b",
        "user preference for less generic answers",
    ),
    (
        "attr.feedback_positive",
        "helpful_or_insightful",
        r"\b(great job|nice answer|insightful|helpful response|made a lot of sense|good answer)\b",
        "positive feedback about a response",
    ),
]


def parse_nl_feedback_facts(content: str, max_facts: int = 20) -> List[Dict[str, Any]]:
    """
    Deterministic natural-language feedback extractor.

    This is intentionally conservative. It only captures obvious user feedback
    about response style/format/length/specificity.
    """
    facts: List[Dict[str, Any]] = []
    if not content:
        return facts

    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return facts

    # Split into rough sentence-like chunks while preserving enough context.
    chunks = re.split(r"(?<=[.!?])\s+|\n+", compact)
    if not chunks:
        chunks = [compact]

    seen = set()
    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue
        low = c.lower()

        for predicate, value, pattern, description in _NL_FEEDBACK_PATTERNS:
            if not re.search(pattern, low, flags=re.IGNORECASE):
                continue

            key = (predicate, value)
            if key in seen:
                continue
            seen.add(key)

            span_start = content.lower().find(c[:60].lower())
            if span_start < 0:
                span_start = None
                span_end = None
            else:
                span_end = span_start + len(c)

            facts.append({
                "predicate": predicate,
                "value": value,
                "span_start": span_start,
                "span_end": span_end,
                "snippet": c[:400],
                "extractor": "nl_feedback_extractor",
                "extractor_version": "v1",
                "confidence": 0.56,
                "description": description,
            })

            if len(facts) >= max_facts:
                return facts

    return facts



def parse_nl_profile_facts(content: str, max_facts: int = 20) -> List[Dict[str, Any]]:
    """
    Conservative natural-language profile extractor.

    Captures obvious identity/background/project/preference facts from normal
    speech without requiring Key: Value syntax.
    """
    facts: List[Dict[str, Any]] = []
    if not content:
        return facts

    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return facts

    chunks = re.split(r"(?<=[.!?])\s+|\n+", compact)
    if not chunks:
        chunks = [compact]

    seen = set()

    def add(predicate: str, value: str, chunk: str, confidence: float, description: str):
        nonlocal facts
        value = str(value or "").strip().strip(".")
        if not value:
            return
        key = (predicate, value.lower())
        if key in seen:
            return
        seen.add(key)

        span_start = content.lower().find(chunk[:60].lower())
        if span_start < 0:
            span_start = None
            span_end = None
        else:
            span_end = span_start + len(chunk)

        facts.append({
            "predicate": predicate,
            "value": value,
            "span_start": span_start,
            "span_end": span_end,
            "snippet": chunk[:400],
            "extractor": "nl_profile_extractor",
            "extractor_version": "v1",
            "confidence": confidence,
            "description": description,
        })

    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue
        low = c.lower()

        # Identity: explicit naming only. Avoid treating every "I'm X" as a name.
        m = re.search(r"\b(?:my name is|call me|preferred name is)\s+([A-Z][A-Za-z]{1,40})\b", c, flags=re.IGNORECASE)
        if m:
            add("attr.identity_preferred_name", m.group(1), c, 0.78, "explicit preferred name")

        # Background/profession.
        profession_map = [
            (r"\b(?:i am|i'm)\s+(?:a\s+|an\s+)?clinical psychologist\b", "clinical psychologist"),
            (r"\b(?:i am|i'm)\s+(?:a\s+|an\s+)?psychologist\b", "psychologist"),
            (r"\b(?:i am|i'm)\s+(?:a\s+|an\s+)?bcba\b", "BCBA"),
            (r"\b(?:i am|i'm)\s+(?:a\s+|an\s+)?behavior analyst\b", "behavior analyst"),
            (r"\bmy professional background is\b.{0,80}\bpsycholog", "psychology"),
        ]
        for pat, val in profession_map:
            if re.search(pat, low, flags=re.IGNORECASE):
                add("attr.background_profession", val, c, 0.70, "explicit professional background")
                break

        # Company/career history.
        if re.search(r"\b(i founded|i started|i built|founded)\b.{0,80}\bcaravel\b", low, flags=re.IGNORECASE):
            add("attr.background_company_history", "founded Caravel Autism Health", c, 0.72, "explicit company/career history")

        # Current life/work status.
        if re.search(r"\b(i am|i'm)\s+retired\b|\bretired now\b", low, flags=re.IGNORECASE):
            add("attr.background_status", "retired", c, 0.70, "explicit retirement/work status")

        # Current work focus.
        if re.search(r"\b(current work|work now|main work|most of my current work)\b.{0,80}\b(focused on|is|centers on|centered on)\b.{0,80}\bverbal\s*sage\b", low, flags=re.IGNORECASE):
            add("attr.project_current_focus", "Verbal Sage", c, 0.72, "explicit current work focus")

        # Current project.
        if re.search(r"\b(i am|i'm|we are|we're)\b.{0,80}\b(building|creating|working on)\b.{0,80}\bverbal\s*sage\b", low, flags=re.IGNORECASE):
            add("attr.project_current_project", "Verbal Sage", c, 0.72, "explicit current project")
        elif re.search(r"\bverbal\s*sage\b", low, flags=re.IGNORECASE) and re.search(r"\b(project|app|system|website)\b", low, flags=re.IGNORECASE):
            add("attr.project_current_project", "Verbal Sage", c, 0.62, "current project reference")

        if re.search(r"\b(seebx|see bx)\b", low, flags=re.IGNORECASE) and re.search(r"\b(company|project|app|system)\b", low, flags=re.IGNORECASE):
            add("attr.project_company", "SeeBX", c, 0.62, "project/company reference")

        # Stable voice preference.
        if re.search(r"\b(prefer|usually use|like|want)\b.{0,60}\bjuniper\b.{0,30}\bvoice\b|\bjuniper\b.{0,30}\bvoice\b", low, flags=re.IGNORECASE):
            add("attr.preference_voice", "Juniper", c, 0.68, "explicit voice preference")

        if len(facts) >= max_facts:
            return facts[:max_facts]

    return facts[:max_facts]


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


async def compute_fact_drives(conn: asyncpg.Connection, vantage_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute fact-pipeline drives for one Vantage.

    Sources are scoped by metadata.vantage_id.
    Claims are scoped indirectly through evidence -> source.
    """
    vid = str(vantage_id or "").strip() or None

    row = await conn.fetchrow(
        """
        WITH scoped_sources AS (
          SELECT source_id
          FROM vantage_fact.source
          WHERE (
            $1::text IS NULL
            OR ($1::text = 'default' AND COALESCE(metadata->>'vantage_id', 'default') = 'default')
            OR (metadata->>'vantage_id' = $1::text)
          )
        ),
        scoped_claims AS (
          SELECT DISTINCT c.claim_id
          FROM vantage_fact.claim c
          JOIN vantage_fact.evidence e ON e.claim_id = c.claim_id
          JOIN scoped_sources ss ON ss.source_id = e.source_id
          WHERE c.status='active'
        ),
        scoped_contradictions AS (
          SELECT DISTINCT cm.contradiction_id
          FROM vantage_fact.contradiction_member cm
          JOIN scoped_claims sc ON sc.claim_id = cm.claim_id
          JOIN vantage_fact.contradiction x ON x.contradiction_id = cm.contradiction_id
          WHERE x.status='open'
        )
        SELECT
          (SELECT count(*) FROM vantage_fact.source s JOIN scoped_sources ss ON ss.source_id=s.source_id WHERE s.status='pending') AS pending_sources,
          (SELECT count(*) FROM vantage_fact.source s JOIN scoped_sources ss ON ss.source_id=s.source_id WHERE s.status='processing') AS processing_sources,
          (SELECT count(*) FROM vantage_fact.source s JOIN scoped_sources ss ON ss.source_id=s.source_id WHERE s.status='error') AS error_sources,
          (SELECT count(*) FROM vantage_fact.entity) AS entities,
          (SELECT count(*) FROM scoped_claims) AS active_claims,
          (SELECT count(*)
             FROM vantage_fact.claim c
             JOIN scoped_claims sc ON sc.claim_id=c.claim_id
            WHERE c.confidence < 0.50) AS low_conf_claims,
          (SELECT count(*) FROM scoped_contradictions) AS open_contradictions
        """,
        vid,
    )
    return {
        "mode": "fact_drives_v1",
        "vantage_id": vid,
        "ts_unix": time.time(),
        "pending_sources": int(row["pending_sources"]),
        "processing_sources": int(row["processing_sources"]),
        "error_sources": int(row["error_sources"]),
        "entities": int(row["entities"]),
        "active_claims": int(row["active_claims"]),
        "low_conf_claims": int(row["low_conf_claims"]),
        "open_contradictions": int(row["open_contradictions"]),
    }


async def fact_extract_once(conn: asyncpg.Connection, vantage_id: Optional[str] = None, max_facts: int = 50) -> Dict[str, Any]:
    """
    Claims ONE pending source for this Vantage, marks processing, extracts deterministic facts,
    writes entities/claims/evidence, marks source done.
    """
    vid = str(vantage_id or "").strip() or None
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            WITH c AS (
              SELECT source_id, title, content
              FROM vantage_fact.source
              WHERE status='pending'
                AND (
                  $1::text IS NULL
                  OR ($1::text = 'default' AND COALESCE(metadata->>'vantage_id', 'default') = 'default')
                  OR (metadata->>'vantage_id' = $1::text)
                )
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
            """,
            vid,
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
        if len(facts) < max_facts:
            facts.extend(parse_nl_feedback_facts(content, max_facts=max_facts - len(facts)))
        if len(facts) < max_facts:
            facts.extend(parse_nl_profile_facts(content, max_facts=max_facts - len(facts)))

        claims_upserted = 1
        for f in facts:
            pred = f["predicate"]
            val = f["value"]
            confidence = float(f.get("confidence", 0.60))
            extractor = str(f.get("extractor", "kv_extractor"))
            extractor_version = str(f.get("extractor_version", "v1"))
            description = str(f.get("description", "attribute from source"))
            await ensure_predicate(conn, pred, cardinality="one", description=description)
            cid = await upsert_claim_literal(conn, doc_eid, pred, val, confidence=confidence)
            await add_evidence(
                conn,
                cid,
                source_id,
                f.get("span_start"),
                f.get("span_end"),
                f.get("snippet"),
                extractor,
                extractor_version,
                confidence,
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
            "vantage_id": vid,
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
      - ingest recent user chat rows for this Vantage
      - extraction may produce KV facts or conservative natural-language feedback facts
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
