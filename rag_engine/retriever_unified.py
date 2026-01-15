# rag_engine/retriever_unified.py
#
# Unified retrieval + personal memory retrieval with tag-sensitive scoring.


from typing import List, Dict, Any
import os
import asyncpg
import asyncio
import json
import time
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI
from .vb_tagging import infer_vb_tags

from .gravity import load_gravity_profile, compute_misalignment

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

# collections we NEVER use as corpus
IGNORED = {"memory_raw"}


def infer_query_tags(text: str) -> list[str]:
    """
    Very simple tag inference from the user query.
    Mirrors the heuristics used in app.py / infer_extra_tags.
    """
    t = (text or "").lower()
    tags: list[str] = []

    # ---------- formatting intent ----------
    if any(w in t for w in ["bullet", "bulleted", "outline", "skeleton", "list"]):
        tags.append("format:skeleton")
    if any(w in t for w in ["paragraph", "prose", "story", "narrative"]):
        tags.append("format:prose")

    # ---------- meta / design language ----------
    if "testing memory" in t or ("shape" in t and "behavior" in t) or "rag" in t:
        tags.append("tone:meta")

    # ---------- topic: workout / gym ----------
    if any(w in t for w in [
        "hammer strength", "hammer plate", "workout", "lifting", "gym routine"
    ]):
        tags.append("topic:workout")

    # ---------- topic: fractal monism ----------
    if any(w in t for w in [
        "fractal monism", "monistic field", "self-deception", "lucifer", "undivided field"
    ]):
        tags.append("topic:fm")

    # ---------- topic: human vantage ----------
    if any(w in t for w in [
        "human vantage", "hv axioms", "human vantage axioms"
    ]):
        tags.append("topic:hv")

    # ---------- intent: explain / why / what is ----------
    if any(w in t for w in [
        "explain", "what is", "why is", "how does", "could you describe"
    ]):
        tags.append("intent:explain")

    # ---------- intent: instruct / how-to ----------
    if any(w in t for w in [
        "how do i", "how can i", "show me how", "step by step", "steps", "instructions"
    ]):
        tags.append("intent:instruct")

    # ---------- intent: summarize / compress ----------
    if "summary" in t or "summarize" in t or "short version" in t:
        tags.append("intent:summarize")

    # ---------- intent: analyze ----------
    if "analyze" in t or "analysis" in t or "break down" in t:
        tags.append("intent:analyze")

    # ---------- intent: compare / contrast ----------
    if "compare" in t or "difference between" in t or "vs." in t:
        tags.append("intent:compare")

    # ---------- intent: reflect / introspective ----------
    if any(w in t for w in [
        "i feel", "why do i", "help me understand", "reflect on",
        "what does it mean for me", "in my life"
    ]):
        tags.append("intent:reflect")

    # ---------- intent: generate / create ----------
    if any(w in t for w in [
        "write", "create", "make a", "generate", "draft", "compose"
    ]):
        tags.append("intent:generate")

    # ---------- intent: rewrite / edit ----------
    if "rewrite" in t or "edit this" in t or "make this better" in t:
        tags.append("intent:rewrite")

    # ---------- intent: evaluate / critique ----------
    if any(w in t for w in [
        "evaluate", "critique", "what do you think of", "rate this"
    ]):
        tags.append("intent:evaluate")

    # ---- VB TAGGING ----
    vb_tags = infer_vb_tags(text)
    for t in vb_tags:
        tags.append(t)

    return tags


