from __future__ import annotations

from typing import Any, Dict, List, Tuple
import os
import re
import requests
import asyncio
import asyncpg
import uuid
from datetime import datetime, timezone
import math
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .openai_client import complete_chat, complete_chat_messages
from .prompt_builder import build_system_prompt
from .role_overlay import overlay_to_instructions
from .retriever_unified import retrieve_personal_memory, unified_retrieve
from .vb_desire_profile import load_latest_vb_desire_profile, vb_desire_bias_map
from .temporal_policy import should_add_reentry_line, build_reentry_line

# Reuse the exact behavior of the current /rag/query path where it matters:
from .rag_router import (
    build_meta_explanation,
    is_pure_reentry_greeting,
    score_personal_hit,  # NOTE: this is the rag_router version (matches current prod behavior)
    classify_feedback_nl,
    extract_tag_from_message,
)

from .vantage_engine import normalize_limits, extract_sd_features, derive_params, decide, build_overlay_text

router = APIRouter()


# ---------- RAG policy (per-vantage corpus selection) ----------
class RagPolicyUpsertReq(BaseModel):
    policy: Dict[str, Any] = {}

def _csv_env(name: str) -> List[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

async def _rag_policy_get(vantage_id: str) -> Dict[str, Any]:
    vid = (vantage_id or "default").strip() or "default"
    dsn = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")
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

async def _rag_policy_upsert(vantage_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    vid = (vantage_id or "default").strip() or "default"
    dsn = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")
    payload_json = json.dumps(policy or {}, ensure_ascii=False)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO vantage_identity.rag_policy(vantage_id, policy)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (vantage_id)
            DO UPDATE SET policy=EXCLUDED.policy, updated_at=now()
            """,
            vid,
            payload_json,
        )
        row = await conn.fetchrow(
            "SELECT policy, created_at, updated_at FROM vantage_identity.rag_policy WHERE vantage_id=$1",
            vid,
        )
    finally:
        await conn.close()

    pol = (row["policy"] if row else {}) or {}
    if isinstance(pol, str):
        try:
            pol = json.loads(pol)
        except Exception:
            pol = {}
    return {
        "policy": dict(pol) if isinstance(pol, dict) else {},
        "created_at": (row["created_at"].isoformat() if row and row["created_at"] else None),
        "updated_at": (row["updated_at"].isoformat() if row and row["updated_at"] else None),
    }

@router.get("/rag_policy")
async def rag_policy_get(vantage_id: str = "default"):
    """
    Get per-vantage RAG policy.
    - env_* are the process defaults from .env
    - db_policy is the stored override for this vantage_id
    - effective_policy is what retrieval should use (db overrides env)
    """
    vid = (vantage_id or "default").strip() or "default"

    env_primary = _csv_env("RAG_CORPUS_PRIMARY")
    env_fallback = _csv_env("RAG_CORPUS_FALLBACK")

    db_policy = await _rag_policy_get(vid)

    effective = {
        "corpus_primary": db_policy.get("corpus_primary") or env_primary,
        "corpus_fallback": db_policy.get("corpus_fallback") or env_fallback,
    }

    # passthrough extras (e.g., topic_overrides, deny_collections, allow_collections, etc.)
    for k, v in (db_policy or {}).items():
        if k not in effective:
            effective[k] = v

    return {
        "status": "ok",
        "vantage_id": vid,
        "env": {"corpus_primary": env_primary, "corpus_fallback": env_fallback},
        "db_policy": db_policy,
        "effective_policy": effective,
    }

@router.post("/rag_policy")
async def rag_policy_upsert(body: RagPolicyUpsertReq, vantage_id: str = "default"):
    """
    Upsert per-vantage RAG policy JSON into Postgres.
    """
    vid = (vantage_id or "default").strip() or "default"
    pol = body.policy or {}
    if not isinstance(pol, dict):
        raise HTTPException(status_code=400, detail="policy must be a JSON object")
    res = await _rag_policy_upsert(vid, pol)
    return {"status": "ok", "vantage_id": vid, **res}

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

    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                """
                INSERT INTO public.vantage_answer_trace(
                  answer_id, user_id, thread_id, vantage_id, model_id,
                  answer_text, answer_text_hash, answer_text_len, memory_ids
                )
                VALUES ($1::uuid, $2, $3::uuid, $4, $5,
                        $6, md5($6), length($6), $7::text[])
                """,
                answer_id,
                user_id,
                tid,
                vantage_id,
                model_id,
                answer_text,
                memory_ids,
            )
        finally:
            await conn.close()
    except Exception as e:
        print(f"[vantage] write_answer_trace error: {e}")


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

def _fetch_thread_context_block(thread_id: str | None, mix: Dict[str, Any] | None) -> str:
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

    dsn = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")

    async def _q() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                """
                SELECT source, text
                FROM chat_log
                WHERE thread_id=$1
                ORDER BY created_at DESC
                LIMIT $2
                """,
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



def _fetch_thread_context_messages(thread_id: str | None, mix: Dict[str, Any] | None, current_message: str | None = None) -> List[Dict[str, str]]:
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

    dsn = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")

    async def _q() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                """
                SELECT source, text
                FROM chat_log
                WHERE thread_id=$1
                ORDER BY created_at DESC
                LIMIT $2
                """,
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
def vantage_query(payload: VantageQuery):
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

        debug_on = bool(payload.debug) or os.getenv("VANTAGE_DEBUG", "0") == "1"
        use_personal = os.getenv("VANTAGE_PERSONAL_MEMORY", "0") == "1"

        mix = payload.mix or {}

        # FM lens (temporary)
        try:
            lens_fm = float(mix.get("lens_fm", 0.0) or 0.0)
        except Exception:
            lens_fm = 0.0
        lens_fm = max(0.0, min(1.0, lens_fm))
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
        thread_messages = _fetch_thread_context_messages(payload.thread_id, mix, current_message=payload.message)

        # debug-only: thread context stats (counts only; no transcript leakage)
        conv_mix = 0.0
        try:
            conv_mix = float((mix or {}).get("conversation", 0.0) or 0.0)
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

        # weights + threshold
        try:
            w_mem = float(mix.get("memory_cards", 0.0))
        except Exception:
            w_mem = 0.0
        try:
            w_corpus = float(mix.get("corpus", 1.0))
        except Exception:
            w_corpus = 1.0

        thr = mix.get("similarity_threshold", None)
        try:
            thr_f = float(thr) if thr is not None else None
        except Exception:
            thr_f = None

        base_k = int(payload.top_k or 5)
        k_personal = 0 if (not use_personal or w_mem <= 0.0) else max(1, int(round(base_k * w_mem)))
        k_corpus = 0 if (w_corpus <= 0.0) else max(1, int(round(base_k * w_corpus)))






        # -----------------------------
        # Pragmatics: phatic ritual handling (v0)
        # Env flags (lab mode): default OFF => always let the LLM generate text
        ritual_bypass_enabled = (os.getenv("VANTAGE_RITUAL_BYPASS", "0").strip().lower() in ("1","true","yes","on"))
        greeting_bypass_enabled = (os.getenv("VANTAGE_GREETING_BYPASS", "0").strip().lower() in ("1","true","yes","on"))
        enforce_clarify_shape = (os.getenv("VANTAGE_ENFORCE_CLARIFY_SHAPE", "0").strip().lower() in ("1","true","yes","on"))
        reentry_prefix_enabled = (os.getenv("VANTAGE_REENTRY_PREFIX", "0").strip().lower() in ("1","true","yes","on"))

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
            model_id = (payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2").strip()
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
            model_id = (payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2").strip()
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
                        "threshold": thr_f,
                        "similarity_threshold": thr_f,
                        "score_threshold": thr_f,
                    },
                ) or []
            except TypeError:
                try:
                    personal_hits = _await_if_needed(
                        retrieve_personal_memory((payload.user_id or "").strip() or "anon", payload.message, k_personal, thr_f)
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
                        "similarity_threshold": thr_f,
                        "score_threshold": thr_f,
                    },
                ) or []
            except TypeError:
                try:
                    corpus_hits = _await_if_needed(unified_retrieve(payload.message, k_corpus)) or []
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

        # Rescore personal hits to match rag_router behavior (best-effort)
        scored_personal: List[Dict[str, Any]] = []
        for h in personal_hits:
            try:
                s = score_personal_hit(payload.message, h)
                h2 = dict(h)
                h2["score"] = float(s)
                scored_personal.append(h2)
            except Exception:
                scored_personal.append(h)
        personal_hits = scored_personal

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

        k_memory = sum(1 for h in memory_chunks if (h or {}).get("_src") == "personal")
        k_corpus_used = sum(1 for h in memory_chunks if (h or {}).get("_src") == "corpus")

        system_prompt = build_system_prompt(
            payload.user_id,
            memory_chunks,
            overlay_text=overlay_text,
            include_persona=False,
            vantage_id=vid,
        )

        meta = build_meta_explanation(payload.user_id, payload.message, memory_chunks) or {}
        model_id = (payload.model or os.getenv("VANTAGE_MODEL") or "gpt-5.2").strip()
        meta["model"] = {"id": model_id}

        meta.setdefault("vantage", {})
        meta["vantage"]["counts"] = {"k_memory": k_memory, "k_corpus": k_corpus_used}
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
                "pragmatics_path": "normal_path",
            })

        reentry_prefix = ""
        if rc != "CLARIFY":
            try:
                temporal = (meta or {}).get("temporal") or {}
                query_tags = (meta or {}).get("query_tags") or []
                if reentry_prefix_enabled and should_add_reentry_line(temporal, payload.message, query_tags=query_tags):
                    reentry_prefix = build_reentry_line(temporal)
            except Exception as e:
                print(f"[temporal] reentry policy error: {e}")

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

        if reentry_prefix:
            answer = reentry_prefix + answer
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
                thread_id=payload.thread_id,
                vantage_id=vid,
                model_id=model_id,
                answer_id=answer_id,
                answer_text=answer,
                memory_ids=memory_ids,
            ))
        except Exception as e:
            print(f"[vantage] write_answer_trace error: {e}")

        return VantageResponse(
            answer=answer,
            answer_id=answer_id,
            meta_explanation=meta,
            memory_used=(memory_chunks if debug_on else None),
            system_prompt=(system_prompt if debug_on else None),
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/feedback")
def vantage_feedback(payload: VantageFeedbackPayload):
    if os.getenv("ENABLE_VANTAGE_ENDPOINTS", "0") != "1":
        raise HTTPException(status_code=404, detail="not found")

    user_id = (payload.user_id or "").strip() or "anon"
    fb_text = (payload.message or "").strip()
    if not fb_text:
        return {"status": "empty"}

    key = _vantage_key(user_id, payload.thread_id, payload.vantage_id)
    last = None

    # Prefer durable trace lookup when answer_id is provided
    if payload.answer_id and _UUID_RE.match(str(payload.answer_id)):
        try:
            dsn = os.getenv("POSTGRES_DSN") or ""
            if dsn.startswith("postgres://"):
                dsn = "postgresql://" + dsn[len("postgres://"):]
            async def _fetch():
                conn = await asyncpg.connect(dsn)
                try:
                    return await conn.fetchrow(
                        "select answer_text, memory_ids from public.vantage_answer_trace "
                        "where answer_id=$1::uuid and user_id=$2",
                        payload.answer_id,
                        user_id,
                    )
                finally:
                    await conn.close()
            if dsn:
                r = asyncio.run(_fetch())
                if r:
                    last = {"answer": r["answer_text"], "memory_ids": list(r["memory_ids"] or [])}
        except Exception as e:
            print(f"[vantage_feedback] trace lookup failed: {e}")

    if last is None:
        last = _last_vantage_result.get(key)

    if not last and key[1]:
        last = _last_vantage_result.get(_vantage_key(user_id, None, payload.vantage_id))
    if not last:
        last = _last_vantage_result.get(_vantage_key(user_id, None, None))
    if not last:
        return {"status": "no_last_answer"}

    memory_ids = last.get("memory_ids") or []

    try:
        signal = classify_feedback_nl(last_answer=last.get("answer", ""), user_message=fb_text)
        tag = extract_tag_from_message(fb_text)
    except Exception as e:
        return {"status": "error", "detail": f"classifier_failed: {e}"}

    if signal == "neutral" and not tag:
        return {"status": "neutral"}

    updated = 0
    if memory_ids:
        for mid in memory_ids:
            out = {"user_id": user_id, "memory_id": mid, "signal": signal}
            if tag:
                out["tag"] = tag
            try:
                r = requests.post("http://127.0.0.1:8088/memory_feedback", json=out, timeout=3.0)
                if r.ok:
                    updated += 1
            except Exception as e:
                print(f"[vantage_feedback] error sending feedback for id={mid}: {e}")

    # Always refresh style card (even if no memory_ids were reinforced)
    try:
        from .persona_loader import quick_persona_refresh
        quick_persona_refresh(user_id)
    except Exception as e:
        print(f"[vantage_feedback] quick_persona_refresh error for user {user_id}: {e}")

    return {"status": "ok", "signal": signal, "tag": tag, "updated": updated, "note": ("no_memory_ids" if not memory_ids else None)}
