from typing import Dict, List
from datetime import datetime
import os
import uuid

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI
import uuid

# --- ENV / CONFIG ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

try:
    qdrant = QdrantClient(
        url=QDRANT_URL,
        timeout=60,
        prefer_grpc=False,
        https=False,
        check_compatibility=False,
    )
except TypeError:
    qdrant = QdrantClient(
        url=QDRANT_URL,
        timeout=60,
        prefer_grpc=False,
        https=False,
    )


def load_user_memories(user_id: str) -> List[dict]:
    """
    Retrieve all memory_raw entries for the user from Qdrant.
    """
    try:
        results = qdrant.scroll(
            collection_name="memory_raw",
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="user_id",
                        match=qmodels.MatchValue(value=user_id)
                    )
                ]
            ),
            limit=20000,   # more than enough
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print("gravity: load_user_memories error:", e)
        return []

    points = results[0] or []
    memories = [p.payload for p in points if p.payload]
    return memories

def load_gravity_profile(user_id: str) -> Dict[str, float]:
    """
    Load the gravity_profile singleton for this user from memory_raw.

    Deterministic id:
      uuid5(NAMESPACE_DNS, f"{user_id}|gravity_profile|__singleton__")

    Returns a dict[tag -> weight]. If none found, returns {}.
    """
    rec_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|gravity_profile|__singleton__"))
    try:
        res = qdrant.retrieve(
            collection_name="memory_raw",
            ids=[rec_id],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print("gravity: load_gravity_profile error:", e)
        return {}

    if not res:
        return {}

    payload = (res[0].payload or {})
    weights = payload.get("weights") or {}
    if not isinstance(weights, dict):
        return {}
    return weights


def extract_style_mode_signals(memories: List[dict]) -> Dict[str, float]:
    """
    Extract long-term style preferences from style_mode cards.
    Produces tag-weight pairs like:
      "format:prose": 0.6
      "intent:analyze": 0.4
    """
    weights: Dict[str, float] = {}

    for mem in memories:
        if mem.get("kind") != "style_mode":
            continue

        tags = mem.get("tags") or []
        for t in tags:
            # give a strong identity-level weight
            weights[t] = weights.get(t, 0.0) + 0.6

    return weights


def extract_preference_signals(memories: List[dict]) -> Dict[str, float]:
    """
    Extract stable user preferences stored as memory cards.
    These include 'preference' and 'format/tone/intent' based cards.
    """
    weights: Dict[str, float] = {}

    for mem in memories:
        if mem.get("kind") not in ("user_preference", "assistant_identity", "preference"):
            continue

        for t in mem.get("tags") or []:
            weights[t] = weights.get(t, 0.0) + 0.4  # medium-strong

    return weights


def extract_longterm_vb_signals(memories: List[dict]) -> Dict[str, float]:
    """
    Extract long-term VB ontology/stance patterns.
    Ontology (conceptual style) is a strong identity indicator.
    Stance (hedged/assertive) is medium strength.
    """
    weights: Dict[str, float] = {}
    counts: Dict[str, int] = {}

    for mem in memories:
        tags = mem.get("tags") or []

        for t in tags:
            if t.startswith("vb_ontology:"):
                counts[t] = counts.get(t, 0) + 1
            if t.startswith("vb_stance:"):
                counts[t] = counts.get(t, 0) + 1

    # normalize lightly and apply identity-level weights
    for t, c in counts.items():
        if t.startswith("vb_ontology:"):
            weights[t] = min(0.5, 0.1 * c)    # strong identity
        elif t.startswith("vb_stance:"):
            weights[t] = min(0.3, 0.05 * c)   # medium identity

    return weights


def extract_longterm_tag_frequencies(memories: List[dict]) -> Dict[str, float]:
    """
    Extract long-term tag frequency patterns from user messages.
    These have weak influence on identity.
    """
    counts: Dict[str, int] = {}
    total = 0

    for mem in memories:
        for t in mem.get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
            total += 1

    weights: Dict[str, float] = {}
    if total == 0:
        return weights

    # scale frequencies lightly
    for t, c in counts.items():
        freq = c / total  # between 0 and 1
        weights[t] = freq * 0.2  # small identity influence

    return weights


def extract_reinforced_patterns(memories: List[dict]) -> Dict[str, float]:
    """
    Compute tag-level reinforcement weights from:
      - positive/negative feedback counts
      - repeated vb_desire patterns
    Produces weights in roughly the range [-0.3, +0.3].
    """
    weights: Dict[str, float] = {}

    for mem in memories:
        tags = mem.get("tags") or []
        fb = mem.get("feedback") or {}

        # 1) Feedback signals
        pos = int(fb.get("positive_signals") or 0)
        neg = int(fb.get("negative_signals") or 0)

        if pos or neg:
            for t in tags:
                delta = 0.05 * (pos - neg)
                weights[t] = weights.get(t, 0.0) + delta

        # 2) VB desire patterns
        for t in tags:
            if t.startswith("vb_desire:"):
                weights[t] = weights.get(t, 0.0) + 0.08

    # Clamp range
    for t in weights:
        if weights[t] > 0.3:
            weights[t] = 0.3
        elif weights[t] < -0.3:
            weights[t] = -0.3

    return weights


def extract_statistical_behavior(memories: List[dict]) -> Dict[str, float]:
    """
    Extract short-term behavioral signals from recent memories.
    We use:
      - recent tag frequencies (last 200 memories)
      - short-term stance/ontology/relation shifts
    Contributes lightly to gravity (±0.15 max).
    """
    if not memories:
        return {}

    # 1. Restrict to recent 200 messages (or as many as exist)
    recent = memories[-200:] if len(memories) > 200 else memories

    counts: Dict[str, int] = {}
    total = 0

    # 2. Count tags in the recent window
    for mem in recent:
        tags = mem.get("tags") or []
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
            total += 1

    if total == 0:
        return {}

    # 3. Scale to a maximum of ±0.15
    weights: Dict[str, float] = {}
    for t, c in counts.items():
        freq = c / total   # normalized frequency
        weight = freq * 0.15
        weights[t] = weight

    return weights


def compute_gravity(user_id: str) -> Dict[str, float]:
    """
    Build a gravity vector for the given user_id.

    This function will:
      1. Load user memories
      2. Load style_mode & preference cards
      3. Compute identity core (55%)
      4. Compute reinforced patterns (30%)
      5. Compute statistical behavior (15%)
      6. Merge them into a single flat tag->weight dict
    """

    # 1. Load all memories
    memories = load_user_memories(user_id)

    # 2. IDENTITY CORE (55%)
    identity_core: Dict[str, float] = {}
    identity_core.update(extract_style_mode_signals(memories))
    identity_core.update(extract_preference_signals(memories))
    identity_core.update(extract_longterm_vb_signals(memories))
    identity_core.update(extract_longterm_tag_frequencies(memories))

    # 3. Reinforced patterns (30%)
    reinforced = extract_reinforced_patterns(memories)

    # 4. Statistical behavior (15%)
    stat_behavior = extract_statistical_behavior(memories)

    # 5. Weighted merge
    gravity: Dict[str, float] = {}

    for tag_dict, factor in [
        (identity_core, 0.55),
        (reinforced,    0.30),
        (stat_behavior, 0.15),
    ]:
        for tag, value in tag_dict.items():
            gravity[tag] = gravity.get(tag, 0.0) + value * factor

    # 6. Clamp to [-1.0, 1.0]
    for tag in gravity:
        if gravity[tag] > 1.0:
            gravity[tag] = 1.0
        elif gravity[tag] < -1.0:
            gravity[tag] = -1.0

    return gravity

def compute_misalignment(query_tags: List[str], gravity_weights: Dict[str, float]) -> float:
    """
    Compute a simple misalignment score between the query tags and gravity.

    0.0  → perfectly aligned or no overlap
    ~0.2 → mild shift
    ~0.5 → strong override
    ~0.8+ → sharp mismatch

    For now, we define misalignment as:
      fraction of overlapping tags whose gravity weight is <= 0
      plus a small penalty if there is no overlap at all.
    """
    if not gravity_weights or not query_tags:
        return 0.0

    overlap = [t for t in query_tags if t in gravity_weights]
    if not overlap:
        # No overlap with gravity at all → mild misalignment
        return 0.3

    misaligned = [t for t in overlap if gravity_weights.get(t, 0.0) <= 0.0]
    frac = len(misaligned) / len(overlap)

    # Small floor so we distinguish "perfect align" from "some tension"
    return min(1.0, max(0.0, frac))

def _dedupe_gravity_profile(user_id: str, keep_id: str, limit: int = 256) -> int:
    """
    Delete any gravity_profile points for user_id except keep_id.
    Returns number of deleted points.
    """
    deleted = 0
    try:
        offset = None
        while True:
            pts, offset = qdrant.scroll(
                collection_name="memory_raw",
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
                        qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value="gravity_profile")),
                    ]
                ),
                limit=int(limit),
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids = [str(p.id) for p in (pts or [])]
            legacy = [i for i in ids if i != keep_id]
            if legacy:
                qdrant.delete(
                    collection_name="memory_raw",
                    points_selector=qmodels.PointIdsList(points=legacy),
                )
                deleted += len(legacy)
            if not offset:
                break
    except Exception as e:
        print("gravity: _dedupe_gravity_profile error:", e)
    return deleted


