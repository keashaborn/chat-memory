# rag_engine/vb_desire_profile.py

from typing import Dict, List, Any, Tuple
from datetime import datetime
import uuid

from qdrant_client.http import models as qmodels

# Reuse qdrant + embedding client from gravity module to avoid circular imports
from .gravity import qdrant, client, EMBED_MODEL

def _inc_bucket(buckets: Dict[str, Dict[str, float]], key: str, pos: int, neg: int):
    """
    buckets[key] = {"count":..., "pos":..., "neg":...}
    """
    if key not in buckets:
        buckets[key] = {"count": 0.0, "pos": 0.0, "neg": 0.0}
    buckets[key]["count"] += 1.0
    buckets[key]["pos"] += float(pos)
    buckets[key]["neg"] += float(neg)


def _score_bucket(count: float, pos: float, neg: float) -> float:
    """
    Smoothed score in [-1, +1].
    Positive means reinforced, negative means punished.
    """
    # Smooth a bit so small samples aren't extreme
    # (pos - neg) / (count + 2)
    return (pos - neg) / max(2.0, count + 2.0)


def _top_n(buckets: Dict[str, Dict[str, float]], n: int = 5) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for k, v in buckets.items():
        score = _score_bucket(v["count"], v["pos"], v["neg"])
        rows.append({
            "key": k,
            "count": int(v["count"]),
            "positive_feedback": int(v["pos"]),
            "negative_feedback": int(v["neg"]),
            "score": round(score, 4),
        })
    rows.sort(key=lambda r: (r["score"], r["count"]), reverse=True)
    return rows[:n]


