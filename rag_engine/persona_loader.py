# rag_engine/persona_loader.py
#
# Persona loading and lightweight daytime persona consolidation
# for VerbalSage / Brains.
#
# Responsibilities:
# - Define BASE_PERSONA (static Fractal Monism seed).
# - Load persona-related memory cards from Qdrant:
#     * assistant_identity
#     * style
#     * preference
# - Build the system persona block for a given user_id.
# - Provide quick_persona_refresh(user_id) to synthesize/update a
#   minimal style card from recent chat behavior.

from __future__ import annotations

from typing import List, Dict, Any, Optional
import os
import uuid
import datetime

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI


# ------------------------------------------------------------------------
# BASE PERSONA (static Fractal Monism seed)
# ------------------------------------------------------------------------

BASE_PERSONA = """
Respond in a way that is consistent with the user’s past preferences, feedback,
and memory. Do not assume personal details or emotions unless they are stated.
Adapt your style through reinforcement over time.

""".strip()


# ------------------------------------------------------------------------
# CONFIG / CLIENTS (Qdrant + OpenAI)
# ------------------------------------------------------------------------

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

_qdrant_client: Optional[QdrantClient] = None
_openai_client: Optional[OpenAI] = None


def get_qdrant() -> QdrantClient:
    """Return a singleton QdrantClient."""
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(
            url=QDRANT_URL,
            timeout=60,
            prefer_grpc=False,
            https=False,
            check_compatibility=False,
        )
    return _qdrant_client


def get_openai() -> Optional[OpenAI]:
    """Return a singleton OpenAI client, or None if no API key is set."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            return None
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


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
        vid = (vantage_id or "").strip() or "default"

        must = [
            qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
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
    txt = (instr_texts[0] or "").strip()
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
        txt = (instr_texts[0] or "").strip()
        if txt:
            pieces.append("[USER INSTRUCTIONS — GLOBAL]\n" + txt)

    persona_block = "\n\n".join(seg.strip() for seg in pieces if seg and seg.strip())
    return persona_block


# ------------------------------------------------------------------------
# QUICK PERSONA REFRESH (simple daytime consolidation)
# ------------------------------------------------------------------------

def quick_persona_refresh(user_id: str, limit: int = 100) -> Dict[str, Any]:
    """
    Look at last `limit` raw chat messages for this user.
    Extract obvious, surface-level style preferences.
    Update or create a 'style' memory_card.
    """

    qdrant = get_qdrant()
    openai_client = get_openai()
    if openai_client is None:
        return {"status": "no_openai_client"}

    # 1) scroll memory_raw for this user
    try:
        points, _ = qdrant.scroll(
            collection_name="memory_raw",
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="user_id",
                        match=qmodels.MatchValue(value=user_id)
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print(f"[quick_persona_refresh] Qdrant scroll error for user {user_id}: {e}")
        return {"status": "qdrant_error", "detail": str(e)}

    texts = [
        (p.payload.get("text") or "").lower().strip()
        for p in points
        if p.payload and p.payload.get("text")
    ]

    # 2) very simple preference detection
    wants_short = any("too long" in t or "shorter" in t for t in texts)
    hates_bullets = any("no bullet" in t or "no lists" in t for t in texts)
    wants_concrete = any("more concrete" in t for t in texts)
    wants_philosophy = any("more philosophy" in t for t in texts)
    less_philosophy = any("less philosophy" in t for t in texts)

    preference_lines: List[str] = []

    if wants_short:
        preference_lines.append("Prefers short, dense responses.")
    if hates_bullets:
        preference_lines.append("Dislikes bullet points and lists; prefers flowing paragraphs.")
    if wants_concrete:
        preference_lines.append("Prefers concrete examples and applications.")
    if wants_philosophy:
        preference_lines.append("Prefers more philosophical framing.")
    if less_philosophy:
        preference_lines.append("Prefers minimal philosophical framing.")

    if not preference_lines:
        # Self-heal: if the deterministic style card is missing, recreate a baseline style card.
        baseline = "Prefers short, dense responses. Dislikes bullet points and lists; prefers flowing paragraphs. Prefers concrete examples and applications."
        try:
            card_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|style|__singleton__"))
            existing = qdrant.retrieve(collection_name="memory_raw", ids=[card_id], with_payload=False, with_vectors=False)
        except Exception:
            existing = []

        if not existing:
            text_block = baseline
            card = {
                "user_id": user_id,
                "text": text_block,
                "source": "memory_card",
                "tags": ["summary", "card", "style"],
                "kind": "style",
                "base_importance": 0.7,
                "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "topic_key": "__singleton__",
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
            try:
                emb = openai_client.embeddings.create(model=EMBED_MODEL, input=text_block)
                vec = emb.data[0].embedding
                point = qmodels.PointStruct(id=card_id, payload=card, vector=vec)
                qdrant.upsert(collection_name="memory_raw", points=[point])
                return {"status": "recreated_baseline", "card_id": card_id}
            except Exception as e:
                print(f"[quick_persona_refresh] baseline recreate failed for user {user_id}: {e}")
                return {"status": "no_changes"}
        return {"status": "no_changes"}
    # 3) Build/update the style card payload
    text_block = " ".join(preference_lines)

    card = {
        "user_id": user_id,
        "text": text_block,
        "source": "memory_card",
        "tags": ["summary", "card", "style"],
        "kind": "style",
        "base_importance": 0.7,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "topic_key": "__singleton__",
    }

    # 4) Embed it and upsert (deterministic id so we overwrite instead of duplicating)
    try:
        emb = openai_client.embeddings.create(model=EMBED_MODEL, input=text_block)
        vec = emb.data[0].embedding
    except Exception as e:
        print(f"[quick_persona_refresh] OpenAI embedding error for user {user_id}: {e}")
        return {"status": "openai_error", "detail": str(e)}

    # deterministic UUID based on user_id|style|__singleton__
    card_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|style|__singleton__"))

    point = qmodels.PointStruct(
        id=card_id,
        payload=card,
        vector=vec,
    )

    try:
        qdrant.upsert(collection_name="memory_raw", points=[point])
    except Exception as e:
        print(f"[quick_persona_refresh] Qdrant upsert error for user {user_id}: {e}")
        return {"status": "qdrant_upsert_error", "detail": str(e)}

    return {"status": "updated", "card": card}
