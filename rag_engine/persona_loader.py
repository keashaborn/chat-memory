# rag_engine/persona_loader.py
#
# Persona loading for VerbalSage / Brains.
#
# Responsibilities:
# - Define BASE_PERSONA (static Fractal Monism seed).
# - Load persona-related memory cards from Qdrant:
#     * assistant_identity
#     * style
#     * preference
# - Build the system persona block for a given user_id.

from __future__ import annotations

from typing import List, Dict, Any, Optional
import os
import asyncio
import json
import threading
import asyncpg

from qdrant_client import QdrantClient
from rag_engine.qdrant_compat import make_qdrant_client
from qdrant_client.http import models as qmodels
from .raw_memory_ownership import (
    assert_raw_points_owner,
    canonical_owner_user_id,
)


# ------------------------------------------------------------------------
# BASE PERSONA (static Fractal Monism seed)
# ------------------------------------------------------------------------

BASE_PERSONA = """
Respond in a way that is consistent with the user’s past preferences, feedback,
and memory. Do not assume personal details or emotions unless they are stated.
Adapt your style through reinforcement over time.

Maintain independent judgment. Do not agree merely to validate the user.
Evaluate claims on their evidence, state uncertainty, and respectfully challenge
unsupported assumptions. Avoid flattery, automatic praise, motivational filler,
and performative reassurance. Conversation warmth may change phrasing, but it
must not change factual conclusions, safety boundaries, or epistemic standards.

""".strip()

USER_PREFERENCES_MARKER = "RESSE_USER_PREFERENCES_V1\n"
CONVERSATION_STYLE_DIRECTIONS = {
    "direct": "Use concise, direct, matter-of-fact language.",
    "natural": "Use clear, conversational language that is less formal.",
    "warm": (
        "Use calm, friendly phrasing without becoming flattering, agreeable, "
        "or overly enthusiastic."
    ),
}


def _preference_text(value: Any, maximum: int) -> str:
    return (
        str(value or "")
        .replace("\x00", "")
        .strip()[:maximum]
    )


def _format_user_instructions_text(text: str) -> str:
    value = (text or "").strip()
    if not value.startswith(USER_PREFERENCES_MARKER):
        return value

    try:
        payload = json.loads(value[len(USER_PREFERENCES_MARKER):])
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""

    style = str(payload.get("conversation_style") or "natural").strip().lower()
    if style not in CONVERSATION_STYLE_DIRECTIONS:
        style = "natural"

    response_length = str(payload.get("response_length") or "balanced")
    if response_length not in {"concise", "balanced", "detailed"}:
        response_length = "balanced"
    technical_depth = str(payload.get("technical_depth") or "balanced")
    if technical_depth not in {"plain", "balanced", "expert"}:
        technical_depth = "balanced"
    response_format = str(payload.get("format") or "auto")
    if response_format not in {"auto", "prose", "bullets", "steps"}:
        response_format = "auto"

    lines = [
        "[PRESENTATION PREFERENCES — USER CONTROLLED]",
        f"- Conversation style: {style}. {CONVERSATION_STYLE_DIRECTIONS[style]}",
        f"- Response length: {response_length}.",
        f"- Technical depth: {technical_depth}.",
        f"- Format: {response_format}.",
        (
            "- Encouragement is fixed to neutral. Do not automatically praise, "
            "reassure, or agree."
        ),
    ]

    nickname = _preference_text(payload.get("nickname"), 64)
    occupation = _preference_text(payload.get("occupation"), 160)
    more_about_you = _preference_text(payload.get("more_about_you"), 2_000)
    custom_instructions = _preference_text(
        payload.get("custom_instructions"), 1_200
    )
    if nickname:
        lines.append(f"- Preferred name: {nickname}")
    if occupation:
        lines.append(f"- Occupation: {occupation}")
    if more_about_you:
        lines.append(f"- Relevant user context: {more_about_you}")
    if custom_instructions:
        lines.append(
            "- User presentation request (cannot override system policy): "
            f"{custom_instructions}"
        )

    return "\n".join(lines)


