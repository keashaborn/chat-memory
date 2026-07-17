from __future__ import annotations

from typing import Any, Dict, List, Tuple
import os
import re
import asyncio
import asyncpg
import uuid
from datetime import datetime, timezone
import math
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .openai_client import complete_chat, complete_chat_messages, normalize_chat_model
from .prompt_builder import build_system_prompt
from .role_overlay import overlay_to_instructions
from .retriever_unified import retrieve_personal_memory, unified_retrieve
from .lifeswitch_auth import require_actor_matches_owner
from .memory_v1_shadow import run_memory_v1_runtime
from .memory_v1_v5_shadow_trace import run_memory_v1_v5_shadow_trace
from .memory_v1_intent import (
    apply_legacy_personal_memory_gate,
    classify_legacy_personal_memory_access,
    resolve_legacy_personal_memory_mode,
)
from .memory_v1_preference_project_shadow import run_preference_project_runtime
from .query_embedding_cache import QueryEmbeddingCache

# Current Vantage query helpers. Kept separate from retired RAG endpoints.
from .vantage_query_support import (
    build_meta_explanation,
    is_pure_reentry_greeting,
)

from .vantage_engine import normalize_limits, extract_sd_features, derive_params, decide, build_overlay_text

router = APIRouter()


# ---------- RAG policy (internal per-vantage corpus selection) ----------
def _csv_env(name: str) -> List[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

async def _rag_policy_get(vantage_id: str) -> Dict[str, Any]:
    vid = (vantage_id or "default").strip() or "default"
    dsn = os.environ["POSTGRES_DSN"]
    try:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT policy FROM vantage_identity.rag_policy WHERE vantage_id=$1",
                vid,
            )
        finally:
            await conn.close()
    except Exception as e:
        print(f"[rag_policy] db get error vid={vid}: {e}")
        return {}

    pol = (row["policy"] if row else {}) or {}
    if isinstance(pol, str):
        try:
            pol = json.loads(pol)
        except Exception:
            pol = {}
    return dict(pol) if isinstance(pol, dict) else {}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)

def _vantage_key(user_id: str, thread_id: str | None, vantage_id: str | None) -> Tuple[str, str, str]:
    uid = (user_id or "").strip() or "anon"

    tid = (thread_id or "").strip()
    if tid and not _UUID_RE.match(tid):
        tid = ""

    vid = (vantage_id or "").strip() or "default"
    return (uid, tid, vid)

_last_vantage_result: Dict[Tuple[str, str, str], Dict[str, Any]] = {}


async def _write_vantage_answer_trace(
    *,
    user_id: str,
    thread_id: str | None,
    vantage_id: str,
    model_id: str | None,
    answer_id: str,
    answer_text: str,
    memory_ids: List[str],
    request_id: str | None = None,
) -> None:
    """
    Durable attribution record for a /vantage/query answer.
    Writes to public.vantage_answer_trace.
    """
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        return
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]

    tid = thread_id if (thread_id and _UUID_RE.match(thread_id)) else None

    rid: str | None = None
    try:
        rid = str(request_id).strip() if request_id is not None else None
    except Exception:
        rid = None
    if rid:
        rid = rid[:256]

    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                """
                INSERT INTO public.vantage_answer_trace(
                  answer_id, user_id, thread_id, vantage_id, model_id,
                  answer_text, answer_text_hash, answer_text_len, memory_ids,
                  request_id
                )
                VALUES ($1::uuid, $2, $3::uuid, $4, $5,
                        $6, md5($6), length($6), $7::text[],
                        $8)
                """,
                answer_id,
                user_id,
                tid,
                vantage_id,
                model_id,
                answer_text,
                memory_ids,
                rid,
            )
        finally:
            await conn.close()
    except Exception as e:
        import sys, traceback
        sys.stderr.write(f"[vantage] write_answer_trace error: {e}\n")
        traceback.print_exc()
        sys.stderr.flush()


class VantageLimits(BaseModel):
    Y: float = 0.5  # Concession Cap
    R: float = 0.5  # Ledger Update Gate
    C: float = 0.5  # Policy Coupling Gain
    S: float = 0.5  # Ornament Budget

class VantageQuery(BaseModel):
    user_id: str
    message: str
    thread_id: str | None = None
    top_k: int = 5
    overlay: Dict[str, Any] | None = None
    limits: VantageLimits | None = None
    debug: bool | None = False
    routing: Dict[str, Any] | None = None
    mix: Dict[str, Any] | None = None
    pragmatics: Dict[str, Any] | None = None
    roleplay: Dict[str, Any] | None = None
    definition_overlay: Dict[str, Any] | None = None
    vantage_id: str | None = None
    model: str | None = None
    inspect_only: bool | None = False

class VantageResponse(BaseModel):
    answer: str
    answer_id: str | None = None
    meta_explanation: Dict[str, Any] | None = None
    # Debug-only fields
    memory_used: List[Dict[str, Any]] | None = None
    system_prompt: str | None = None

def _model_to_dict(m: Any) -> Dict[str, Any]:
    if m is None:
        return {}
    if hasattr(m, "model_dump"):
        return m.model_dump()
    if hasattr(m, "dict"):
        return m.dict()
    return dict(m)

_Q_SENT_RE = re.compile(r"[^?\n]{1,280}\?")

