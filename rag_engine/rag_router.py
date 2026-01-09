
import os
import asyncpg
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
from typing import List, Dict, Any, Tuple
import re




def is_identity_or_policy_query(message: str) -> bool:
    """
    True for questions about Verbal Sage's response policy / identity cards.
    Those are already loaded deterministically via build_persona_block(),
    so retrieval should be bypassed to avoid irrelevant corpus injection.
    Keep this intentionally narrow (do NOT catch "my writing style").
    """
    m = (message or "").strip().lower()
    if not m:
        return False

    phrases = [
        "preferred response style",
        "style modes",
        "interaction contract",
        "infra roles",
        "project mission",
        "our project mission",
        "our mission",
        "user preferences",
        "assistant identity",
        "user identity",
        "what is my name",
        "what's my name",
        "who am i",
        "what is your name",
        "what's your name",
        "who are you",
    ]
    if any(p in m for p in phrases):
        return True

    # tight variants
    if re.search(r"what(?:'s| is) my preferred response style", m):
        return True
    if re.search(r"what(?:'s| is) (our|the) project mission", m):
        return True

    return False

# ---------- identity canonicalization (alias -> canonical) ----------
PG_DSN = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")

async def _resolve_canonical_user_id_async(vantage_id: str, alias_user_id: str) -> tuple[str, str]:
    """
    Returns (canonical_user_id, alias_user_id). Falls back to alias if lookup fails.
    Source of truth: Postgres table vantage_identity.user_alias.
    """
    vid = (vantage_id or "default").strip() or "default"
    alias = (alias_user_id or "").strip() or "anon"
    canon = alias

    try:
        conn = await asyncpg.connect(PG_DSN)
        try:
            row = await conn.fetchrow(
                "select canonical_user_id from vantage_identity.user_alias where vantage_id=$1 and alias_user_id=$2",
                vid, alias
            )
        finally:
            await conn.close()

        if row and row["canonical_user_id"]:
            canon = str(row["canonical_user_id"])
    except Exception as e:
        print(f"[identity] user_alias lookup failed vid={vid} alias={alias}: {e}")

    return canon, alias

def resolve_canonical_user_id(vantage_id: str, alias_user_id: str) -> tuple[str, str]:
    """
    Sync wrapper for sync FastAPI routes (threadpool).
    """
    try:
        return asyncio.run(_resolve_canonical_user_id_async(vantage_id, alias_user_id))
    except RuntimeError:
        # If a loop is already running in this thread, create a dedicated loop.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_resolve_canonical_user_id_async(vantage_id, alias_user_id))

from .retriever_unified import (
    unified_retrieve,
    retrieve_personal_memory,
    infer_query_tags,
)
from .prompt_builder import build_system_prompt
from .role_overlay import overlay_to_instructions
from .openai_client import complete_chat
from .persona_loader import quick_persona_refresh, get_openai
from .gravity import load_gravity_profile, compute_misalignment
from .temporal_policy import should_add_reentry_line, build_reentry_line
from .vb_desire_profile import load_latest_vb_desire_profile, vb_desire_bias_map

def is_pure_reentry_greeting(message: str) -> bool:
    """
    True when the message is basically a greeting / re-entry with no real task.
    Used to bypass retrieval so we don't inject content on simple "hi I'm back" messages.
    """
    msg = (message or "").strip().lower()
    if not msg:
        return False

    # short greeting-like messages only
    if len(msg) > 40:
        return False

    # Must start with a greeting token (avoid substring traps like 'yo' in 'you', 'hi' in 'this')
    if re.match(r"^(hey|hi|hello|yo)\b", msg):
        pass
    elif msg.startswith("i'm back") or msg.startswith("im back") or msg.startswith("back again"):
        pass
    else:
        return False

    # If it contains clear request markers, it's not "pure greeting"
    request_markers = [
        "give me", "show me", "help me", "explain", "how do", "steps",
        "outline", "bulleted", "write", "generate", "tell me"
    ]
    if any(m in msg for m in request_markers):
        return False

    return True