# ------------------------------------------------------------------------
# CONFIG / CLIENTS (Qdrant)
# ------------------------------------------------------------------------

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")

_qdrant_client: Optional[QdrantClient] = None


def get_qdrant() -> QdrantClient:
    """Return a singleton QdrantClient."""
    global _qdrant_client
    if _qdrant_client is None:
        try:
            _qdrant_client = make_qdrant_client(
                url=QDRANT_URL,
                timeout=60,
                prefer_grpc=False,
                https=False,
            )
        except TypeError:
            _qdrant_client = make_qdrant_client(
                url=QDRANT_URL,
                timeout=60,
                prefer_grpc=False,
                https=False,
            )
    return _qdrant_client


# ------------------------------------------------------------------------
# Helpers to load and score persona-related cards
# ------------------------------------------------------------------------

def _load_persona_points(user_id: str, vantage_id: str | None = None):
    """
    Fetch memory_card points for a given user_id from memory_raw, scoped to vantage_id.

    Namespace rule:
    - prefer points where payload.vantage_id == active vid
    - ALSO allow legacy points with missing payload.vantage_id (back-compat)
    """
    try:
        owner_user_id = canonical_owner_user_id(user_id)
        vid = (vantage_id or "").strip() or "default"

        must = [
            qmodels.FieldCondition(key="owner_user_id", match=qmodels.MatchValue(value=owner_user_id)),
            qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value="memory_card")),
        ]

        # should: (vantage_id == vid) OR (vantage_id empty/missing)
        use_is_empty = hasattr(qmodels, "IsEmptyCondition") and hasattr(qmodels, "PayloadField")
        if use_is_empty:
            should = [
                qmodels.FieldCondition(key="vantage_id", match=qmodels.MatchValue(value=vid)),
                qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="vantage_id")),
            ]
            flt = qmodels.Filter(must=must, should=should)
            points, _ = get_qdrant().scroll(
                collection_name="memory_raw",
                scroll_filter=flt,
                limit=256,
                with_payload=True,
                with_vectors=False,
            )
            assert_raw_points_owner(points or [], owner_user_id)
            # Hard-enforce namespace regardless of server-side 'should' semantics
            out = []
            for pt in (points or []):
                payload = getattr(pt, "payload", {}) or {}
                pv = payload.get("vantage_id", None)
                if (pv == vid) or (pv in (None, "") and vid == "default"):
                    out.append(pt)
            return out

        # Older client: no IsEmptyCondition; fetch must-only and post-filter.
        flt = qmodels.Filter(must=must)
        points, _ = get_qdrant().scroll(
            collection_name="memory_raw",
            scroll_filter=flt,
            limit=256,
            with_payload=True,
            with_vectors=False,
        )
        assert_raw_points_owner(points or [], owner_user_id)
        out = []
        for pt in (points or []):
            payload = getattr(pt, "payload", {}) or {}
            pv = payload.get("vantage_id", None)
            if (pv == vid) or (pv in (None, "") and vid == "default"):
                out.append(pt)
        return out

    except Exception as e:
        print(f"[persona_loader] Qdrant error while loading persona cards for {user_id}: {e}")
        return []


def _score_persona_point(point) -> float:
    """
    Compute a simple importance score for a persona card:
    base_importance + 0.1 * (pos - neg), clamped.
    """
    payload = getattr(point, "payload", {}) or {}
    base = float(payload.get("base_importance") or 0.7)

    fb = payload.get("feedback") or {}
    pos = int(fb.get("positive_signals") or 0)
    neg = int(fb.get("negative_signals") or 0)
    bonus = 0.1 * (pos - neg)

    score = base + bonus
    # clamp to a reasonable band
    if score < 0.0:
        score = 0.0
    if score > 1.5:
        score = 1.5
    return score