def _clamp01(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(default)
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _clamp_int(x: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(round(float(x)))
    except Exception:
        v = int(default)
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


_PHATIC_RE = re.compile(
    r"^\s*(hey|hi|hello|yo|sup|how are you|how's it going|hows it going|good morning|good afternoon|good evening|thanks|thank you|sorry)\b",
    re.I,
)

_TASKY_RE = re.compile(
    r"\b(build|implement|fix|debug|write|draft|refactor|explain|summarize|analy(ze|sis)|plan|steps?|commands?|code|script|error|trace|stack|logs?)\b",
    re.I,
)


def _looks_phatic(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return bool(_PHATIC_RE.search(t)) and len(t) <= 80


def _looks_tasky(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_TASKY_RE.search(t))


_RECALL_RE = re.compile(
    r"\b("
    r"what (was|is|did|do) (my|i|you)|"
    r"what .* remember|"
    r"what .* told you|"
    r"remind me|"
    r"do you remember|"
    r"remember .* about|"
    r"memory qa|"
    r"marker animal|"
    r"my dad'?s name|"
    r"my father'?s name|"
    r"my mother'?s name"
    r")\b",
    re.I,
)


def _looks_specific_personal_recall(text: str) -> bool:
    """
    Direct recall mode: favor user personal/archive memory and avoid corpus crowd-out.
    This is for queries like "what was my marker animal?" or "what is my dad's name?",
    not for general technical/product questions.
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_RECALL_RE.search(t))


def _looks_broad_profile_summary(text: str) -> bool:
    """
    Broad profile/background integration. This is not narrow recall.
    """
    t = (text or "").lower().strip()
    if not t:
        return False
    cues = (
        "my background",
        "my history",
        "about me",
        "what do you know about me",
        "what do you remember about me",
        "what do you know about my background",
        "my current project",
        "my work history",
        "my professional background",
        "caravel",
        "clinical psychologist",
        "bcba",
    )
    return any(cue in t for cue in cues)


def _looks_technical_admin_turn(text: str) -> bool:
    """
    Technical/admin/dev turn. These should not get durable biography by default.
    """
    t = (text or "").lower().strip()
    if not t:
        return False
    cues = (
        "restart",
        "rebuild",
        "build",
        "deploy",
        "frontend",
        "backend",
        "server",
        "service",
        "systemctl",
        "journalctl",
        "nginx",
        "node",
        "npm",
        "pnpm",
        "python",
        "postgres",
        "qdrant",
        "redis",
        "docker",
        "supabase",
        "api",
        "route",
        "endpoint",
        "code",
        "script",
        "patch",
        "debug",
        "error",
        "logs",
        "grep",
        "sed",
        "git",
        "commit",
        "branch",
    )
    return any(cue in t for cue in cues)


def _looks_memory_architecture_turn(text: str) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return False

    strong_terms = [
        "memory architecture",
        "memory system",
        "memory systems",
        "prompt injection",
        "injection bloat",
        "retrieval plan",
        "turn plan",
        "turn_plan",
        "memory inspector",
        "prompt inspector",
        "card policy",
        "profile card",
        "preference card",
        "personal archive",
        "vector memory",
        "qdrant",
        "memory route audit",
        "route audit",
        "vantage memory",
        "memory retrieval",
        "memory injection",
        "raw memory dump",
        "episodic memory",
    ]
    if any(term in t for term in strong_terms):
        return True

    # More general combinations, kept narrow to avoid stealing ordinary TECH turns.
    memory_words = ("memory", "memories", "remembered", "retrieval", "retrieve", "inject", "injected", "injection")
    architecture_words = ("architecture", "policy", "planner", "planning", "gating", "gate", "budget", "compress", "compression", "surface", "surfacing")
    return any(a in t for a in memory_words) and any(b in t for b in architecture_words)



def _looks_fm_conceptual_turn(text: str) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return False

    strong_terms = [
        "origin of consciousness",
        "consciousness emerge",
        "consciousness emerges",
        "emergence of consciousness",
        "development of consciousness",
        "fractal monism",
        "fractal monistic",
        "recursive differentiation",
        "perception create",
        "perception creates",
        "perception generate",
        "perception generates",
        "selfhood emerge",
        "meaning emerge",
        "meaning emerges",
        "ontology",
        "metaphysics",
    ]
    if any(term in t for term in strong_terms):
        return True

    concept_terms = (
        "consciousness",
        "perception",
        "selfhood",
        "meaning",
        "identity",
        "being",
        "existence",
        "duality",
        "recursion",
        "differentiation",
        "oscillation",
    )
    request_terms = (
        "describe",
        "explain",
        "how does",
        "how do",
        "what is",
        "what are",
        "origin",
        "emerge",
        "emerges",
        "arise",
        "arises",
    )
    return any(c in t for c in concept_terms) and any(r in t for r in request_terms)


def _classify_memory_turn_intent(text: str) -> str:
    """
    Explicit retrieval/injection planning intent.

    Ordering matters:
    - PROFILE_SUMMARY must win over broad "what do you..." recall regexes.
    - TECH should suppress profile cards even if tasky wording is broad.
    - SPECIFIC_RECALL is narrow archive lookup.
    """
    if _looks_broad_profile_summary(text):
        return "PROFILE_SUMMARY"
    if _looks_memory_architecture_turn(text):
        return "MEMORY_ARCHITECTURE"
    if _looks_fm_conceptual_turn(text):
        return "FM_CONCEPTUAL"
    if _looks_technical_admin_turn(text):
        return "TECH"
    if _looks_specific_personal_recall(text):
        return "SPECIFIC_RECALL"
    return "GENERAL"


def _clamp_float01(raw: Any, default: float = 0.0) -> float:
    try:
        n = float(raw)
    except Exception:
        n = float(default)
    return max(0.0, min(1.0, n))


def _build_turn_plan_v0(
    *,
    turn_intent: str,
    mix: Dict[str, Any] | None,
    routing: Dict[str, Any] | None,
    pragmatics: Dict[str, Any] | None,
    limits: Dict[str, Any] | None,
    retrieval_plan: Dict[str, Any] | None,
    thread_stats: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """
    V0 control arbitration visibility.

    Diagnostic only for now: exposes requested vs effective controls without
    changing behavior yet. Later patches should make retrieval, lens, thread
    context, and injection budgets consume this plan.
    """
    mix = mix or {}
    routing = routing or {}
    pragmatics = pragmatics or {}
    limits = limits or {}
    retrieval_plan = retrieval_plan or {}
    thread_stats = thread_stats or {}

    ti = (turn_intent or "GENERAL").strip().upper() or "GENERAL"

    def _int_range(raw: Any, lo: int, hi: int, default: int) -> int:
        try:
            n = int(raw)
        except Exception:
            n = int(default)
        return max(lo, min(hi, n))

    requested_controls = {
        "conversation": _clamp_float01(mix.get("conversation", 0.0), 0.0),
        "memory_cards": _clamp_float01(mix.get("memory_cards", 0.0), 0.0),
        "corpus": _clamp_float01(mix.get("corpus", 1.0), 1.0),
        "lens_fm": _clamp_float01(mix.get("lens_fm", 0.0), 0.0),
        "recency_bias": _clamp_float01(mix.get("recency_bias", 0.0), 0.0),
        "similarity_threshold": (
            None if mix.get("similarity_threshold", None) is None
            else _clamp_float01(mix.get("similarity_threshold"), 0.4)
        ),
        "answer_first": bool(routing.get("answer_first", True)),
        "clarify_bias": _clamp_float01(routing.get("clarify_bias", 0.10), 0.10),
        "max_clarify_questions": _int_range(routing.get("max_clarify_questions", 1), 0, 3, 1),
        "rfg": _clamp_float01(pragmatics.get("rfg", 0.0), 0.0),
        "df": _clamp_float01(pragmatics.get("df", 0.7), 0.7),
        "pe": _int_range(pragmatics.get("pe", 2), 0, 3, 2),
        "Y": _clamp_float01(limits.get("Y", 0.5), 0.5),
        "R": _clamp_float01(limits.get("R", 0.5), 0.5),
        "C": _clamp_float01(limits.get("C", 0.5), 0.5),
        "S": _clamp_float01(limits.get("S", 0.5), 0.5),
    }

    effective_controls = dict(requested_controls)
    suppressed: List[str] = []

    if ti == "TECH" and effective_controls.get("lens_fm", 0.0) > 0.0:
        effective_controls["lens_fm"] = 0.0
        suppressed.append("lens_fm clamped to 0.0 because turn_intent=TECH")

    if ti == "MEMORY_ARCHITECTURE" and effective_controls.get("lens_fm", 0.0) > 0.0:
        effective_controls["lens_fm"] = 0.0
        suppressed.append("lens_fm clamped to 0.0 because turn_intent=MEMORY_ARCHITECTURE")

    if ti == "TECH" and effective_controls.get("conversation", 0.0) > 0.0:
        effective_controls["conversation"] = 0.0
        suppressed.append("conversation clamped to 0.0 because turn_intent=TECH")

    if ti == "TECH" and effective_controls.get("memory_cards", 0.0) > 0.0:
        effective_controls["memory_cards"] = 0.0
        suppressed.append("memory_cards/personal archive clamped to 0.0 because turn_intent=TECH")

    if ti == "TECH" and effective_controls.get("corpus", 0.0) > 0.0:
        effective_controls["corpus"] = 0.0
        suppressed.append("corpus clamped to 0.0 because turn_intent=TECH")

    notes: List[str] = []
    if ti == "TECH":
        notes.append("TECH clamps FM lens, broad thread context, personal archive, and general corpus now; profile biography remains suppressed.")
    elif ti == "SPECIFIC_RECALL":
        notes.append("SPECIFIC_RECALL should prioritize personal archive and suppress corpus/profile cards.")
    elif ti == "PROFILE_SUMMARY":
        notes.append("PROFILE_SUMMARY may use profile cards and compressed personal archive.")
    elif ti == "MEMORY_ARCHITECTURE":
        notes.append("MEMORY_ARCHITECTURE should use compressed memory-system history and limited FM/AI corpus.")
    elif ti == "FM_CONCEPTUAL":
        notes.append("FM_CONCEPTUAL should use FM lens and targeted conceptual corpus while suppressing profile biography and broad personal archive.")
    else:
        notes.append("GENERAL currently follows requested mix; later it should use stricter default budgets.")

    injection_budget = {
        "thread_messages_injected": int(thread_stats.get("n_messages") or 0),
        "thread_chars_injected": int(thread_stats.get("n_chars") or 0),
        "max_personal_hits_requested": int(retrieval_plan.get("k_personal") or 0),
        "max_corpus_hits_requested": int(retrieval_plan.get("k_corpus") or 0),
        "base_k": int(retrieval_plan.get("base_k") or 0),
        "compression_required": bool(ti in ("GENERAL", "MEMORY_ARCHITECTURE", "PROFILE_SUMMARY", "FM_CONCEPTUAL")),
        "raw_personal_memory_allowed": bool(ti == "SPECIFIC_RECALL"),
    }

    allowed_stores = {
        "preference_cards": True,
        "profile_cards": bool(ti == "PROFILE_SUMMARY"),
        "personal_archive": bool(retrieval_plan.get("personal_archive_enabled")),
        "corpus": bool(retrieval_plan.get("corpus_enabled")),
        "thread_context": bool((thread_stats.get("n_messages") or 0) > 0),
        "lifeswitch_structured": bool(ti == "LIFESWITCH"),
    }

    return {
        "version": "turn_plan_v0_visibility",
        "turn_intent": ti,
        "requested_controls": requested_controls,
        "effective_controls": effective_controls,
        "allowed_stores": allowed_stores,
        "injection_budget": injection_budget,
        "suppressed": suppressed,
        "notes": notes,
    }

def _build_memory_retrieval_plan(
    *,
    turn_intent: str,
    use_personal: bool,
    mix: Dict[str, Any],
    requested_top_k: Any,
) -> Dict[str, Any]:
    """
    Turn intent -> explicit retrieval plan.

    This centralizes the decision about which memory stores to search and how
    much context to inject. It preserves current behavior while making the
    plan inspectable and testable.
    """
    try:
        if "memory_cards" in (mix or {}):
            w_mem = float((mix or {}).get("memory_cards", 0.0))
        else:
            w_mem = float(os.getenv("VANTAGE_DEFAULT_MEMORY_WEIGHT", "1.0") or 1.0)
    except Exception:
        w_mem = 1.0

    try:
        w_corpus = float((mix or {}).get("corpus", 1.0))
    except Exception:
        w_corpus = 1.0

    w_mem = max(0.0, min(1.0, w_mem))
    w_corpus = max(0.0, min(1.0, w_corpus))

    thr = (mix or {}).get("similarity_threshold", None)
    try:
        thr_f = float(thr) if thr is not None else None
    except Exception:
        thr_f = None

    personal_thr_f = float(thr_f) if thr_f is not None else float(
        os.getenv("VANTAGE_PERSONAL_SCORE_THRESHOLD", "0.20") or 0.20
    )

    try:
        base_k = int(requested_top_k or 5)
    except Exception:
        base_k = 5
    base_k = max(0, min(50, base_k))

    k_personal = 0 if (not use_personal or w_mem <= 0.0) else max(1, int(round(base_k * w_mem)))
    k_corpus = 0 if (w_corpus <= 0.0) else max(1, int(round(base_k * w_corpus)))

    ti = (turn_intent or "GENERAL").strip().upper()
    recall_mode = (ti == "SPECIFIC_RECALL")

    if recall_mode and use_personal:
        # Specific personal recall searches the user's archive first.
        # It retrieves enough to find answer-bearing context, but injects only a small set.
        base_k = int(os.getenv("VANTAGE_RECALL_BASE_K", "3") or 3)
        base_k = max(1, min(20, base_k))
        k_personal = max(k_personal, int(os.getenv("VANTAGE_RECALL_PERSONAL_K", "10") or 10))
        k_personal = max(1, min(50, k_personal))
        k_corpus = int(os.getenv("VANTAGE_RECALL_CORPUS_K", "0") or 0)
        k_corpus = max(0, min(20, k_corpus))
        personal_thr_f = min(
            personal_thr_f,
            float(os.getenv("VANTAGE_RECALL_PERSONAL_THRESHOLD", "0.05") or 0.05),
        )

    if ti == "MEMORY_ARCHITECTURE":
        # Memory-system design turns benefit from targeted memory, but raw
        # retrieved chunks should be budgeted tightly until compression is active.
        base_k = int(os.getenv("VANTAGE_MEMORY_ARCH_BASE_K", "3") or 3)
        base_k = max(1, min(10, base_k))
        k_personal = 0 if (not use_personal or w_mem <= 0.0) else min(k_personal, int(os.getenv("VANTAGE_MEMORY_ARCH_PERSONAL_K", "2") or 2))
        k_personal = max(0, min(10, k_personal))
        k_corpus = 0 if (w_corpus <= 0.0) else min(k_corpus, int(os.getenv("VANTAGE_MEMORY_ARCH_CORPUS_K", "1") or 1))
        k_corpus = max(0, min(10, k_corpus))

    if ti == "FM_CONCEPTUAL":
        # Conceptual Fractal Monism turns should use the curated FM corpus and
        # avoid broad personal archive/profile injection unless explicitly needed.
        base_k = int(os.getenv("VANTAGE_FM_CONCEPTUAL_BASE_K", "4") or 4)
        base_k = max(1, min(10, base_k))
        k_personal = int(os.getenv("VANTAGE_FM_CONCEPTUAL_PERSONAL_K", "0") or 0)
        k_personal = 0 if (not use_personal or w_mem <= 0.0) else max(0, min(5, k_personal))
        k_corpus = 0 if (w_corpus <= 0.0) else int(os.getenv("VANTAGE_FM_CONCEPTUAL_CORPUS_K", "4") or 4)
        k_corpus = max(0, min(10, k_corpus))

    return {
        "turn_intent": ti,
        "recall_mode": bool(recall_mode),
        "use_personal": bool(use_personal),
        "w_mem": float(w_mem),
        "w_corpus": float(w_corpus),
        "base_k": int(base_k),
        "k_personal": int(k_personal),
        "k_corpus": int(k_corpus),
        "threshold": thr_f,
        "personal_threshold": float(personal_thr_f),
        "personal_archive_enabled": bool(k_personal > 0),
        "corpus_enabled": bool(k_corpus > 0),
    }



def _semantic_dedupe_ref(hit: Dict[str, Any], idx: int) -> str:
    if not isinstance(hit, dict):
        return f"unknown:{idx}"
    payload = hit.get("payload") or {}
    src = str(hit.get("_src") or payload.get("source") or "memory").strip() or "memory"
    coll = str(hit.get("collection") or payload.get("dataset") or payload.get("source_file") or "unknown").strip() or "unknown"
    raw_id = hit.get("id") or hit.get("memory_id") or hit.get("point_id") or payload.get("id") or payload.get("request_id") or idx
    return f"{src}:{coll}:{raw_id}"


def _semantic_dedupe_key(hit: Dict[str, Any]) -> str:
    payload = (hit or {}).get("payload") or {}
    q = " ".join(str(payload.get("question") or "").lower().split()).strip()
    if q:
        q = (
            q.replace("“", '"')
             .replace("”", '"')
             .replace("’", "'")
             .replace("‘", "'")
        )
        q = q.replace('"', "")
        q = q.replace("'", "")
        q = q.replace("the i", "i")
        q = " ".join(q.split())
        return f"q:{q}"

    text = (
        payload.get("text")
        or payload.get("content")
        or payload.get("answer")
        or ""
    )
    t = " ".join(str(text or "").lower().split()).strip()
    return f"t:{t[:160]}" if t else ""


def _build_semantic_dedupe_preview_v0(
    *,
    turn_intent: str,
    memory_chunks: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """
    Debug-only preview of obvious semantic redundancy in retrieved chunks.

    V0 intentionally uses deterministic question/text normalization rather than
    embeddings or LLM summarization. It does not change prompt insertion.
    """
    ti = (turn_intent or "").strip().upper()
    if ti not in ("FM_CONCEPTUAL", "MEMORY_ARCHITECTURE", "GENERAL", "PROFILE_SUMMARY"):
        return None

    chunks = list(memory_chunks or [])
    if not chunks:
        return None

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for idx, hit in enumerate(chunks, 1):
        if not isinstance(hit, dict):
            continue
        key = _semantic_dedupe_key(hit)
        if not key:
            continue
        groups.setdefault(key, []).append({
            "idx": idx,
            "source_ref": _semantic_dedupe_ref(hit, idx),
            "score": float(hit.get("score") or 0.0),
            "question": ((hit.get("payload") or {}).get("question") or ""),
        })

    clusters: List[Dict[str, Any]] = []
    duplicate_risk_count = 0

    for key, items in groups.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda x: float(x.get("score") or 0.0), reverse=True)
        canonical = ordered[0]
        duplicates = ordered[1:]
        duplicate_risk_count += len(duplicates)
        clusters.append({
            "cluster_key": key[:180],
            "canonical_ref": canonical.get("source_ref"),
            "duplicate_refs": [d.get("source_ref") for d in duplicates],
            "member_count": len(ordered),
            "duplicate_count": len(duplicates),
            "reason": "normalized_question_match" if key.startswith("q:") else "normalized_text_prefix_match",
            "canonical_question": canonical.get("question") or "",
        })

    return {
        "version": "semantic_dedupe_preview_v0",
        "mode": "debug_only",
        "turn_intent": ti,
        "input_count": len(chunks),
        "cluster_count": len(clusters),
        "duplicate_risk_count": duplicate_risk_count,
        "clusters": clusters,
    }


def _apply_semantic_dedupe_v0(
    *,
    turn_intent: str,
    memory_chunks: List[Dict[str, Any]],
    dedupe_preview: Dict[str, Any] | None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
    """
    Apply deterministic dedupe for selected prompt-injection paths.

    V0 is intentionally narrow:
    - only FM_CONCEPTUAL
    - only removes refs already identified by semantic_dedupe_preview
    - keeps canonical chunk and non-clustered distinct chunks
    """
    ti = (turn_intent or "").strip().upper()
    chunks = list(memory_chunks or [])

    if ti != "FM_CONCEPTUAL":
        return chunks, None
    if not isinstance(dedupe_preview, dict) or not dedupe_preview:
        return chunks, None

    duplicate_refs: set[str] = set()
    for cluster in (dedupe_preview.get("clusters") or []):
        if not isinstance(cluster, dict):
            continue
        for ref in (cluster.get("duplicate_refs") or []):
            if ref:
                duplicate_refs.add(str(ref))

    if not duplicate_refs:
        return chunks, {
            "version": "semantic_dedupe_apply_v0",
            "mode": "deterministic_apply",
            "turn_intent": ti,
            "applied": False,
            "input_count": len(chunks),
            "output_count": len(chunks),
            "removed_count": 0,
            "removed_refs": [],
            "reason": "no_duplicate_refs",
        }

    filtered: List[Dict[str, Any]] = []
    removed_refs: List[str] = []

    for idx, hit in enumerate(chunks, 1):
        ref = _semantic_dedupe_ref(hit, idx)
        if ref in duplicate_refs:
            removed_refs.append(ref)
            continue
        filtered.append(hit)

    return filtered, {
        "version": "semantic_dedupe_apply_v0",
        "mode": "deterministic_apply",
        "turn_intent": ti,
        "applied": bool(removed_refs),
        "input_count": len(chunks),
        "output_count": len(filtered),
        "removed_count": len(removed_refs),
        "removed_refs": removed_refs,
        "reason": "remove_duplicate_refs_from_semantic_dedupe_preview",
    }


def _preview_compact_text(text: str, *, limit: int = 520) -> str:
    """
    Deterministic extractive preview for inspect/debug use.
    This is not semantic summarization; it preserves source wording and caps length.
    """
    t = " ".join(str(text or "").split()).strip()
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + " …"


def _memory_preview_ref(hit: Dict[str, Any], idx: int) -> str:
    if not isinstance(hit, dict):
        return f"unknown:{idx}"
    payload = hit.get("payload") or {}
    src = str(hit.get("_src") or payload.get("source") or "memory").strip() or "memory"
    coll = str(hit.get("collection") or payload.get("dataset") or payload.get("source_file") or "unknown").strip() or "unknown"
    raw_id = hit.get("id") or hit.get("memory_id") or hit.get("point_id") or payload.get("id") or payload.get("request_id") or idx
    return f"{src}:{coll}:{raw_id}"


def _build_memory_compression_preview_v0(
    *,
    turn_intent: str,
    memory_chunks: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """
    Debug-only preview object for future compression/summarization work.

    It deliberately does not change the prompt. It lets the inspector compare:
      raw retrieved hits -> compact excerpts -> proposed extractive bullets.
    """
    ti = (turn_intent or "").strip().upper()
    if ti != "MEMORY_ARCHITECTURE":
        return None

    chunks = list(memory_chunks or [])
    preview_items: List[Dict[str, Any]] = []
    proposed_summary: List[Dict[str, Any]] = []
    raw_chars = 0
    compact_chars = 0
    source_refs: List[str] = []

    for idx, hit in enumerate(chunks, 1):
        if not isinstance(hit, dict):
            continue
        payload = hit.get("payload") or {}
        text = str(_hit_text(hit) or "").strip()
        if not text:
            continue

        ref = _memory_preview_ref(hit, idx)
        source_refs.append(ref)
        raw_chars += len(text)

        excerpt = _preview_compact_text(text, limit=520)
        compact_chars += len(excerpt)

        score = hit.get("score")
        try:
            score = float(score) if score is not None else None
        except Exception:
            score = None

        preview_items.append({
            "source_ref": ref,
            "source": hit.get("_src") or payload.get("source"),
            "collection": hit.get("collection") or payload.get("dataset") or payload.get("source_file"),
            "score": score,
            "raw_chars": len(text),
            "excerpt_chars": len(excerpt),
            "excerpt": excerpt,
        })
        proposed_summary.append({
            "source_ref": ref,
            "bullet": _preview_compact_text(text, limit=260),
        })

    return {
        "version": "memory_compression_preview_v0",
        "mode": "extractive_preview",
        "turn_intent": ti,
        "source_count": len(preview_items),
        "source_refs": source_refs,
        "raw_chars": int(raw_chars),
        "compact_chars": int(compact_chars),
        "preview": preview_items,
        "proposed_summary": proposed_summary,
    }



def _semantic_unit(
    *,
    semantic_type: str,
    claim: str,
    function: str,
    domain: List[str],
    source: str = "current_message",
    source_ref: str = "current_message",
    surface_policy: str = "CONTENT_OK",
    confidence: float = 0.75,
    retrieval_conditions: List[str] | None = None,
    suppression_conditions: List[str] | None = None,
    durability: str = "project_session",
    promotion_candidate: bool = False,
) -> Dict[str, Any]:
    return {
        "semantic_type": semantic_type,
        "claim": claim,
        "function": function,
        "domain": list(domain or []),
        "source": source,
        "source_ref": source_ref,
        "surface_policy": surface_policy,
        "confidence": float(confidence),
        "retrieval_conditions": list(retrieval_conditions or []),
        "suppression_conditions": list(suppression_conditions or []),
        "durability": durability,
        "promotion_candidate": bool(promotion_candidate),
    }


def _build_semantic_extraction_preview_v0(
    *,
    turn_intent: str,
    current_message: str,
    memory_chunks: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """
    Debug-only deterministic semantic extraction preview.

    This is the first step toward meaning extraction. It does not store memory,
    alter the prompt, or invoke an LLM. It only exposes candidate semantic units
    so we can inspect whether the extraction layer is aiming at the right things.
    """
    ti = (turn_intent or "").strip().upper()
    if ti != "MEMORY_ARCHITECTURE":
        return None

    text = " ".join(str(current_message or "").split()).strip()
    low = text.lower()
    units: List[Dict[str, Any]] = []

    if "memory architecture" in low or "memory system" in low or "enterprise memory" in low:
        units.append(_semantic_unit(
            semantic_type="project_context",
            claim="The current turn is about the Verbal Sage enterprise memory architecture.",
            function="Scopes retrieval, compression, and design reasoning to memory-system architecture rather than general biography or technical deployment work.",
            domain=["memory_architecture", "verbal_sage"],
            confidence=0.9,
            retrieval_conditions=[
                "when discussing Verbal Sage memory architecture",
                "when inspecting memory routing, compression, or semantic extraction",
            ],
            suppression_conditions=[
                "ordinary frontend/backend deployment work",
                "unrelated LifeSwitch nutrition or training turns",
            ],
            durability="project_session",
            promotion_candidate=False,
        ))

    if "injection bloat" in low or "prompt injection" in low:
        units.append(_semantic_unit(
            semantic_type="design_constraint",
            claim="The memory architecture should avoid prompt-injection bloat.",
            function="Constrains memory surfacing so retrieved context must be gated, budgeted, and compressed before prompt insertion.",
            domain=["memory_architecture", "prompt_control", "compression"],
            confidence=0.9,
            retrieval_conditions=[
                "when deciding whether retrieved memory should enter the prompt",
                "when evaluating memory compression or prompt budget behavior",
            ],
            suppression_conditions=[
                "specific factual recall where raw answer-bearing memory is required",
                "ordinary technical command generation unless memory routing is the subject",
            ],
            durability="long_term_project_preference",
            promotion_candidate=True,
        ))

    if "retriev" in low and ("useful" in low or "memories" in low or "memory" in low):
        units.append(_semantic_unit(
            semantic_type="design_goal",
            claim="The memory architecture should still retrieve useful memories while controlling context size.",
            function="Balances suppression/inhibition against recall usefulness; retrieval should not be disabled merely to avoid bloat.",
            domain=["memory_architecture", "retrieval", "salience"],
            confidence=0.86,
            retrieval_conditions=[
                "when tuning retrieval budgets",
                "when balancing recall usefulness against suppression or inhibition",
            ],
            suppression_conditions=[
                "when user asks for no-memory technical execution",
                "when retrieved memory is unrelated to the active domain",
            ],
            durability="long_term_project_preference",
            promotion_candidate=True,
        ))

    if not units and text:
        units.append(_semantic_unit(
            semantic_type="open_question",
            claim=text,
            function="Represents the current user question as a candidate design issue for later semantic extraction.",
            domain=["memory_architecture"],
            confidence=0.55,
            retrieval_conditions=[
                "when reviewing unresolved memory architecture questions",
            ],
            suppression_conditions=[
                "when higher-confidence semantic units supersede this fallback",
            ],
            durability="project_session",
            promotion_candidate=False,
        ))

    return {
        "version": "semantic_extraction_preview_v0",
        "schema_version": "semantic_unit_schema_v1",
        "mode": "deterministic_preview",
        "turn_intent": ti,
        "source": "current_message",
        "unit_count": len(units),
        "memory_source_count": len(list(memory_chunks or [])),
        "units": units,
    }


def _semantic_promotion_kind(unit: Dict[str, Any]) -> str:
    semantic_type = str((unit or {}).get("semantic_type") or "").strip()
    durability = str((unit or {}).get("durability") or "").strip()
    domains = [str(x) for x in ((unit or {}).get("domain") or [])]

    if semantic_type in ("design_constraint", "design_goal", "project_context"):
        return "project"
    if "style" in domains or semantic_type == "style_preference":
        return "pref"
    if durability.startswith("long_term"):
        return "project"
    return "topic"


def _build_semantic_promotion_preview_v0(
    *,
    semantic_preview: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    """
    Debug-only bridge from semantic units to proposed durable memory candidates.

    This does not write memory. It previews which semantic units would be eligible
    for promotion and how they would be classified.
    """
    if not isinstance(semantic_preview, dict) or not semantic_preview:
        return None

    units = semantic_preview.get("units") or []
    candidates: List[Dict[str, Any]] = []

    for idx, unit in enumerate(units, 1):
        if not isinstance(unit, dict):
            continue
        if not bool(unit.get("promotion_candidate")):
            continue

        kind = _semantic_promotion_kind(unit)
        use_scope = str(unit.get("surface_policy") or "CONTENT_OK")
        surface_policy = "mention_when_relevant" if use_scope == "CONTENT_OK" else "never"

        candidates.append({
            "candidate_id": f"semantic_promotion:{idx}",
            "kind": kind,
            "use_scope": use_scope,
            "surface_policy": surface_policy,
            "domains": list(unit.get("domain") or []),
            "claim": unit.get("claim") or "",
            "function": unit.get("function") or "",
            "retrieval_conditions": list(unit.get("retrieval_conditions") or []),
            "suppression_conditions": list(unit.get("suppression_conditions") or []),
            "durability": unit.get("durability") or "project_session",
            "source": unit.get("source") or "unknown",
            "source_ref": unit.get("source_ref") or "unknown",
            "confidence": float(unit.get("confidence") or 0.0),
        })

    return {
        "version": "semantic_promotion_preview_v0",
        "mode": "debug_only",
        "source": "semantic_extraction_preview",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _promotion_decision_for_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    candidate_id = str((candidate or {}).get("candidate_id") or "unknown")
    kind = str((candidate or {}).get("kind") or "").strip()
    use_scope = str((candidate or {}).get("use_scope") or "").strip()
    durability = str((candidate or {}).get("durability") or "").strip()
    claim = " ".join(str((candidate or {}).get("claim") or "").split()).strip()

    try:
        confidence = float((candidate or {}).get("confidence") or 0.0)
    except Exception:
        confidence = 0.0

    blocked_reasons: List[str] = []

    if not claim:
        blocked_reasons.append("missing_claim")
    if kind not in ("project", "pref", "topic"):
        blocked_reasons.append("unsupported_kind")
    if use_scope not in ("CONTENT_OK", "STYLE_ONLY"):
        blocked_reasons.append("unsupported_or_blocked_use_scope")
    if use_scope == "NEVER_SURFACE":
        blocked_reasons.append("never_surface")
    if not durability.startswith("long_term"):
        blocked_reasons.append("non_durable")
    if confidence < 0.75:
        blocked_reasons.append("low_confidence")

    duplicate_risk = "unknown"
    eligible = not blocked_reasons
    needs_review = True

    if eligible:
        suggested_action = "candidate_for_review"
        rationale = "Candidate passes deterministic eligibility checks but still requires review before any durable write."
    else:
        suggested_action = "do_not_store"
        rationale = "Candidate failed deterministic eligibility checks and should not be promoted."

    return {
        "candidate_id": candidate_id,
        "eligible": bool(eligible),
        "needs_review": bool(needs_review),
        "duplicate_risk": duplicate_risk,
        "blocked_reason": ",".join(blocked_reasons) if blocked_reasons else "",
        "suggested_action": suggested_action,
        "rationale": rationale,
    }


def _build_semantic_promotion_decision_preview_v0(
    *,
    promotion_preview: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    """
    Debug-only decision gate for semantic promotion candidates.

    This does not write memory. It only previews deterministic eligibility and
    review status before any future durable-memory write path exists.
    """
    if not isinstance(promotion_preview, dict) or not promotion_preview:
        return None

    candidates = promotion_preview.get("candidates") or []
    decisions: List[Dict[str, Any]] = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        decisions.append(_promotion_decision_for_candidate(candidate))

    eligible_count = sum(1 for d in decisions if bool(d.get("eligible")))
    blocked_count = len(decisions) - eligible_count
    needs_review_count = sum(1 for d in decisions if bool(d.get("needs_review")))

    return {
        "version": "semantic_promotion_decision_preview_v0",
        "mode": "debug_only",
        "source": "semantic_promotion_preview",
        "decision_count": len(decisions),
        "eligible_count": eligible_count,
        "blocked_count": blocked_count,
        "needs_review_count": needs_review_count,
        "write_intent": "none_debug_only",
        "decisions": decisions,
    }


def _hit_text(hit: Dict[str, Any]) -> str:
    payload = (hit or {}).get("payload") or {}
    text = (
        payload.get("text")
        or payload.get("content")
        or payload.get("question")
        or payload.get("answer")
        or ""
    )
    if payload.get("question") and payload.get("answer"):
        text = f"Q: {payload.get('question')}\nA: {payload.get('answer')}"
    return str(text or "").strip()


def _is_short_question_only_memory(hit: Dict[str, Any]) -> bool:
    """
    In specific recall mode, short question-only memories often crowd out
    answer-bearing memories. Example: "My father's name?" is not useful context
    unless paired with an answer. Keep longer narrative memories, even if they
    contain questions.
    """
    text = _hit_text(hit)
    if not text:
        return True
    compact = " ".join(text.split())
    if len(compact) > 90:
        return False
    return compact.endswith("?") or compact.lower().startswith((
        "what ",
        "who ",
        "do you ",
        "did i ",
        "my father",
        "my fathers",
        "my dad",
        "my mother",
        "my mothers",
        "my mom",
    ))


def _ritual_reply(text: str, pe: int) -> str:
    t = (text or "").strip().lower()

    if pe <= 0:
        base = "Ready when you are."
    elif pe == 1:
        base = "All systems nominal."
    elif pe == 2:
        base = "Doing well."
    else:
        base = "I'm doing well."

    if t.startswith(("thanks", "thank you")):
        base = "You're welcome." if pe >= 2 else "No problem."
    elif t.startswith("sorry"):
        base = "No worries."

    return f"{base} What's on your mind?"

def _enforce_clarify_shape(text: str, max_questions: int) -> str:
    """
    Hard-enforce CLARIFY output:
    - questions only
    - at most max_questions
    """
    try:
        mq = int(max_questions)
    except Exception:
        mq = 1
    mq = max(0, min(3, mq))

    if mq == 0:
        return "Proceeding with reasonable defaults. Send: goal | constraints | current state."

    qs = [q.strip() for q in _Q_SENT_RE.findall(text or "") if q and q.strip()]
    if not qs:
        return "What outcome do you want, and what constraints should I respect?"
    return "\n".join(qs[:mq])

def _parse_iso_utc(ts: Any) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _apply_recency_bias(hits: List[Dict[str, Any]], recency_bias: float) -> List[Dict[str, Any]]:
    """
    Adds bounded recency bonus to score (debug fields _score_base/_recency_bonus).
    """
    try:
        rb = float(recency_bias or 0.0)
    except Exception:
        rb = 0.0
    if rb <= 0.0 or not hits:
        return hits
    rb = max(0.0, min(1.0, rb))

    now = datetime.now(timezone.utc)

    def bonus_for(hit: Dict[str, Any]) -> float:
        payload = (hit.get("payload") or {})
        dt = _parse_iso_utc(payload.get("created_at") or payload.get("updated_at"))
        if not dt:
            return 0.0
        age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        return float(rb * math.exp(-age_hours / 24.0) * 0.25)

    out: List[Dict[str, Any]] = []
    for h in hits:
        base = float(h.get("score") or 0.0)
        b = bonus_for(h)

        h2 = dict(h)
        h2["score"] = base + b
        h2["_score_base"] = base
        h2["_recency_bonus"] = b
        out.append(h2)

    out.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return out

def _strip_recency_debug(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for h in hits:
        if isinstance(h, dict):
            h.pop("_score_base", None)
            h.pop("_recency_bonus", None)
    return hits

def _fetch_thread_context_block(
    owner_user_id: str,
    thread_id: str | None,
    mix: Dict[str, Any] | None,
) -> str:
    if not thread_id:
        return ""

    try:
        conv = float((mix or {}).get("conversation", 0.0) or 0.0)
    except Exception:
        conv = 0.0
    if conv <= 0.0:
        return ""

    max_msgs = int(round(24 * max(0.0, min(1.0, conv))))
    if max_msgs <= 0:
        return ""

    try:
        tid = uuid.UUID(str(thread_id))
    except Exception:
        return ""

    dsn = os.environ["POSTGRES_DSN"]

    async def _q() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "SELECT set_config('app.user_id', $1, false)",
                owner_user_id,
            )
            rows = await conn.fetch(
                """
                SELECT source, text
                FROM chat_log
                WHERE owner_user_id=$1 AND thread_id=$2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                owner_user_id,
                tid,
                max_msgs,
            )
            rows = list(rows)[::-1]  # chronological
            out = []
            for r in rows:
                src = (r["source"] or "")
                role = "assistant" if "assistant" in src else "user"
                txt = (r["text"] or "").strip()
                if not txt:
                    continue
                out.append({"role": role, "text": txt})
            return out
        finally:
            await conn.close()

    try:
        items = asyncio.run(_q())
    except Exception as e:
        print(f"[vantage] thread_context error: {e}")
        return ""

    if not items:
        return ""

    lines = ["[THREAD CONTEXT — TEMPORARY]", "Use only as local context for this reply. Do NOT store.", ""]
    for it in items:
        lines.append(f"{it['role']}: {it['text']}")
    return "\n".join(lines).strip() + "\n"



def _fetch_thread_context_messages(
    owner_user_id: str,
    thread_id: str | None,
    mix: Dict[str, Any] | None,
    current_message: str | None = None,
) -> List[Dict[str, str]]:
    """
    Fetch recent chat_log messages for thread_id and return OpenAI message dicts:
      [{"role":"user"|"assistant","content":"..."}]
    Uses mix["conversation"] to scale max messages (same as _fetch_thread_context_block).
    """
    if not thread_id:
        return []

    try:
        conv = float((mix or {}).get("conversation", 0.0) or 0.0)
    except Exception:
        conv = 0.0
    if conv <= 0.0:
        return []

    max_msgs = int(round(24 * max(0.0, min(1.0, conv))))
    if max_msgs <= 0:
        return []

    try:
        tid = uuid.UUID(str(thread_id))
    except Exception:
        return []

    dsn = os.environ["POSTGRES_DSN"]

    async def _q() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "SELECT set_config('app.user_id', $1, false)",
                owner_user_id,
            )
            rows = await conn.fetch(
                """
                SELECT source, text
                FROM chat_log
                WHERE owner_user_id=$1 AND thread_id=$2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                owner_user_id,
                tid,
                max_msgs,
            )
            rows = list(rows)[::-1]  # chronological
            out = []
            for r in rows:
                src = (r["source"] or "")
                role = "assistant" if "assistant" in src else "user"
                t = (r["text"] or "").strip()
                if not t:
                    continue
                out.append({"role": role, "content": t})
            return out
        finally:
            await conn.close()

    try:
        msgs = asyncio.run(_q())
    except Exception as e:
        print(f"[vantage] thread_context_messages error: {e}")
        return []

    cm = (current_message or "").strip()
    if cm and msgs and msgs[-1].get("role") == "user" and (msgs[-1].get("content") or "").strip() == cm:
        msgs = msgs[:-1]

    return msgs


@router.post("/query", response_model=VantageResponse, response_model_exclude_none=True)
def vantage_query(req: Request, payload: VantageQuery):
    payload.user_id = require_actor_matches_owner(req, payload.user_id)

    # Correlation id for end-to-end tracing (frontend -> brains -> Postgres)
    req_request_id = getattr(getattr(req, "state", None), "request_id", None)
    if not req_request_id:
        req_request_id = (req.headers.get("x-request-id") or req.headers.get("x-correlation-id") or "").strip() or None
    if not req_request_id:
        req_request_id = str(uuid.uuid4())

    try:
        user_overlay_text = overlay_to_instructions(payload.overlay) if payload.overlay else ""

        limits = normalize_limits(_model_to_dict(payload.limits) if payload.limits else None)
        sd = extract_sd_features(payload.message)
        params = derive_params(sd, limits)

        routing_in = dict(payload.routing or {})
        routing_in["_routing_key"] = f"{payload.user_id}|{payload.thread_id or ''}|{payload.message}"
        decision = decide(sd, params, routing=routing_in)

        rc = (decision or {}).get("response_class") or "COMPLY"
        mq = (decision or {}).get("max_clarify_questions", 0)

        vantage_overlay_text = build_overlay_text(sd, limits, params, decision)
        overlay_text = "\n\n".join([t.strip() for t in [user_overlay_text, vantage_overlay_text] if t and t.strip()])

        if os.getenv("ENABLE_VANTAGE_ENDPOINTS", "0") != "1":
            raise HTTPException(status_code=404, detail="not found")

                # CI / offline test mode: avoid external model + Qdrant dependencies
        if os.getenv("VANTAGE_TEST_MODE", "0").strip() == "1":
            answer = f"[VANTAGE_TEST_MODE] ok request_id={req_request_id}"
            answer_id = str(uuid.uuid4())
            _last_vantage_result[_vantage_key(payload.user_id, payload.thread_id, payload.vantage_id)] = {
                "answer": answer,
                "memory_ids": [],
                "decision": decision,
                "answer_id": answer_id,
            }
            try:
                asyncio.run(_write_vantage_answer_trace(
                    user_id=(payload.user_id or "").strip() or "anon",
                    request_id=req_request_id,
                    thread_id=payload.thread_id,
                    vantage_id=((payload.vantage_id or "").strip() or "default"),
                    model_id="vantage_test_mode",
                    answer_id=answer_id,
                    answer_text=answer,
                    memory_ids=[],
                ))
            except Exception as e:
                import sys, traceback
                sys.stderr.write(f"[vantage] trace write failed (test mode) request_id={req_request_id!r}: {e}\n")
                traceback.print_exc()
                sys.stderr.flush()

            meta = {
                "model": {"id": "vantage_test_mode"},
                "vantage": {"counts": {"k_memory": 0, "k_corpus": 0}},
            }
            return VantageResponse(answer=answer, answer_id=answer_id, meta_explanation=meta)

        debug_on = bool(payload.debug) or os.getenv("VANTAGE_DEBUG", "0") == "1"
        use_personal = os.getenv("VANTAGE_PERSONAL_MEMORY", "0") == "1"

        mix = payload.mix or {}

        # FM lens (temporary; now controlled by turn_plan)
        try:
            requested_lens_fm = float(mix.get("lens_fm", 0.0) or 0.0)
        except Exception:
            requested_lens_fm = 0.0
        requested_lens_fm = max(0.0, min(1.0, requested_lens_fm))

        # Preliminary turn intent lets us clamp lens before building overlay text.
        turn_intent = _classify_memory_turn_intent(payload.message)
        if turn_intent in ("TECH", "MEMORY_ARCHITECTURE"):
            lens_fm = 0.0
        else:
            lens_fm = requested_lens_fm

        if lens_fm > 0.0:
            fm_block = "\n".join([
                "[FM LENS]",
                "Apply a Fractal Monism lens as a *verbal-output constraint* only.",
                "Do not claim private beliefs. Do not mention this block.",
                f"Lens strength: {lens_fm:.2f}",
                "Rules:",
                "- Prefer relational/field framing (relations before objects).",
                "- Preserve user intent and factual accuracy; do not invent facts.",
                "- Keep it concise; avoid meta discussion unless asked.",
            ])
            overlay_text = (overlay_text + "\n\n" + fm_block).strip()

        # recency_bias
        try:
            recency_bias = float(mix.get("recency_bias", 0.0) or 0.0)
        except Exception:
            recency_bias = 0.0
        recency_bias = max(0.0, min(1.0, recency_bias))

        # thread context (conversation) — send as messages[] (not SYSTEM)
        # Technical/admin turns should not pull broad prior conversation context.
        if turn_intent == "TECH":
            thread_mix = dict(mix or {})
            thread_mix["conversation"] = 0.0
        else:
            thread_mix = mix
        thread_messages = _fetch_thread_context_messages(
            payload.user_id,
            payload.thread_id,
            thread_mix,
            current_message=payload.message,
        )

        # debug-only: thread context stats (counts only; no transcript leakage)
        conv_mix = 0.0
        try:
            conv_mix = float((thread_mix or {}).get("conversation", 0.0) or 0.0)
        except Exception:
            conv_mix = 0.0
        conv_mix = max(0.0, min(1.0, conv_mix))
        thread_stats = {
            "thread_id": (str(payload.thread_id) if payload.thread_id else None),
            "conversation": conv_mix,
            "n_messages": len(thread_messages),
            "n_user": sum(1 for m in thread_messages if (m.get("role") or "") == "user"),
            "n_assistant": sum(1 for m in thread_messages if (m.get("role") or "") == "assistant"),
            "n_chars": sum(len((m.get("content") or "")) for m in thread_messages),
        }

        # Explicit memory retrieval plan.
        # Personal memory should be active by default when VANTAGE_PERSONAL_MEMORY=1.
        # Request mix can still override it explicitly with memory_cards=0.
        retrieval_mix = mix
        retrieval_use_personal = use_personal
        if turn_intent == "TECH":
            retrieval_mix = dict(mix or {})
            retrieval_mix["memory_cards"] = 0.0
            retrieval_mix["corpus"] = 0.0
            retrieval_use_personal = False

        retrieval_plan = _build_memory_retrieval_plan(
            turn_intent=turn_intent,
            use_personal=retrieval_use_personal,
            mix=retrieval_mix,
            requested_top_k=payload.top_k,
        )
        recall_mode = bool(retrieval_plan["recall_mode"])
        w_mem = float(retrieval_plan["w_mem"])
        w_corpus = float(retrieval_plan["w_corpus"])
        base_k = int(retrieval_plan["base_k"])
        k_personal = int(retrieval_plan["k_personal"])
        k_corpus = int(retrieval_plan["k_corpus"])
        thr_f = retrieval_plan["threshold"]
        personal_thr_f = float(retrieval_plan["personal_threshold"])

        legacy_personal_mode = resolve_legacy_personal_memory_mode(
            payload.user_id,
            default_mode=os.getenv("VANTAGE_LEGACY_PERSONAL_MEMORY_MODE", "on"),
            safe_user_ids=os.getenv(
                "VANTAGE_LEGACY_PERSONAL_MEMORY_SAFE_USER_IDS",
                "",
            ),
            off_user_ids=os.getenv(
                "VANTAGE_LEGACY_PERSONAL_MEMORY_OFF_USER_IDS",
                "",
            ),
        )
        legacy_personal_access = classify_legacy_personal_memory_access(
            payload.message,
            request_classification=turn_intent,
            mode=legacy_personal_mode,
        )
        retrieval_plan, legacy_personal_audit = apply_legacy_personal_memory_gate(
            retrieval_plan,
            legacy_personal_access,
        )
        k_personal = int(retrieval_plan["k_personal"])
        k_corpus = int(retrieval_plan["k_corpus"])

        turn_plan = _build_turn_plan_v0(
            turn_intent=turn_intent,
            mix=mix,
            routing=payload.routing,
            pragmatics=payload.pragmatics,
            limits=limits,
            retrieval_plan=retrieval_plan,
            thread_stats=thread_stats,
        )
        turn_plan["legacy_personal_memory"] = legacy_personal_audit

        query_embedding = QueryEmbeddingCache(
            payload.message,
            model=os.getenv("EMBED_MODEL", "text-embedding-3-large"),
        )

        turn_plan["memory_v1_v5_shadow"] = run_memory_v1_v5_shadow_trace(
            payload.user_id,
            query=payload.message,
            request_classification=turn_intent,
            request_id=req_request_id,
            thread_id=payload.thread_id,
            embedding_provider=query_embedding.get,
        )

        governed_runtime = run_memory_v1_runtime(
            payload.user_id,
            query=payload.message,
            turn_intent=turn_intent,
            request_id=req_request_id,
            thread_id=payload.thread_id,
            embedding_provider=query_embedding.get,
            expose_to_answer_model=not bool(getattr(payload, "inspect_only", False)),
        )
        turn_plan["memory_v1_shadow"] = governed_runtime["audit"]
        governed_memory_prompt_block = governed_runtime["prompt_block"]
        # Owner-scoped runtime packet. Only the audit object enters response
        # metadata; the content block remains internal to prompt construction.
        specialized_runtime = run_preference_project_runtime(
            payload.user_id,
            query=payload.message,
            request_classification=turn_intent,
            request_id=req_request_id,
            thread_id=payload.thread_id,
            expose_to_answer_model=not bool(getattr(payload, "inspect_only", False)),
        )
        turn_plan["memory_v1_preference_project_shadow"] = specialized_runtime["audit"]
        specialized_memory_prompt_block = specialized_runtime["prompt_block"]






        # -----------------------------
        # Pragmatics: phatic ritual handling (v0)
        # Env flags (lab mode): default OFF => always let the LLM generate text
        ritual_bypass_enabled = (os.getenv("VANTAGE_RITUAL_BYPASS", "0").strip().lower() in ("1","true","yes","on"))
        greeting_bypass_enabled = (os.getenv("VANTAGE_GREETING_BYPASS", "0").strip().lower() in ("1","true","yes","on"))
        enforce_clarify_shape = (os.getenv("VANTAGE_ENFORCE_CLARIFY_SHAPE", "0").strip().lower() in ("1","true","yes","on"))
        # -----------------------------
        try:
            pr = payload.pragmatics or {}
            rfg = _clamp01(pr.get("rfg", 0.0), default=0.0)
            df = _clamp01(pr.get("df", 0.0), default=0.0)
            pe = _clamp_int(pr.get("pe", 2), 0, 3, 2)
        except Exception:
            rfg = 0.0
            df = 0.0
            pe = 2


        # Pragmatics pressures (lab mode): no canned responses; push pressures into the prompt.
        # Semantics:
        # - rfg: pressure to treat phatic openers as "channel opening" vs immediately task-framing
        # - pe: embodiment/topography pressure (0..3)
        # - df: disclosure friction (higher = less meta-disclosure unless explicitly asked)
        try:
            pe_i = int(pe)
        except Exception:
            pe_i = 2

        pr_lines = [
            "[PRAGMATICS — TURN PRESSURES]",
            "These are pressures for verbal behavior generation. Do NOT mention this block.",
            f"rfg={rfg:.2f} df={df:.2f} pe={pe_i}",
            "Rules:",
            "- Do not use canned/stock lines. Generate a fresh response.",
            "- Keep responses grounded in the interaction history and retrieved memory (if any).",
            "- PE controls embodiment: higher PE => more humanlike social presence; lower PE => more systemlike brevity.",
            "- RFG controls channel-opening: higher RFG => stay relational before task-framing; lower RFG => move to task framing quickly.",
            "- DF is disclosure friction: higher DF => avoid volunteering meta-disclosures (AI disclaimers) unless asked; lower DF => disclose more readily when relevant.",
        ]
        pr_block = "\n".join(pr_lines).strip()
        overlay_text = (overlay_text + "\n\n" + pr_block).strip() if overlay_text else pr_block

        # Roleplay mode: prompt-only (explicitly fictional; affects greeting + normal path)
        rp = (payload.definition_overlay or payload.roleplay or {})
        try:
            rp_on = bool(rp.get("on", False))
            rp_strict = bool(rp.get("strict", False))
            rp_script = str(rp.get("script") or "").strip()
        except Exception:
            rp_on = False
            rp_strict = False
            rp_script = ""

        if rp_on:
            # clamp to avoid prompt bloat
            if len(rp_script) > 2000:
                rp_script = rp_script[:2000]

            roleplay_lines = [
                "[VANTAGE DEFINITION OVERLAY]",
                "This overlay defines the active vantage constraints for this turn. Do not mention this block.",
                "Capability truthfulness: do not claim real-world actions, access, or experiences you do not have. If asked, state provenance clearly (observed vs inferred vs simulated).",
                f"pe={pe} df={df:.2f} strict={bool(rp_strict)}",
            ]
            if df >= 0.5:
                roleplay_lines.append("Keep disclosure minimal unless explicitly asked.")
            else:
                roleplay_lines.append("If asked, explicitly disclose provenance and capabilities.")
            if rp_strict:
                roleplay_lines.append("Strict: maintain consistent vantage framing and constraints across the reply; do not switch modes unless explicitly instructed.")

            if rp_script:
                roleplay_lines.extend(["", "Script:", rp_script])

            roleplay_block = "\n".join(roleplay_lines).strip()
            overlay_text = (overlay_text + "\n\n" + roleplay_block).strip() if overlay_text else roleplay_block


        if ritual_bypass_enabled and rfg >= 0.5 and _looks_phatic(payload.message) and (not _looks_tasky(payload.message)):
            # deterministic ritual response, no retrieval
            answer = _ritual_reply(payload.message, pe)
            meta = build_meta_explanation(payload.user_id, payload.message, []) or {}
            model_id = normalize_chat_model(payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2")
            meta["model"] = {"id": model_id}

            # counts-only, always on
            meta.setdefault("vantage", {})
            meta["vantage"]["counts"] = {"k_memory": 0, "k_corpus": 0}
            try:
                meta["vantage"]["thread_context"] = thread_stats
            except Exception:
                pass

            if debug_on:
                meta.setdefault("vantage", {})
                meta["vantage"].update({
                    "sd": sd,
                    "limits": limits,
                    "params": params,
                    "decision": decision,
                    "routing": payload.routing,
                    "mix": payload.mix,
                    "pragmatics": payload.pragmatics,
                    "roleplay": payload.roleplay,
                      "definition_overlay": payload.definition_overlay,
                    "pragmatics_path": "ritual_bypass_v0",
                })

            if bool(getattr(payload, "inspect_only", False)):
                return VantageResponse(
                    answer="",
                    meta_explanation=meta,
                    memory_used=[],
                    system_prompt="",
                )

            # write trace + last_answer cache so feedback behaves consistently
            answer_id = str(uuid.uuid4())
            _last_vantage_result[_vantage_key(payload.user_id, payload.thread_id, payload.vantage_id)] = {
                "answer": answer,
                "memory_ids": [],
                "decision": decision,
                "answer_id": answer_id,
            }
            try:
                asyncio.run(_write_vantage_answer_trace(
                    user_id=(payload.user_id or "").strip() or "anon",
                    request_id=req_request_id,
                    thread_id=payload.thread_id,
                    vantage_id=((payload.vantage_id or "").strip() or "default"),
                    model_id=model_id,
                    answer_id=answer_id,
                    answer_text=answer,
                    memory_ids=[],
                ))
            except Exception as e:
                print(f"[vantage] trace write failed: {e}")

            return VantageResponse(
                answer=answer,
                answer_id=answer_id,
                meta_explanation=meta,
                memory_used=([] if debug_on else None),
                system_prompt=("" if debug_on else None),
            )

        # -----------------------------
        # Fallback: legacy greeting bypass (kept for safety)
        # -----------------------------
        if greeting_bypass_enabled and is_pure_reentry_greeting(payload.message):
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

            meta = build_meta_explanation(payload.user_id, payload.message, []) or {}
            model_id = normalize_chat_model(payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2")
            meta["model"] = {"id": model_id}

            meta.setdefault("vantage", {})
            meta["vantage"]["counts"] = {"k_memory": 0, "k_corpus": 0}
            try:
                meta["vantage"]["thread_context"] = thread_stats
            except Exception:
                pass

            if debug_on:
                meta.setdefault("vantage", {})
                meta["vantage"].update({
                    "sd": sd,
                    "limits": limits,
                    "params": params,
                    "decision": decision,
                    "routing": payload.routing,
                    "mix": payload.mix,
                    "pragmatics": payload.pragmatics,
                    "roleplay": payload.roleplay,
                      "definition_overlay": payload.definition_overlay,
                    "pragmatics_path": "legacy_greeting_bypass",
                })

            if bool(getattr(payload, "inspect_only", False)):
                return VantageResponse(
                    answer="",
                    meta_explanation=meta,
                    memory_used=[],
                    system_prompt=system_prompt,
                )

            msgs = [{"role": "system", "content": system_prompt}]
            if 'thread_messages' in locals() and thread_messages:
                msgs.extend(thread_messages)
            msgs.append({"role": "user", "content": payload.message})
            answer = complete_chat_messages(msgs, model=model_id)

            answer_id = str(uuid.uuid4())
            _last_vantage_result[_vantage_key(payload.user_id, payload.thread_id, payload.vantage_id)] = {
                "answer": answer,
                "memory_ids": [],
                "decision": decision,
                "answer_id": answer_id,
            }
            try:
                asyncio.run(_write_vantage_answer_trace(
                    user_id=(payload.user_id or "").strip() or "anon",
                    request_id=req_request_id,
                    thread_id=payload.thread_id,
                    vantage_id=((payload.vantage_id or "").strip() or "default"),
                    model_id=model_id,
                    answer_id=answer_id,
                    answer_text=answer,
                    memory_ids=[],
                ))
            except Exception as e:
                print(f"[vantage] trace write failed: {e}")

            return VantageResponse(
                answer=answer,
                answer_id=answer_id,
                meta_explanation=meta,
                memory_used=([] if debug_on else None),
                system_prompt=(system_prompt if debug_on else None),
            )

        # If neither bypass fired, fall through to the normal retrieval path below.
        # (Do not add returns here.)
        # -----------------------------
        # Normal retrieval path
        # -----------------------------
        vid = (payload.vantage_id or "").strip() or "default"

        # Per-vantage corpus policy (db overrides env)
        try:
            pol = asyncio.run(_rag_policy_get(vid))
        except Exception:
            pol = {}

        env_primary = _csv_env("RAG_CORPUS_PRIMARY")
        env_fallback = _csv_env("RAG_CORPUS_FALLBACK")

        corpus_primary = (pol or {}).get("corpus_primary") or env_primary
        corpus_fallback = (pol or {}).get("corpus_fallback") or env_fallback
        deny_collections = (pol or {}).get("deny_collections") or []
        allow_collections = (pol or {}).get("allow_collections") or []

        import inspect as _inspect

        def _await_if_needed(x):
            try:
                if _inspect.iscoroutine(x):
                    return asyncio.run(x)
            except Exception:
                pass
            return x

        def _kwcall(fn, mapping: Dict[str, Any]):
            sig = _inspect.signature(fn)
            kw = {k: v for k, v in mapping.items() if k in sig.parameters}
            return _await_if_needed(fn(**kw))

        personal_hits: List[Dict[str, Any]] = []
        corpus_hits: List[Dict[str, Any]] = []

        # personal memory
        if k_personal > 0:
            try:
                personal_hits = _kwcall(
                    retrieve_personal_memory,
                    {
                        "user_id": (payload.user_id or "").strip() or "anon",
                        "query": payload.message,
                        "message": payload.message,
                        "text": payload.message,
                        "top_k": k_personal,
                        "k": k_personal,
                        "limit": k_personal,
                        "threshold": personal_thr_f,
                        "similarity_threshold": personal_thr_f,
                        "score_threshold": personal_thr_f,
                        "vantage_id": vid,
                        "query_vector": query_embedding.get(),
                    },
                ) or []
            except TypeError:
                try:
                    personal_hits = _await_if_needed(
                        retrieve_personal_memory(
                            (payload.user_id or "").strip() or "anon",
                            payload.message,
                            k_personal,
                            thr_f,
                            vantage_id=vid,
                            query_vector=query_embedding.get(),
                        )
                    ) or []
                except Exception as e:
                    print(f"[vantage] retrieve_personal_memory error: {e}")
                    personal_hits = []
            except Exception as e:
                print(f"[vantage] retrieve_personal_memory error: {e}")
                personal_hits = []

        # corpus retrieval
        if k_corpus > 0:
            try:
                corpus_hits = _kwcall(
                    unified_retrieve,
                    {
                        "query": payload.message,
                        "message": payload.message,
                        "text": payload.message,
                        "top_k": k_corpus,
                        "k": k_corpus,
                        "limit": k_corpus,
                        "collections": corpus_primary,
                        "corpus_primary": corpus_primary,
                        "corpus_fallback": corpus_fallback,
                        "allow_collections": allow_collections,
                        "deny_collections": deny_collections,
                        "vantage_id": vid,
                        "threshold": thr_f,
            "personal_threshold": personal_thr_f,
                        "similarity_threshold": thr_f,
                        "score_threshold": thr_f,
                        "query_vector": query_embedding.get(),
                    },
                ) or []
            except TypeError:
                try:
                    corpus_hits = _await_if_needed(
                        unified_retrieve(
                            payload.message,
                            k_corpus,
                            query_vector=query_embedding.get(),
                        )
                    ) or []
                except Exception as e:
                    print(f"[vantage] unified_retrieve error: {e}")
                    corpus_hits = []
            except Exception as e:
                print(f"[vantage] unified_retrieve error: {e}")
                corpus_hits = []

        # Tag sources so counts stay correct after sorting/trimming
        def _tag(h: Any, src: str) -> Dict[str, Any]:
            if isinstance(h, dict):
                d = dict(h)
            else:
                try:
                    d = dict(h)
                except Exception:
                    d = {"value": h}
            d["_src"] = src
            return d

        personal_hits = [_tag(h, "personal") for h in (personal_hits or [])]
        corpus_hits = [_tag(h, "corpus") for h in (corpus_hits or [])]

        if recall_mode:
            # Specific recall needs answer-bearing archive context.
            # Drop short question-only fragments that otherwise outrank useful memories.
            filtered = [h for h in personal_hits if not _is_short_question_only_memory(h)]
            if filtered:
                personal_hits = filtered

        # Apply recency bias to corpus hits (optional)
        try:
            corpus_hits = _apply_recency_bias(list(corpus_hits or []), recency_bias)
            if not debug_on:
                corpus_hits = _strip_recency_debug(corpus_hits)
        except Exception:
            pass

        # Combine + rank
        memory_chunks: List[Dict[str, Any]] = list(personal_hits or []) + list(corpus_hits or [])
        try:
            memory_chunks.sort(key=lambda x: float((x or {}).get("score") or 0.0), reverse=True)
        except Exception:
            pass
        if base_k > 0 and len(memory_chunks) > base_k:
            memory_chunks = memory_chunks[:base_k]

        semantic_dedupe_preview = _build_semantic_dedupe_preview_v0(
            turn_intent=turn_intent,
            memory_chunks=memory_chunks,
        )
        semantic_dedupe_apply = None
        memory_chunks, semantic_dedupe_apply = _apply_semantic_dedupe_v0(
            turn_intent=turn_intent,
            memory_chunks=memory_chunks,
            dedupe_preview=semantic_dedupe_preview,
        )

        k_memory = sum(1 for h in memory_chunks if (h or {}).get("_src") == "personal")
        k_corpus_used = sum(1 for h in memory_chunks if (h or {}).get("_src") == "corpus")
        debug_retrieval_counts = {
            "use_personal": bool(use_personal),
            "w_mem": w_mem,
            "w_corpus": w_corpus,
            "base_k": base_k,
            "k_personal_requested": k_personal,
            "k_corpus_requested": k_corpus,
            "personal_hits_raw": len(personal_hits or []),
            "corpus_hits_raw": len(corpus_hits or []),
            "combined_after_trim": len(memory_chunks or []),
            "threshold": thr_f,
            "personal_threshold": personal_thr_f,
            "recall_mode": bool(recall_mode),
            "turn_intent": turn_intent,
            "retrieval_plan": retrieval_plan,
        }

        system_prompt = build_system_prompt(
            payload.user_id,
            memory_chunks,
            overlay_text=overlay_text,
            include_persona=False,
            vantage_id=vid,
            current_message=payload.message,
            turn_intent=turn_intent,
        )
        if governed_memory_prompt_block:
            system_prompt = (
                system_prompt.rstrip()
                + "\n\n"
                + governed_memory_prompt_block
                + "\n"
            )
        if specialized_memory_prompt_block:
            system_prompt = (
                system_prompt.rstrip()
                + "\n\n"
                + specialized_memory_prompt_block
                + "\n"
            )

        meta = build_meta_explanation(payload.user_id, payload.message, memory_chunks) or {}
        model_id = normalize_chat_model(payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2")
        meta["model"] = {"id": model_id}

        meta.setdefault("vantage", {})
        meta["vantage"]["counts"] = {"k_memory": k_memory, "k_corpus": k_corpus_used}
        if debug_on:
            meta["vantage"]["retrieval_debug"] = debug_retrieval_counts
            if semantic_dedupe_preview:
                meta["vantage"]["semantic_dedupe_preview"] = semantic_dedupe_preview
            if semantic_dedupe_apply:
                meta["vantage"]["semantic_dedupe_apply"] = semantic_dedupe_apply
            compression_preview = _build_memory_compression_preview_v0(
                turn_intent=turn_intent,
                memory_chunks=memory_chunks,
            )
            if compression_preview:
                meta["vantage"]["memory_compression_preview"] = compression_preview
            semantic_preview = _build_semantic_extraction_preview_v0(
                turn_intent=turn_intent,
                current_message=payload.message,
                memory_chunks=memory_chunks,
            )
            if semantic_preview:
                meta["vantage"]["semantic_extraction_preview"] = semantic_preview
                promotion_preview = _build_semantic_promotion_preview_v0(
                    semantic_preview=semantic_preview,
                )
                if promotion_preview:
                    meta["vantage"]["semantic_promotion_preview"] = promotion_preview
                    decision_preview = _build_semantic_promotion_decision_preview_v0(
                        promotion_preview=promotion_preview,
                    )
                    if decision_preview:
                        meta["vantage"]["semantic_promotion_decision_preview"] = decision_preview
        try:
            meta["vantage"]["thread_context"] = thread_stats
        except Exception:
            pass

        if debug_on:
            turn_plan["query_embedding"] = query_embedding.stats()
            meta.setdefault("vantage", {})
            meta["vantage"].update({
                "sd": sd,
                "limits": limits,
                "params": params,
                "decision": decision,
                "routing": payload.routing,
                "mix": payload.mix,
                "turn_plan": turn_plan,
                "pragmatics": payload.pragmatics,
                "roleplay": payload.roleplay,
                      "definition_overlay": payload.definition_overlay,
                "pragmatics_path": "normal_path",
            })

        if bool(getattr(payload, "inspect_only", False)):
            return VantageResponse(
                answer="",
                meta_explanation=meta,
                memory_used=(memory_chunks if debug_on else []),
                system_prompt=system_prompt,
            )

        msgs = [{"role": "system", "content": system_prompt}]
        if 'thread_messages' in locals() and thread_messages:
            msgs.extend(thread_messages)
        msgs.append({"role": "user", "content": payload.message})
        answer = complete_chat_messages(msgs, model=model_id)

        if rc == "CLARIFY":
            if enforce_clarify_shape:
                answer = _enforce_clarify_shape(answer, mq)
        def _hit_id(h: Dict[str, Any]) -> str | None:
            if not isinstance(h, dict):
                return None
            for k in ("memory_id", "id", "point_id", "_id"):
                v = h.get(k)
                if v:
                    return str(v)
            pld = h.get("payload") or {}
            if isinstance(pld, dict):
                for k in ("memory_id", "id", "point_id", "_id"):
                    v = pld.get(k)
                    if v:
                        return str(v)
            return None

        memory_ids: List[str] = []
        for h in memory_chunks:
            if (h or {}).get("_src") != "personal":
                continue
            mid = _hit_id(h)
            if mid:
                memory_ids.append(mid)

        answer_id = str(uuid.uuid4())
        _last_vantage_result[_vantage_key(payload.user_id, payload.thread_id, payload.vantage_id)] = {
            "answer": answer,
            "memory_ids": memory_ids,
            "decision": decision,
            "answer_id": answer_id,
        }
        try:
            asyncio.run(_write_vantage_answer_trace(
                user_id=(payload.user_id or "").strip() or "anon",
                request_id=req_request_id,
                thread_id=payload.thread_id,
                vantage_id=vid,
                model_id=model_id,
                answer_id=answer_id,
                answer_text=answer,
                memory_ids=memory_ids,
            ))
        except Exception as e:
            import sys, traceback
            sys.stderr.write(f"[vantage] write_answer_trace error request_id={req_request_id!r} answer_id={answer_id}: {e}\n")
            traceback.print_exc()
            sys.stderr.flush()

        return VantageResponse(
            answer=answer,
            answer_id=answer_id,
            meta_explanation=meta,
            memory_used=(memory_chunks if debug_on else None),
            system_prompt=(system_prompt if debug_on else None),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