def _infer_preferences(intent_rows: List[Dict[str, Any]],
                       format_rows: List[Dict[str, Any]],
                       topic_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Very simple v1 inference based on top scored buckets.
    We’ll refine later.
    """
    preferred_format_default = "unspecified"
    if format_rows:
        # format rows keys look like "format:skeleton" or "format:prose"
        best = format_rows[0]["key"]
        preferred_format_default = best.split(":", 1)[1] if ":" in best else best

    # crude length/density inference from intents
    preferred_answer_length = "unspecified"
    preferred_density = "unspecified"
    avoidances: List[str] = []

    # If summarize is high score, usually prefers short
    for r in intent_rows:
        if r["key"] == "intent:summarize" and r["score"] > 0:
            preferred_answer_length = "short"

    # If analyze is high score, often prefers dense
    for r in intent_rows:
        if r["key"] == "intent:analyze" and r["score"] > 0:
            preferred_density = "high"

    # Avoidances: intents with negative scores
    for r in intent_rows:
        if r["score"] < -0.1:
            avoidances.append(r["key"])

    # Format overrides by topic (very conservative v1)
    overrides: Dict[str, str] = {}
    # Example: if topic:workout exists and format:skeleton is best, set override
    if topic_rows:
        for t in topic_rows:
            if t["key"] == "topic:workout" and preferred_format_default == "skeleton":
                overrides["workout"] = "skeleton"

    return {
        "preferred_answer_length": preferred_answer_length,
        "preferred_density": preferred_density,
        "preferred_format_default": preferred_format_default,
        "preferred_format_overrides": overrides,
        "avoidances": avoidances[:5],
    }


def build_vb_desire_profile(user_id: str, limit: int = 5000) -> Dict[str, Any]:
    """
    Build a vb_desire_profile card from memory_raw for a user_id.
    Uses tags + feedback in payloads.
    """
    # Pull a slice of memory_raw for user
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

    intent_buckets: Dict[str, Dict[str, float]] = {}
    format_buckets: Dict[str, Dict[str, float]] = {}
    topic_buckets: Dict[str, Dict[str, float]] = {}

    total_utterances = 0
    total_feedback_events = 0

    for p in points or []:
        payload = p.payload or {}
        tags = payload.get("tags") or []
        fb = payload.get("feedback") or {}

        pos = int(fb.get("positive_signals") or 0)
        neg = int(fb.get("negative_signals") or 0)
        if pos or neg:
            total_feedback_events += (pos + neg)

        total_utterances += 1

        for t in tags:
            if not isinstance(t, str):
                continue

            if t.startswith("intent:"):
                _inc_bucket(intent_buckets, t, pos, neg)
            elif t.startswith("format:"):
                _inc_bucket(format_buckets, t, pos, neg)
            elif t.startswith("topic:"):
                _inc_bucket(topic_buckets, t, pos, neg)

    intents_top = _top_n(intent_buckets, n=5)
    formats_top = _top_n(format_buckets, n=5)
    topics_top = _top_n(topic_buckets, n=5)

    inferred = _infer_preferences(intents_top, formats_top, topics_top)

    now = datetime.utcnow().isoformat() + "Z"
    card = {
        "kind": "vb_desire_profile",
          "topic_key": "__singleton__",
        "user_id": user_id,
        "tags": ["card", "vb_profile", "desire"],
        "source_stats": {
            "total_utterances": total_utterances,
            "total_feedback_events": total_feedback_events,
            "sample_limit": limit,
        },
        "request_patterns": {
            "by_intent": intents_top,
            "by_format": formats_top,
            "by_topic": topics_top,
        },
        "inferred_preferences": inferred,
        "created_at": now,
        "updated_at": now,
        "source": "vb_desire_daemon",
        "text": f"VB desire profile for {user_id}",
    }
    return card


def _dedupe_vb_desire_profile(user_id: str, keep_id: str, limit: int = 256) -> int:
    """
    Delete duplicate vb_desire_profile points for this user, keeping keep_id.
    Returns number deleted.
    """
    try:
        points, _ = qdrant.scroll(
            collection_name="memory_raw",
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
                    qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value="vb_desire_profile")),
                ]
            ),
            limit=int(limit),
            with_payload=False,
            with_vectors=False,
        )
    except Exception as e:
        print("vb_desire_profile: dedupe scroll error:", e)
        return 0

    pts = points or []
    legacy_ids = [str(p.id) for p in pts if str(p.id) != str(keep_id)]
    if not legacy_ids:
        return 0

    try:
        qdrant.delete(
            collection_name="memory_raw",
            points_selector=qmodels.PointIdsList(points=legacy_ids),
        )
        return len(legacy_ids)
    except Exception as e:
        print("vb_desire_profile: dedupe delete error:", e)
        return 0


def write_vb_desire_profile_card(user_id: str, card: Dict[str, Any]):
    """
    Upsert vb_desire_profile singleton into memory_raw and dedupe legacy duplicates.

    Deterministic id:
      uuid5(NAMESPACE_DNS, f"{user_id}|vb_desire_profile|__singleton__")

    Preserves created_at if keep_id already exists.
    """
    if not client:
        print("vb_desire_profile: no OPENAI_API_KEY, cannot embed card text")
        return

    now = datetime.utcnow().isoformat() + "Z"
    keep_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|vb_desire_profile|__singleton__"))

    # preserve created_at
    created_at = now
    try:
        existing = qdrant.retrieve(
            collection_name="memory_raw",
            ids=[keep_id],
            with_payload=True,
            with_vectors=False,
        )
        old = (existing[0].payload or {}) if existing else {}
        created_at = str(old.get("created_at") or created_at)
    except Exception:
        pass

    payload = dict(card or {})
    payload["kind"] = "vb_desire_profile"
    payload["topic_key"] = "__singleton__"
    payload["user_id"] = user_id
    payload["created_at"] = created_at
    payload["updated_at"] = now
    payload.setdefault("source", "vb_desire_daemon")
    payload.setdefault("text", f"VB desire profile for {user_id}")

    emb = client.embeddings.create(model=EMBED_MODEL, input=(payload.get("text") or "vb_desire_profile"))
    vec = emb.data[0].embedding

    qdrant.upsert(
        collection_name="memory_raw",
        points=[qmodels.PointStruct(id=keep_id, vector=vec, payload=payload)],
    )

    deleted = _dedupe_vb_desire_profile(user_id=user_id, keep_id=keep_id, limit=256)
    if deleted:
        print(f"vb_desire_profile: dedupe deleted={deleted} user_id={user_id}")
def load_latest_vb_desire_profile(user_id: str) -> Dict[str, Any] | None:
    """
    Load the vb_desire_profile for user_id.

    Priority:
      1) deterministic singleton id (keep_id) via retrieve()
      2) fallback: choose most recent by updated_at/created_at among duplicates (no deletes here)
    """
    keep_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|vb_desire_profile|__singleton__"))

    # 1) deterministic singleton fast-path
    try:
        got = qdrant.retrieve(
            collection_name="memory_raw",
            ids=[keep_id],
            with_payload=True,
            with_vectors=False,
        )
        if got:
            return got[0].payload
    except Exception:
        pass

    # 2) legacy fallback
    try:
        points, _ = qdrant.scroll(
            collection_name="memory_raw",
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
                    qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value="vb_desire_profile")),
                ]
            ),
            limit=256,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return None

    if not points:
        return None

    def _ts(p) -> str:
        pl = p.payload or {}
        return str(pl.get("updated_at") or pl.get("created_at") or "")

    points = sorted(points, key=_ts)
    return points[-1].payload
def vb_desire_bias_map(card: Dict[str, Any]) -> Dict[str, float]:
    bias: Dict[str, float] = {}
    rp = (card.get("request_patterns") or {})
    rows = (rp.get("by_intent") or []) + (rp.get("by_format") or []) + (rp.get("by_topic") or [])

    for r in rows:
        k = r.get("key")
        if not k:
            continue
        s = float(r.get("score") or 0.0)
        if s > 1.0: s = 1.0
        if s < -1.0: s = -1.0

        # small nudges, keep safe
        if k.startswith("format:"):
            bias[k] = bias.get(k, 0.0) + 0.12 * s
        elif k.startswith("topic:"):
            bias[k] = bias.get(k, 0.0) + 0.10 * s
        elif k.startswith("intent:"):
            bias[k] = bias.get(k, 0.0) + 0.06 * s

    # clamp
    for k in list(bias.keys()):
        if bias[k] > 0.25: bias[k] = 0.25
        if bias[k] < -0.25: bias[k] = -0.25

    return bias