def score_personal_hit(hit: Dict[str, Any], message: str, bias_map: Dict[str, float] | None = None) -> float:
    """
    Compute an adjusted score for a personal memory hit based on:
    - original vector score
    - feedback (positive/negative signals)
    - simple tag matching (e.g. format:skeleton when user mentions bullets/outline)
    """
    base = float(hit.get("score") or 0.0)
    payload = hit.get("payload") or {}
    fb = payload.get("feedback") or {}
    tags = payload.get("tags") or []
    user_tags = payload.get("user_tags") or []

    pos = int(fb.get("positive_signals") or 0)
    neg = int(fb.get("negative_signals") or 0)
    net = pos - neg

    # Feedback bonus: each net positive adds a bit, each net negative subtracts
    fb_bonus = 0.1 * net
    if fb_bonus > 0.5:
        fb_bonus = 0.5
    if fb_bonus < -0.5:
        fb_bonus = -0.5

    # Very simple format matching
    msg = (message or "").lower()
    tag_bonus = 0.0

    # If user is talking about bullets/outline, prefer format:skeleton
    wants_skeleton = any(w in msg for w in ["bullet", "bulleted", "outline", "skeleton"])
    if wants_skeleton and ("format:skeleton" in tags or "format:skeleton" in user_tags):
        tag_bonus += 0.3

    # vb_desire_profile bias (tag-level)
    vb_bias = 0.0
    if bias_map:
        all_tags = set(tags) | set(user_tags)
        for t in all_tags:
            vb_bias += float(bias_map.get(t) or 0.0)

    return base + fb_bonus + tag_bonus + vb_bias


def extract_tag_from_message(text: str) -> str | None:
    """
    Look for phrases like 'tag this as ...' in the user's feedback message.
    Returns a slug like 'fm_expansion' or None if no tag is found.
    """
    if not text:
        return None

    lowered = text.strip().lower()

    # Match "tag this as <something>"
    m = re.search(r"\btag this(?: as)?\s+(.+)", lowered)
    if not m:
        return None

    raw = m.group(1).strip()

    # Stop at first sentence break if present
    raw = re.split(r"[.!?,]", raw, 1)[0].strip()

    # Turn into a simple slug: letters/numbers -> keep, everything else -> underscore
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not slug:
        return None

    # e.g. "fractal monism expansion" -> "fractal_monism_expansion"
    return slug


router = APIRouter()

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)

def _rag_key(user_id: str, thread_id: str | None) -> Tuple[str, str]:
    uid = (user_id or "").strip() or "anon"
    tid = (thread_id or "").strip()
    if tid and not _UUID_RE.match(tid):
        tid = ""
    return (uid, tid)

# last answer + last-used personal memory ids, keyed by (user_id, thread_id)
_last_rag_result: Dict[Tuple[str, str], Dict[str, Any]] = {}
class RAGQuery(BaseModel):
    user_id: str
    message: str
    thread_id: str | None = None
    top_k: int = 5
    overlay: Dict[str, Any] | None = None


class RAGResponse(BaseModel):
    answer: str
    memory_used: List[Dict[str, Any]]
    system_prompt: str
    meta_explanation: Dict[str, Any] | None = None