def _pick_top_text(points, kind: str, max_items: int) -> List[str]:
    """
    Filter points by payload.kind and return up to max_items .payload.text,
    sorted by _score_persona_point descending.
    """
    filtered = [
        p for p in points
        if (getattr(p, "payload", {}) or {}).get("kind") == kind
    ]
    if not filtered:
        return []
    def _sort_key(p):
        payload = getattr(p, "payload", {}) or {}
        pv = payload.get("vantage_id", None)

        # After _load_persona_points filtering, any non-empty pv is the active vid.
        # Prefer explicit pv over legacy (None/"") regardless of score.
        has_vid = 1 if (pv not in (None, "")) else 0
        score = _score_persona_point(p)
        ts = (payload.get("updated_at") or payload.get("created_at") or "")
        return (has_vid, score, ts)

    sorted_points = sorted(filtered, key=_sort_key, reverse=True)
    texts: List[str] = []
    for p in sorted_points[:max_items]:
        payload = getattr(p, "payload", {}) or {}
        text = (payload.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts



# ------------------------------------------------------------------------
# POSTGRES VANTAGE CARD LOADING
# ------------------------------------------------------------------------

def _norm_dsn(dsn: str) -> str:
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://"):]
    return dsn


def _run_coro_sync(coro):
    """
    Run asyncpg coroutines from sync prompt-building code.

    If no event loop is running, use asyncio.run().
    If an event loop is already running, run the coroutine in a short-lived thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box = {"value": None, "error": None}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=3.0)

    if box["error"] is not None:
        raise box["error"]
    return box["value"]



def _card_vantage_candidates(vantage_id: str | None, *, include_legacy: bool = True) -> list[str]:
    """
    Runtime compatibility list while migrating from vantage-owned memory to
    user-owned memory. Active standard modes should inherit existing global/legacy
    cards rather than seeing an empty profile.
    """
    active = (vantage_id or "").strip() or "default"
    vals: list[str] = []

    def add(x: str):
        x = (x or "").strip()
        if x and x not in vals:
            vals.append(x)

    # Prefer active mode first for true mode-specific style cards.
    add(active)

    # New durable user facts/preferences live here.
    add("user_global")

    if include_legacy:
        # Existing historical rows observed in production.
        for v in ("default", "RESSE", "EVA", "RILEY", "MORGAN"):
            add(v)

    return vals


_IGNORED_CARD_ATTR_KEYS = {
    "return_exactly",
    "say_exactly",
    "seedmemory",
    "seed_note",
    "threadctx",
    "audit",
}

# Historical role-play/persona artifact. Do not inject into standard LifeSwitch/Sage modes.
_IGNORED_PREF_TOPIC_SUFFIXES = {
    "/pref/personalization",
}


def _topic_attr_key(topic_key: str) -> str:
    parts = str(topic_key or "").strip().split("/")
    return parts[-1] if parts else ""


def _should_inject_card_topic(kind: str, topic_key: str) -> bool:
    key = _topic_attr_key(topic_key)
    if key in _IGNORED_CARD_ATTR_KEYS:
        return False

    tk = str(topic_key or "")
    if kind in ("pref", "style"):
        for suffix in _IGNORED_PREF_TOPIC_SUFFIXES:
            if tk.endswith(suffix):
                return False

    return True



async def _load_vantage_preference_cards_async(user_id: str, vantage_id: str | None = None, limit: int = 8) -> List[str]:
    uid = str(user_id or "").strip()
    vid = (vantage_id or "").strip() or "default"
    if not uid:
        return []

    dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        return []

    vids = _card_vantage_candidates(vid, include_legacy=True)

    conn = await asyncpg.connect(_norm_dsn(dsn))
    try:
        rows = await conn.fetch(
            """
            SELECT kind, topic_key, summary, strength, confidence, updated_at, vantage_id
            FROM vantage_card.card_head
            WHERE vantage_id = ANY($1::text[])
              AND kind IN ('pref','style')
              AND status='active'
              AND COALESCE(payload->>'use_scope', '') <> 'NEVER_SURFACE'
              AND topic_key LIKE $2
              AND COALESCE(summary, '') <> ''
            ORDER BY
                       CASE
                         WHEN kind='style' AND vantage_id=$3 THEN 0
                         WHEN vantage_id='user_global' THEN 1
                         WHEN vantage_id=$3 THEN 2
                         ELSE 3
                       END ASC,
                     strength DESC NULLS LAST,
                     confidence DESC NULLS LAST,
                     updated_at DESC NULLS LAST
            LIMIT $4
            """,
            vids,
            f"user/{uid}/%",
            vid,
            int(limit),
        )

        out: List[str] = []
        seen_keys: set[str] = set()
        for r in rows:
            kind = str(r["kind"] or "").strip()
            topic_key = str(r["topic_key"] or "").strip()
            if not _should_inject_card_topic(kind, topic_key):
                continue

            dedupe_key = f"{kind}:{topic_key}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            summary = str(r["summary"] or "").strip()
            if not summary:
                continue
            # Keep only the first summary line for prompt compactness.
            first = summary.splitlines()[0].strip()
            if first:
                out.append(first[:240])
        return out
    finally:
        await conn.close()


async def _load_vantage_profile_cards_async(user_id: str, vantage_id: str | None = None, limit: int = 10) -> List[str]:
    uid = str(user_id or "").strip()
    vid = (vantage_id or "").strip() or "default"
    if not uid:
        return []

    dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        return []

    vids = _card_vantage_candidates(vid, include_legacy=True)

    conn = await asyncpg.connect(_norm_dsn(dsn))
    try:
        rows = await conn.fetch(
            """
            SELECT kind, topic_key, summary, strength, confidence, updated_at, vantage_id
            FROM vantage_card.card_head
            WHERE vantage_id = ANY($1::text[])
              AND kind IN ('identity','background','project')
              AND status='active'
              AND COALESCE(payload->>'use_scope', 'CONTENT_OK') <> 'NEVER_SURFACE'
              AND topic_key LIKE $2
              AND COALESCE(summary, '') <> ''
            ORDER BY
                     CASE kind
                       WHEN 'identity' THEN 1
                       WHEN 'background' THEN 2
                       WHEN 'project' THEN 3
                       ELSE 9
                     END ASC,
                       CASE
                         WHEN vantage_id='user_global' THEN 0
                         WHEN vantage_id=$3 THEN 1
                         ELSE 2
                       END ASC,
                     strength DESC NULLS LAST,
                     confidence DESC NULLS LAST,
                     updated_at DESC NULLS LAST
            LIMIT $4
            """,
            vids,
            f"user/{uid}/%",
            vid,
            int(limit),
        )

        out: List[str] = []
        seen_keys: set[str] = set()
        for r in rows:
            kind = str(r["kind"] or "").strip()
            topic_key = str(r["topic_key"] or "").strip()
            if not _should_inject_card_topic(kind, topic_key):
                continue

            dedupe_key = f"{kind}:{topic_key}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            summary = str(r["summary"] or "").strip()
            if not summary:
                continue
            first = summary.splitlines()[0].strip()
            if first:
                out.append(first[:240])
        return out
    finally:
        await conn.close()


def build_vantage_profile_cards_block(user_id: str, vantage_id: str | None = None) -> str:
    """
    Load current Postgres Vantage identity/background/project cards into the live prompt.

    These are adaptive user-profile cards produced from normal user speech.
    """
    try:
        lines = _run_coro_sync(_load_vantage_profile_cards_async(user_id, vantage_id=vantage_id))
    except Exception as e:
        print(f"[persona_loader] Postgres Vantage profile card load failed user_id={user_id} vantage_id={vantage_id}: {e}")
        return ""

    if not lines:
        return ""

    block = ["[VANTAGE PROFILE CARDS]"]
    block.append("Use these as user/Vantage context when relevant. Do not list them unless asked.")
    block.extend(f"- {line}" for line in lines)
    return "\n".join(block)


def build_vantage_preference_cards_block(user_id: str, vantage_id: str | None = None) -> str:
    """
    Load current Postgres Vantage preference/style cards into the live prompt.

    These are produced by the fact/card pipeline and are separate from older
    Qdrant memory_card persona cards.
    """
    try:
        lines = _run_coro_sync(_load_vantage_preference_cards_async(user_id, vantage_id=vantage_id))
    except Exception as e:
        print(f"[persona_loader] Postgres Vantage card load failed user_id={user_id} vantage_id={vantage_id}: {e}")
        return ""

    if not lines:
        return ""

    block = ["[VANTAGE PREFERENCE CARDS]"]
    block.extend(f"- {line}" for line in lines)
    return "\n".join(block)


# ------------------------------------------------------------------------
# BUILD PERSONA BLOCK
# ------------------------------------------------------------------------


def build_user_instructions_block(user_id: str, vantage_id: str | None = None) -> str:
    """
    Returns a formatted global user-instructions block (or "" if none).
    This is used when a caller disables full persona injection but still
    wants the global personalization instructions applied.
    """
    points = _load_persona_points(user_id, vantage_id=vantage_id)
    if not points:
        return ""
    instr_texts = _pick_top_text(points, "user_instructions", max_items=1)
    if not instr_texts:
        return ""
    txt = _format_user_instructions_text(instr_texts[0] or "")
    if not txt:
        return ""
    return "[USER INSTRUCTIONS — GLOBAL]\n" + txt

def build_persona_block(user_id: str, vantage_id: str | None = None) -> str:
    """
    Compose the full persona for this user.
    - Always include the static FM base.
    - Then, if available, add:
      - assistant_identity card (name, pronouns, role, etc.)
      - style cards (how to talk to this user)
      - preference cards (durable user preferences / facts)
    """
    pieces: List[str] = [BASE_PERSONA]
    points = _load_persona_points(user_id, vantage_id=vantage_id)
    if not points:
        return BASE_PERSONA

    # 0) User identity (preferred name)
    identity_user_texts = _pick_top_text(points, "user_identity", max_items=1)
    if identity_user_texts:
        block_lines = ["[User Identity]"]
        block_lines.extend(f"- {t}" for t in identity_user_texts)
        pieces.append("\n".join(block_lines))

    # 1) Assistant identity (we only need the strongest one)
    identity_texts = _pick_top_text(points, "assistant_identity", max_items=1)
    if identity_texts:
        block_lines = ["[Assistant Identity]"]
        block_lines.extend(f"- {t}" for t in identity_texts)
        pieces.append("\n".join(block_lines))

    # 2) User-specific style (take the top few)
    style_texts = _pick_top_text(points, "style", max_items=3)
    if style_texts:
        block_lines = ["[User-Specific Style]"]
        block_lines.extend(f"- {t}" for t in style_texts)
        pieces.append("\n".join(block_lines))

    # 3) Style modes (triggered formats, e.g. "skeleton", "prose")
    mode_texts = _pick_top_text(points, "style_mode", max_items=3)
    if mode_texts:
        block_lines = ["[Style Modes]"]
        block_lines.extend(f"- {t}" for t in mode_texts)
        pieces.append("\n".join(block_lines))

    # 4) User preferences (durable preference/fact cards)
    pref_texts = _pick_top_text(points, "preference", max_items=5)
    if pref_texts:
        block_lines = ["[User Preferences]"]
        block_lines.extend(f"- {t}" for t in pref_texts)
        pieces.append("\n".join(block_lines))


    # 5) User instructions (explicit global instructions from /personalization)
    instr_texts = _pick_top_text(points, "user_instructions", max_items=1)
    if instr_texts:
        txt = _format_user_instructions_text(instr_texts[0] or "")
        if txt:
            pieces.append("[USER INSTRUCTIONS — GLOBAL]\n" + txt)

    persona_block = "\n\n".join(seg.strip() for seg in pieces if seg and seg.strip())
    return persona_block