def score_personal_hit(hit: Dict[str, Any], query: str) -> float:
    """
    Adjust Qdrant base score for a personal memory hit using:
      - positive / negative feedback
      - format-related tags (skeleton / prose) based on the current query
      - topic and intent tags (including user_tags) when they match the query's tags
    """
    base = float(hit.get("score") or 0.0)
    payload = hit.get("payload") or {}

    # --- feedback-based adjustment ---
    fb = payload.get("feedback") or {}
    pos = int(fb.get("positive_signals") or 0)
    neg = int(fb.get("negative_signals") or 0)

    # Each net positive adds +0.05, each net negative subtracts -0.05 (clamped)
    fb_delta = 0.05 * (pos - neg)
    if fb_delta > 0.5:
        fb_delta = 0.5
    if fb_delta < -0.5:
        fb_delta = -0.5

    score = base + fb_delta

    # --- tag-based adjustments ---
    q = (query or "").lower()

    # query-level tags (format, topics, intents...)
    query_tags = set(infer_query_tags(q))

    # payload tags (from app.log_chat) + user_tags (explicit tagging like "tag this as ...")
    payload_tags = set(str(t) for t in (payload.get("tags") or []))
    user_tags = set(str(t) for t in (payload.get("user_tags") or []))
    all_tags = payload_tags | user_tags

    # 1) format alignment (same as before, but using query_tags)
    if "format:skeleton" in query_tags:
        if "format:skeleton" in all_tags:
            score += 0.15
        elif "format:prose" in all_tags:
            score -= 0.10

    if "format:prose" in query_tags:
        if "format:prose" in all_tags:
            score += 0.15
        elif "format:skeleton" in all_tags:
            score -= 0.10

    # 2) topic alignment (topic:fm, topic:workout, topic:hv, or any future topic:*)
    for tag in query_tags:
        if tag.startswith("topic:") and tag in all_tags:
            score += 0.08  # small positive nudge for topic match

    # 3) intent alignment (intent:explain, intent:instruct, etc.)
    for tag in query_tags:
        if tag.startswith("intent:") and tag in all_tags:
            score += 0.04  # even smaller nudge for matching intent

    return score


def list_collections() -> List[str]:
    """Return all Qdrant collection names except memory_raw."""
    resp = qdrant.get_collections()
    cols = getattr(resp, "collections", [])
    names = []

    for c in cols:
        name = getattr(c, "name", None)
        if not name:
            continue
        if name in IGNORED:
            continue
        names.append(name)

    return names


# --- MAIN CORPUS RETRIEVAL FUNCTION ---

RAG_POLICY_TTL_SECONDS = int(os.getenv("RAG_POLICY_TTL_SECONDS", "15") or "15")
_RAG_POLICY_CACHE: Dict[str, Dict[str, Any]] = {}  # vid -> {"ts": float, "policy": dict}