def build_meta_explanation(user_id: str, message: str, memory_chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a small, human-readable explanation of why this answer
    looks the way it does: which tags/feedback/topics shaped it,
    plus a simple consistency check between current request and
    historical patterns.
    """
    q = (message or "").strip()
    query_tags = set(infer_query_tags(q))

    # --- gather feedback summary & topic tags on all used memories ---
    total_pos = 0
    total_neg = 0
    topic_tags: set[str] = set()

    for m in memory_chunks:
        payload = m.get("payload") or {}

        # feedback counts
        fb = payload.get("feedback") or {}
        total_pos += int(fb.get("positive_signals") or 0)
        total_neg += int(fb.get("negative_signals") or 0)

        # topic:xxx tags
        for t in payload.get("tags") or []:
            if isinstance(t, str) and t.startswith("topic:"):
                topic_tags.add(t.split(":", 1)[1])

    # --- format reasoning line (current request) ---
    fmt_bits: list[str] = []
    if "format:skeleton" in query_tags:
        fmt_bits.append("user explicitly asked for skeleton / outline style")
    if "format:prose" in query_tags:
        fmt_bits.append("user explicitly asked for narrative / prose style")

    feedback_bits: list[str] = []
    if total_pos or total_neg:
        feedback_bits.append(f"related memories have feedback +{total_pos} / -{total_neg}")

    topic_bits: list[str] = []
    if topic_tags:
        topic_bits.append("topics seen in used memories: " + ", ".join(sorted(topic_tags)))

    summary_parts = []
    if fmt_bits:
        summary_parts.append("format: " + "; ".join(fmt_bits))
    if feedback_bits:
        summary_parts.append("; ".join(feedback_bits))
    if topic_bits:
        summary_parts.append("; ".join(topic_bits))

    summary = " ".join(summary_parts) if summary_parts else ""

    # --- consistency analysis (historical vs current) ---
    # look only at personal memories (memory_raw)
    personal = [m for m in memory_chunks if m.get("collection") == "memory_raw"]
    fmt_counts = {"format:skeleton": 0, "format:prose": 0}

    for m in personal:
        tags = (m.get("payload") or {}).get("tags") or []
        for t in tags:
            if t in fmt_counts:
                fmt_counts[t] += 1

    if fmt_counts["format:skeleton"] > fmt_counts["format:prose"]:
        historical_fmt = "skeleton-leaning"
    elif fmt_counts["format:prose"] > fmt_counts["format:skeleton"]:
        historical_fmt = "prose-leaning"
    else:
        historical_fmt = "undetermined"

    if "format:skeleton" in query_tags:
        current_fmt = "skeleton"
    elif "format:prose" in query_tags:
        current_fmt = "prose"
    else:
        current_fmt = "unspecified"

    if current_fmt == "skeleton" and historical_fmt == "prose-leaning":
        format_shift = "user_now_requesting_skeleton_vs_historical_prose"
    elif current_fmt == "prose" and historical_fmt == "skeleton-leaning":
        format_shift = "user_now_requesting_prose_vs_historical_skeleton"
    else:
        format_shift = "aligned_or_unknown"

    consistency = {
        "historical_format": historical_fmt,
        "current_request_format": current_fmt,
        "format_shift": format_shift,
    }

    # --- gravity misalignment ---
    gravity_weights = load_gravity_profile(user_id) if user_id else {}
    misalignment = 0.0
    misalignment_label = "no_gravity"

    if gravity_weights:
        misalignment = compute_misalignment(sorted(query_tags), gravity_weights)
        if misalignment < 0.15:
            misalignment_label = "aligned"
        elif misalignment < 0.40:
            misalignment_label = "mild_escape"
        elif misalignment < 0.70:
            misalignment_label = "strong_escape"
        else:
            misalignment_label = "disconnected"

    # --- temporal: time since last user message ---
    temporal = {
        "seconds_since_last_user_message": None,
        "bucket": "unknown",
    }
    try:
        r = requests.get(f"http://127.0.0.1:8088/temporal/{user_id}", timeout=1.0)
        if r.ok:
            tj = r.json() or {}
            temporal["seconds_since_last_user_message"] = tj.get("seconds_since_last_user_message")
            temporal["bucket"] = tj.get("bucket") or "unknown"
    except Exception as e:
        print(f"[temporal] error fetching temporal info: {e}")

    return {
        "query_tags": sorted(query_tags),
        "feedback_summary": {"positive": total_pos, "negative": total_neg},
        "topic_tags": sorted(topic_tags),
        "summary": summary,
        "consistency": consistency,
        "gravity": {
            "misalignment": misalignment,
            "label": misalignment_label,
        },
        "temporal": temporal,
    }


@router.post("/query", response_model=RAGResponse)
def rag_query(payload: RAGQuery, vantage_id: str = "default"):
    """
    Main RAG endpoint.
    Uses unified corpus retrieval + per-user episodic memory.
    """
    try:
        overlay_text = overlay_to_instructions(payload.overlay) if payload.overlay else ""

        # Canonicalize user identity (alias -> canonical) before any cache keys or card loads
        alias_uid = payload.user_id
        canon_uid, _ = resolve_canonical_user_id(vantage_id, alias_uid)
        if canon_uid != (alias_uid or ""):
            print(f"[identity] rag_query canonicalized user_id alias={alias_uid} canon={canon_uid} vid={vantage_id}")
        payload.user_id = canon_uid
        # --- [policy] bypass retrieval for identity/style/preferences/mission queries ---
        # Persona already contains deterministic policy cards; retrieval here only adds noise.
        if is_identity_or_policy_query(payload.message):
            model_id = "gpt-5.2"
            overlay_text_local = overlay_to_instructions(payload.overlay) if getattr(payload, "overlay", None) else ""
            system_prompt = build_system_prompt(payload.user_id, [], overlay_text=overlay_text_local, vantage_id=vantage_id)
            meta = build_meta_explanation(payload.user_id, payload.message, []) or {}
            meta["model"] = {"id": model_id}
            try:
                meta.setdefault("identity", {"vantage_id": vantage_id, "user_id_alias": alias_uid, "canonical_user_id": payload.user_id})
            except Exception:
                pass
            answer = complete_chat(system_prompt, payload.message, model=model_id)
            return RAGResponse(
                answer=answer,
                memory_used=[],
                system_prompt=system_prompt,
                meta_explanation=meta,
            )

        # --- Pure re-entry greeting: bypass retrieval to avoid therapy-ish injections ---
        if is_pure_reentry_greeting(payload.message):
            # Build minimal prompt (no memory injection)
            system_prompt = (
                "You are Verbal Sage.\n"
                "Speak like a normal, thoughtful person in natural prose.\n"
                "Avoid bullet points and numbered menus unless explicitly requested.\n"
                "Do not steer with category choices like \"writing/speaking/grammar\".\n"
                "Do not suggest next steps at the end.\n"
                "Ask one open-ended question that helps the user continue.\n"
            )
            if overlay_text:
                system_prompt = system_prompt + "\n\n" + overlay_text
            meta = build_meta_explanation(payload.user_id, payload.message, [])

            # attach model id (keep your current approach)
            model_id = "gpt-5.2"
            if meta is None:
                meta = {}
            meta.setdefault("identity", {"vantage_id": vantage_id, "user_id_alias": alias_uid, "canonical_user_id": payload.user_id})
            meta["model"] = {"id": model_id}
            # apply re-entry line if policy says so
            reentry_prefix = ""
            try:
                temporal = (meta or {}).get("temporal") or {}
                query_tags = (meta or {}).get("query_tags") or []
                if should_add_reentry_line(temporal, payload.message, query_tags=query_tags):
                    reentry_prefix = build_reentry_line(temporal)
            except Exception as e:
                print(f"[temporal] reentry policy error: {e}")

            # generate answer
            answer = complete_chat(system_prompt, payload.message, model=model_id)
            if reentry_prefix:
                answer = reentry_prefix + answer

            return RAGResponse(
                answer=answer,
                memory_used=[],
                system_prompt=system_prompt,
                meta_explanation=meta,
            )
        # 1) personal memory from memory_raw
        personal_memory = retrieve_personal_memory(
            user_id=payload.user_id,
            query=payload.message,
            top_k=min(8, payload.top_k),  # get a few more so re-ranking has room
            vantage_id=vantage_id,
        )

        # --- vb_desire_profile bias map (used in personal-memory rerank) ---
        bias_map = {}
        try:
            card = load_latest_vb_desire_profile(payload.user_id)
            if card:
                bias_map = vb_desire_bias_map(card)
        except Exception as e:
            print(f"[vb_desire] bias_map error: {e}")
            bias_map = {}

        print(f"[vb_desire] user={payload.user_id} bias_keys={list(bias_map.keys())[:10]}")

        # 1b) re-rank personal memory by feedback + tags
        if personal_memory:
            personal_memory = sorted(
                personal_memory,
                key=lambda h: score_personal_hit(h, payload.message, bias_map=bias_map),
                reverse=True,
            )
            # keep only the top few after re-ranking
            personal_memory = personal_memory[: min(3, payload.top_k)]

        # 2) corpus from all other collections
        corpus_memory = unified_retrieve(
            query=payload.message,
            top_k=payload.top_k,
            vantage_id=vantage_id,
        )

        # 3) combined list, personal first
        memory_chunks: List[Dict[str, Any]] = personal_memory + corpus_memory

        # 4) build persona + memory system prompt
        system_prompt = build_system_prompt(payload.user_id, memory_chunks, overlay_text=overlay_text, vantage_id=vantage_id)

        # 4b) build meta explanation (why this answer looks this way)
        meta = build_meta_explanation(payload.user_id, payload.message, memory_chunks)

                # --- temporal re-entry line (v1) ---
        reentry_prefix = ""
        try:
            temporal = (meta or {}).get("temporal") or {}
            query_tags = (meta or {}).get("query_tags") or []
            if should_add_reentry_line(temporal, payload.message, query_tags=query_tags):
                reentry_prefix = build_reentry_line(temporal)
        except Exception as e:
            print(f"[temporal] reentry policy error: {e}")

        # 4c) choose model
        model_id = "gpt-5.2"  # or "gpt-5.1" or your ft model
        if meta is None:
            meta = {}
        meta.setdefault("identity", {"vantage_id": vantage_id, "user_id_alias": alias_uid, "canonical_user_id": payload.user_id})
        meta["model"] = {"id": model_id}
        # 4d) inject a small gravity/misalignment hint into the system prompt (only for escapes)
        try:
            gravity_info = (meta or {}).get("gravity") or {}
            mis = float(gravity_info.get("misalignment") or 0.0)
            label = gravity_info.get("label") or "aligned"

            if mis >= 0.4:
                system_prompt = (
                    system_prompt
                    + f"\n\n[gravity-note] Current request is classified as '{label}' "
                      f"(misalignment={mis:.3f}) relative to the user's usual style. "
                      "Prioritize satisfying the explicit request and local context, "
                      "even if it differs from past patterns or preferences.\n"
                )
        except Exception as e:
            print(f"[gravity-note] error while annotating system_prompt: {e}")

        # 5) generate answer (ONCE)
        answer = complete_chat(system_prompt, payload.message, model=model_id)

        if reentry_prefix:
            answer = reentry_prefix + answer

        # 6) store last answer and personal memory ids for feedback
        memory_ids = []
        for chunk in personal_memory:  # only memory_raw hits
            mid = chunk.get("id")
            if mid:
                memory_ids.append(mid)

        _last_rag_result[_rag_key(payload.user_id, payload.thread_id)] = {
            "answer": answer,
            "memory_ids": memory_ids,
        }

        # 7) return answer + debug info
        return RAGResponse(
            answer=answer,
            memory_used=memory_chunks,
            system_prompt=system_prompt,
            meta_explanation=meta,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def classify_feedback_nl(last_answer: str, user_message: str) -> str:
    """
    Natural-language feedback classifier.
    Returns: "positive", "negative", or "neutral".

    Strategy:
    1) Check for strong regex markers (cheap and deterministic).
    2) If still neutral, optionally ask OpenAI to classify the feedback.
    """

    text = (user_message or "").strip().lower()
    if not text:
        return "neutral"

    # --- 1) strong negative signals ---
    negative_markers = [
        "that wasn't helpful",
        "that wasnt helpful",
        "not helpful",
        "that is wrong",
        "that's wrong",
        "you are wrong",
        "this is wrong",
        "that missed the point",
        "you missed the point",
        "i don't like that answer",
        "i do not like that answer",
    ]
    for m in negative_markers:
        if m in text:
            return "negative"

    # --- 2) strong positive signals ---
    positive_markers = [
        "that was helpful",
        "this was helpful",
        "that is helpful",
        "that's helpful",
        "exactly right",
        "that's perfect",
        "that’s perfect",
        "perfect, thank you",
        "this is good",
        "that is good",
        "this is exactly what i meant",
        "that is exactly what i meant",
    ]
    for m in positive_markers:
        if m in text:
            return "positive"

    # --- 3) fallback: ask OpenAI, if available ---
    client = get_openai()
    if client is None:
        # no OpenAI client configured → stay neutral
        return "neutral"

    # Build a short, safe classification prompt
    prompt_system = (
        "You are a classifier. The user has just reacted to an answer.\n"
        "Your job is to decide if their reaction expresses positive, negative, or neutral\n"
        "feedback about how helpful that answer was.\n\n"
        "Respond with exactly one word: 'positive', 'negative', or 'neutral'."
    )

    prompt_user = (
        "Answer that was given:\n"
        f"{(last_answer or '').strip()}\n\n"
        "User's reaction:\n"
        f"{user_message.strip()}\n\n"
        "Classify the user's reaction."
    )

    try:
        resp = client.chat.completions.create(
            model=os.getenv("FEEDBACK_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
            max_tokens=1,
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip().lower()
        if "positive" in raw:
            return "positive"
        if "negative" in raw:
            return "negative"
        return "neutral"
    except Exception as e:
        print("[classify_feedback_nl] OpenAI error:", str(e))
        return "neutral"

class FeedbackPayload(BaseModel):
    user_id: str
    message: str  # user message that may carry feedback
    thread_id: str | None = None


@router.post("/feedback")
def rag_feedback(payload: FeedbackPayload, vantage_id: str = "default"):
    """
    Interpret the user's latest message as feedback on the previous answer.
    - Classify it as positive/negative/neutral.
    - Optionally extract a user-defined tag ("tag this as ...").
    - Send both signal and tag to /memory_feedback for all last-used memory points.
    """
    alias_uid = payload.user_id
    canon_uid, _ = resolve_canonical_user_id(vantage_id, alias_uid)
    if canon_uid != (alias_uid or ""):
        print(f"[identity] rag_feedback canonicalized user_id alias={alias_uid} canon={canon_uid} vid={vantage_id}")
    user_id = canon_uid
    payload.user_id = canon_uid
    fb_text = (payload.message or "").strip()
    if not fb_text:
        return {"status": "empty"}

    key = _rag_key(user_id, payload.thread_id)

    last = _last_rag_result.get(key)


    # backward-compatible fallback (older clients that never sent thread_id)

    if not last and key[1]:

        last = _last_rag_result.get(_rag_key(user_id, None))
    if not last:
        return {"status": "no_last_answer"}

    # 1) classify feedback sentiment (positive/negative/neutral)
    signal = classify_feedback_nl(
        last_answer=last.get("answer", ""),
        user_message=fb_text,
    )

    # 2) extract an optional tag like "fractal_monism_expansion"
    tag = extract_tag_from_message(fb_text)

    # If no sentiment and no tag, nothing to do
    if signal == "neutral" and not tag:
        return {"status": "neutral"}

    memory_ids = last.get("memory_ids") or []
    if not memory_ids:
        return {"status": "no_memory_ids"}

    updated = 0
    for mid in memory_ids:
        payload_out = {
            "user_id": user_id,
            "memory_id": mid,
            "signal": signal,  # may be "neutral"
        }
        if tag:
            payload_out["tag"] = tag

        try:
            r = requests.post(
                "http://127.0.0.1:8088/memory_feedback",
                json=payload_out,
                timeout=3.0,
            )
            if r.ok:
                updated += 1
        except Exception as e:
            print(f"[rag_feedback] error sending feedback for id={mid}: {e}")

    # Optional: refresh persona/style card based on recent messages
    try:
        quick_persona_refresh(user_id)
    except Exception as e:
        print(f"[rag_feedback] quick_persona_refresh error for user {user_id}: {e}")

    return {"status": "ok", "signal": signal, "tag": tag, "updated": updated}