def write_gravity_card(user_id: str, gravity: Dict[str, float]):
    """
    Writes or updates the gravity_profile singleton in memory_raw, then deletes any duplicates.
    """
    if not client:
        print("gravity: no OPENAI_API_KEY, cannot embed gravity card")
        return

    now = datetime.utcnow().isoformat() + "Z"
    rec_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|gravity_profile|__singleton__"))

    # Preserve created_at if the singleton already exists.
    created = now
    try:
        existing = qdrant.retrieve(
            collection_name="memory_raw",
            ids=[rec_id],
            with_payload=True,
            with_vectors=False,
        )
        if existing:
            old = (existing[0].payload or {})
            created = old.get("created_at") or created
    except Exception as e:
        print("gravity: write_gravity_card retrieve existing error:", e)

    payload = {
        "kind": "gravity_profile",
        "topic_key": "__singleton__",
        "user_id": user_id,
        "weights": gravity,
        "tags": ["gravity", "system"],
        "base_importance": 1.0,
        "created_at": created,
        "updated_at": now,
        "source": "gravity_daemon",
        "text": f"Gravity profile for {user_id}",
    }

    emb = client.embeddings.create(model=EMBED_MODEL, input=payload["text"])
    vec = emb.data[0].embedding

    point = qmodels.PointStruct(
        id=rec_id,
        vector=vec,
        payload=payload,
    )

    qdrant.upsert(
        collection_name="memory_raw",
        points=[point],
    )

    deleted = _dedupe_gravity_profile(user_id, rec_id, limit=256)
    if deleted:
        print(f"gravity: dedupe gravity_profile user_id={user_id} deleted={deleted}")