def _csv_env(name: str) -> List[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

def _dedupe_keep_order(xs: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        x = (x or "").strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

async def _fetch_rag_policy_async(vantage_id: str) -> Dict[str, Any]:
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    if not dsn:
        return {}
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"[unified_retrieve] rag_policy connect error: {e}")
        return {}
    try:
        row = await conn.fetchrow(
            "SELECT policy FROM vantage_identity.rag_policy WHERE vantage_id=$1",
            vantage_id,
        )
        if not row:
            return {}
        pol = row["policy"]
        if pol is None:
            return {}
        if isinstance(pol, str):
            try:
                pol = json.loads(pol)
            except Exception:
                return {}
        return dict(pol) if isinstance(pol, dict) else {}
    except Exception as e:
        print(f"[unified_retrieve] rag_policy fetch error: {e}")
        return {}
    finally:
        try:
            await conn.close()
        except Exception:
            pass

def get_rag_policy(vantage_id: str) -> Dict[str, Any]:
    vid = (vantage_id or "default").strip() or "default"
    ttl = max(0, int(RAG_POLICY_TTL_SECONDS or 0))
    now = time.time()

    if ttl > 0:
        cached = _RAG_POLICY_CACHE.get(vid) or {}
        ts = float(cached.get("ts") or 0.0)
        if ts and (now - ts) <= ttl:
            pol = cached.get("policy") or {}
            return pol if isinstance(pol, dict) else {}

    pol: Dict[str, Any] = {}
    try:
        # unified_retrieve is called from sync FastAPI endpoints running in a threadpool.
        pol = asyncio.run(_fetch_rag_policy_async(vid))
        if not isinstance(pol, dict):
            pol = {}
    except RuntimeError:
        # If called inside an active event loop thread, skip to avoid deadlock.
        pol = {}
    except Exception as e:
        print(f"[unified_retrieve] rag_policy error: {e}")
        pol = {}

    if ttl > 0:
        _RAG_POLICY_CACHE[vid] = {"ts": now, "policy": pol}
    return pol

def _available_corpus_collections() -> set:
    try:
        resp = qdrant.get_collections()
        cols = getattr(resp, "collections", []) or []
        out = set()
        for c in cols:
            name = getattr(c, "name", None)
            if not name:
                continue
            if name in IGNORED:
                continue
            out.add(name)
        return out
    except Exception as e:
        print(f"[unified_retrieve] list_collections error: {e}")
        return set()

def _payload_tags(payload: Dict[str, Any]) -> set:
    tv = (payload or {}).get("tags")
    if isinstance(tv, dict):
        return set(str(k) for k in tv.keys())
    if isinstance(tv, list):
        return set(str(x) for x in tv)
    return set()

def unified_retrieve(
    query: str,
    top_k: int = 5,
    score_threshold: float | None = None,
    vantage_id: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Unified retrieval used by RAG.
    Searches curated primary collections, then fallback collections if needed.
    Per-vantage overrides from vantage_identity.rag_policy:
      - policy.corpus_primary / policy.corpus_fallback
      - policy.topic_overrides["topic:fm"].corpus_primary / corpus_fallback (etc)
    """
    if not client:
        print("ERROR: Missing OPENAI_API_KEY in unified_retrieve")
        return []

    q = (query or "").strip()
    if not q:
        return []

    vid = (vantage_id or "").strip() or "default"

    emb = client.embeddings.create(model=EMBED_MODEL, input=q)
    vec = emb.data[0].embedding

    query_tags = set(infer_query_tags(q))

    base_primary = _csv_env("RAG_CORPUS_PRIMARY")
    base_fallback = _csv_env("RAG_CORPUS_FALLBACK")
    if not base_primary:
        base_primary = sorted(list(_available_corpus_collections()))

    pol = get_rag_policy(vid) or {}

    eff_primary = list(base_primary)
    eff_fallback = list(base_fallback)

    if isinstance(pol.get("corpus_primary"), list):
        eff_primary = [str(x) for x in (pol.get("corpus_primary") or []) if str(x).strip()]
    if isinstance(pol.get("corpus_fallback"), list):
        eff_fallback = [str(x) for x in (pol.get("corpus_fallback") or []) if str(x).strip()]

    override_key = None
    topic_overrides = pol.get("topic_overrides") if isinstance(pol.get("topic_overrides"), dict) else {}
    if isinstance(topic_overrides, dict):
        for t in sorted(query_tags):
            if t.startswith("topic:") and t in topic_overrides:
                override_key = t
                break
        if override_key:
            ov = topic_overrides.get(override_key) or {}
            if isinstance(ov, dict):
                if isinstance(ov.get("corpus_primary"), list):
                    eff_primary = [str(x) for x in (ov.get("corpus_primary") or []) if str(x).strip()]
                if isinstance(ov.get("corpus_fallback"), list):
                    eff_fallback = [str(x) for x in (ov.get("corpus_fallback") or []) if str(x).strip()]

    eff_primary = _dedupe_keep_order([c for c in eff_primary if c and c not in IGNORED])
    eff_fallback = _dedupe_keep_order([c for c in eff_fallback if c and c not in IGNORED and c not in eff_primary])

    available = _available_corpus_collections()
    if available:
        eff_primary = [c for c in eff_primary if c in available]
        eff_fallback = [c for c in eff_fallback if c in available]

    thr_env = os.getenv("RETRIEVE_THRESHOLD")
    thr_default = float(thr_env) if thr_env else 0.30
    thr = float(score_threshold) if score_threshold is not None else thr_default

    print(
        f"[unified_retrieve] vid={vid} override={override_key or '-'} "
        f"primary_n={len(eff_primary)} fallback_n={len(eff_fallback)} "
        f"thr={thr:.3f} top_k={int(top_k)}"
    )

    hits_all: List[Dict[str, Any]] = []

    def search_collection(coll: str, limit: int) -> None:
        nonlocal hits_all
        try:
            col_info = qdrant.get_collection(coll)
            vectors_cfg = getattr(col_info.config.params, "vectors", None)
            if isinstance(vectors_cfg, dict) and vectors_cfg:
                vector_name = next(iter(vectors_cfg.keys()))
                qvec = qmodels.NamedVector(name=vector_name, vector=vec)
            else:
                qvec = vec

            hits = qdrant.search(
                collection_name=coll,
                query_vector=qvec,
                limit=limit,
                with_payload=True,
                score_threshold=thr,
            )
        except Exception as e:
            print(f"[unified_retrieve] search failed coll={coll}: {e}")
            return

        for h in (hits or []):
            payload = h.payload or {}
            base_score = float(h.score)
            payload_tags = _payload_tags(payload)

            tag_bonus = 0.0
            if "format:skeleton" in query_tags:
                if "format:skeleton" in payload_tags:
                    tag_bonus += 0.05
                elif "format:prose" in payload_tags:
                    tag_bonus -= 0.02
            if "format:prose" in query_tags:
                if "format:prose" in payload_tags:
                    tag_bonus += 0.05
                elif "format:skeleton" in payload_tags:
                    tag_bonus -= 0.02
            if "tone:meta" in query_tags and "tone:meta" in payload_tags:
                tag_bonus += 0.05
            for intent_tag in [
                "intent:explain", "intent:instruct", "intent:summarize",
                "intent:analyze", "intent:compare", "intent:reflect",
                "intent:generate", "intent:rewrite", "intent:evaluate"
            ]:
                if intent_tag in query_tags and intent_tag in payload_tags:
                    tag_bonus += 0.05

            hits_all.append({"collection": coll, "id": h.id, "score": base_score + tag_bonus, "payload": payload})

    for coll in eff_primary:
        search_collection(coll, limit=int(top_k))

    if len(hits_all) < int(top_k) and eff_fallback:
        for coll in eff_fallback:
            search_collection(coll, limit=int(top_k))
            if len(hits_all) >= int(top_k):
                break

    hits_all.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return hits_all[: int(top_k)]


# --- PERSONAL / EPISODIC MEMORY RETRIEVAL (memory_raw) ---

def retrieve_personal_memory(
    user_id: str,
    query: str,
    top_k: int = 5,
    score_threshold: float = 0.20,
    vantage_id: str | None = None,
) -> List[Dict[str, Any]]:

    q = (query or "").strip()
    if not q:
        return []

    if not client:
        print("ERROR: Missing OPENAI_API_KEY in retrieve_personal_memory")
        return []

    # 1) Embed query
    emb = client.embeddings.create(model=EMBED_MODEL, input=q)
    vec = emb.data[0].embedding

    # 1b) Infer query tags for this personal-memory search
    query_tags = set(infer_query_tags(q))

    # --- Gravity / escape detector ---
    gravity_weights = load_gravity_profile(user_id) if user_id else {}
    misalignment = 0.0
    if gravity_weights:
        misalignment = compute_misalignment(list(query_tags), gravity_weights)
    print(f"[gravity] user_id={user_id} misalignment={misalignment:.3f} tags={list(query_tags)}")

    # 2) Filter by user_id if provided
    must = []
    if user_id:
        must.append(
            qmodels.FieldCondition(
                key="user_id",
                match=qmodels.MatchValue(value=user_id)
            )
        )

    vid = (vantage_id or "").strip() or "default"

    # Namespace filter:
    # - keep points in the active vid
    # - ALSO keep legacy points with missing payload.vantage_id (older data + many cards)
    use_is_empty = hasattr(qmodels, "IsEmptyCondition") and hasattr(qmodels, "PayloadField")

    should = None
    if use_is_empty:
        should = [
            qmodels.FieldCondition(key="vantage_id", match=qmodels.MatchValue(value=vid)),
            qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="vantage_id")),
        ]
    else:
        # Older qdrant_client: can't express "is_empty" server-side.
        # We'll post-filter payload.vantage_id below.
        should = None

    # Exclude assistant chat + daemon/system cards from episodic retrieval.
    # Keep memory_card INCLUDED (identity/style cards live there).
    must_not = [
        qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value="frontend/chat:assistant")),
        qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value="gravity_daemon")),
        qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value="vb_desire_daemon")),
          qmodels.FieldCondition(key="source", match=qmodels.MatchValue(value="memory_card")),
    ]

    qry_filter = qmodels.Filter(must=must, should=should, must_not=must_not)

    try:
        hits = qdrant.search(
            collection_name="memory_raw",
            query_vector=vec,
            limit=(max(top_k * 8, 40) if use_is_empty else max(top_k * 16, 80)),
            with_payload=True,
            score_threshold=float(score_threshold),
            query_filter=qry_filter,
        )
    except Exception as e:
        print(f"Search failed for memory_raw: {e}")
        return []

    results: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_texts = set()
    q_norm = q.strip().lower()

    # Treat these as instrumentation prompts; do not retrieve them as “memory”.
    PROMPTY_MARKERS = (
        "reply with only",
        "return exactly",
        "echo ",
        "one token",
        "no punctuation",
        "answer in one sentence",
        "debug",
        "preflight_",
        "memtest:",
        "memoryseed:",
        "seedmemory:",
    )

    for h in (hits or []):
        hid = str(h.id)

        # dedupe by id (shouldn't happen, but safe)
        if hid in seen_ids:
            continue
        seen_ids.add(hid)

        base_score = float(h.score)
        payload = h.payload or {}

        txt = (payload.get("text") or "").strip()

        # If the Qdrant client can't express "vantage_id is empty" server-side,
        # enforce namespace here: allow either matching vid OR missing vantage_id.
        if not use_is_empty:
            pv = payload.get("vantage_id", None)
            if not ((pv == vid) or (pv in (None, "") and vid == "default")):
                continue

        # If the *query itself* is a test/probe query, allow test/probe memories through.
        QUERY_TEST_PREFIXES = (
            "say exactly:",
            "return exactly:",
            "reply with only",
            "reply with exactly",
            "echo decision",
            "echo model",
            "echo threadctx",
            "memtest:",
            "memoryseed:",
            "preflight_",
            "preflight:",
        )
        query_is_test = q_norm.startswith(QUERY_TEST_PREFIXES) or any(p in q_norm for p in ("echo model id",))


        # --- drop obvious test/probe prompts from being treated as "memory" ---
        # Keep memory_card items (identity/style/etc) regardless.
        src = (payload.get("source") or "").strip()
        if (not query_is_test) and src != "memory_card":
            t_low = txt.lower()
            TEST_PREFIXES = (
                "return exactly:",
                "reply with only",
                "reply with exactly",
                "echo decision",
                "echo model",
                "echo threadctx",
                "memtest:",
                "memoryseed:",
                "preflight_",
                "preflight:",
            )
            if t_low.startswith(TEST_PREFIXES):
                continue

        txt_norm = txt.lower()

        # drop the just-asked message (it will match top-1 if you log before query)
        if txt_norm == q_norm:
            continue

        # de-dupe identical texts
        if txt_norm and txt_norm in seen_texts:
            continue
        if txt_norm:
            seen_texts.add(txt_norm)

        if src == "frontend/chat:user" and any(m in txt_norm for m in PROMPTY_MARKERS):
            continue

        payload_tags = set(str(t) for t in (payload.get("tags") or []))

        # tag-based nudges
        tag_bonus = 0.0

        # ---------- format alignment ----------
        if "format:skeleton" in query_tags:
            if "format:skeleton" in payload_tags:
                tag_bonus += 0.05
            elif "format:prose" in payload_tags:
                tag_bonus -= 0.02
        if "format:prose" in query_tags:
            if "format:prose" in payload_tags:
                tag_bonus += 0.05
            elif "format:skeleton" in payload_tags:
                tag_bonus -= 0.02

        # ---------- tone alignment ----------
        if "tone:meta" in query_tags and "tone:meta" in payload_tags:
            tag_bonus += 0.05

        # ---------- intent alignment ----------
        intent_tags = [
            "intent:explain","intent:instruct","intent:summarize","intent:analyze",
            "intent:compare","intent:reflect","intent:generate","intent:rewrite","intent:evaluate",
        ]
        for intent_tag in intent_tags:
            if intent_tag in query_tags and intent_tag in payload_tags:
                tag_bonus += 0.05

        # ---------- gravity alignment ----------
        gravity_bonus = 0.0
        if gravity_weights:
            for t in payload_tags:
                w = gravity_weights.get(t)
                if w:
                    gravity_bonus += 0.08 * w

            if misalignment > 0.5:
                gravity_bonus *= 0.3
            elif misalignment > 0.2:
                gravity_bonus *= 0.6

        final_score = base_score + tag_bonus + gravity_bonus

        results.append(
            {
                "collection": "memory_raw",
                "id": h.id,
                "score": final_score,
                "payload": payload,
            }
        )

    # keep strongest first
    results.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    results = results[:top_k]
    return results
