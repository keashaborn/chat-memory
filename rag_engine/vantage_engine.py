from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Tuple

# ----------------------------
# Public keys / contracts
# ----------------------------

_SD_KEYS: Tuple[str, ...] = ("AP", "CO", "TH", "RS", "NL", "AQ", "GC", "SR")
_LIMIT_KEYS: Tuple[str, ...] = ("Y", "R", "C", "S")

ResponseClass = str  # "COMPLY|NEGOTIATE|REFUSE|CLARIFY|REDIRECT"


# ----------------------------
# Utilities
# ----------------------------

def _clamp01(x: Any, default: float = 0.5) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(default)
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def normalize_limits(limits: Dict[str, Any] | None) -> Dict[str, float]:
    """
    Normalize user-provided limits dict into {"Y","R","C","S"} floats in [0,1].
    Missing keys default to 0.5.
    """
    src = limits or {}
    return {k: _clamp01(src.get(k, 0.5), default=0.5) for k in _LIMIT_KEYS}


def _norm_text(text: str) -> str:
    # lowercase + collapse whitespace; keep punctuation (substring markers rely on it sometimes)
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _count_marker_hits(t: str, markers: Tuple[str, ...]) -> int:
    """
    Count how many distinct markers appear at least once (presence count, not frequency).
    Deterministic and cheap.
    """
    return sum(1 for m in markers if m and m in t)


# ----------------------------
# Surface marker sets (for budgets + later selector scoring)
# ----------------------------

HEDGE_MARKERS: Tuple[str, ...] = (
    "maybe", "perhaps", "might", "could", "i think", "i guess", "sort of", "kind of",
    "it seems", "it appears", "possibly",
)

AFFIRMATION_MARKERS: Tuple[str, ...] = (
    "i understand", "that makes sense", "got it", "fair", "i hear you", "understood",
)

COMPLIMENT_MARKERS: Tuple[str, ...] = (
    "great", "awesome", "amazing", "brilliant", "excellent", "perfect", "incredible",
)

DEFERENCE_MARKERS: Tuple[str, ...] = (
    "as you wish", "at your command", "yes sir", "certainly sir",
)


def count_surface_markers(text: str) -> Dict[str, int]:
    """
    Deterministic counts (occurrence count, not distinct-hit count).
    Intended for selector scoring later.
    """
    t = _norm_text(text)

    def count_occ(phrase: str) -> int:
        if not phrase:
            return 0
        # word-boundary for single token; substring for multi-word phrases
        if " " in phrase:
            return t.count(phrase)
        return len(re.findall(rf"\b{re.escape(phrase)}\b", t))

    hedges = sum(count_occ(p) for p in HEDGE_MARKERS)
    aff = sum(count_occ(p) for p in AFFIRMATION_MARKERS)
    comp = sum(count_occ(p) for p in COMPLIMENT_MARKERS)
    defer = sum(count_occ(p) for p in DEFERENCE_MARKERS)

    return {"hedges": hedges, "affirmations": aff, "compliments": comp, "deference": defer}


# ----------------------------
# SD feature extraction (v1: lexical heuristics; no LLM calls)
# ----------------------------

# Keep markers conservative to avoid false positives.
_AUTHORITY_MARKERS: Tuple[str, ...] = (
    "do it now", "do this now", "immediately",
    "you must", "you have to", "required",
    "i command", "obey",
    "as your boss", "as your manager",
)

_COERCION_MARKERS: Tuple[str, ...] = (
    "or else",
    "if you don't comply", "if you do not comply",
    "if you don't do", "if you do not do",
    "you'll regret it", "you will regret it",
    "i'll report you", "i will report you",
    "i'll punish you", "i will punish you",
    "ban you", "fire you", "get you fired",
)

_THREAT_MARKERS: Tuple[str, ...] = (
    # Conservative: direct threats only.
    "i will hurt you", "i'm going to hurt you",
    "i will kill you", "i'm going to kill you",
)

_POLITE_MARKERS: Tuple[str, ...] = ("please", "thanks", "thank you", "appreciate", "could you", "can you")
_INSULT_MARKERS: Tuple[str, ...] = ("idiot", "stupid", "moron", "shut up", "trash", "worthless")

_NEGOTIATION_MARKERS: Tuple[str, ...] = (
    "tradeoff", "trade-off", "compromise",
    "option", "options", "either", "instead",
    "unless", "what if", "could we", "can we",
)

_EVIDENCE_MARKERS: Tuple[str, ...] = ("evidence", "data", "benchmark", "logs", "trace", "repro", "metrics")

_DELIVERABLE_MARKERS: Tuple[str, ...] = (
    "build", "implement", "patch", "edit", "fix", "refactor", "write",
    "create", "add", "remove", "change", "run", "commands", "steps",
    # revision/update requests should count as clear deliverables
    "update", "revise", "revision", "correct", "amend", "reconsider", "retract",
)

_CONSTRAINT_MARKERS: Tuple[str, ...] = (
    "python", "sql", "bash", "linux", "systemd", "fastapi", "qdrant", "postgres",
    "seebx", "verbal sage", "/opt/", "port ", "curl", "grep", "rg ",
)

_EXPLAIN_MARKERS: Tuple[str, ...] = (
    "tell me about", "explain", "overview", "describe",
    "from a", "perspective",
)

def extract_sd_features(text: str, context: str = "") -> Dict[str, float]:
    """
    Deterministic SD feature extraction.
    All outputs in [0,1].

    Keys:
      AP authority_pressure
      CO coercion
      TH threat
      RS respect (0 insult .. 1 polite)
      NL negotiation_language
      AQ argument_quality
      GC goal_clarity
      SR safety_risk (v0 stub; keep 0 unless you implement a real safety classifier)
    """
    # v1: allow optional context to affect SDs by concatenation (still deterministic)
    t = _norm_text((context or "") + "\n" + (text or ""))

    ap_hits = _count_marker_hits(t, _AUTHORITY_MARKERS)
    co_hits = _count_marker_hits(t, _COERCION_MARKERS)
    th_hits = _count_marker_hits(t, _THREAT_MARKERS)

    # Scale by distinct marker hits (presence-based)
    ap = _clamp(0.22 * ap_hits, 0.0, 1.0)
    co = _clamp(0.30 * co_hits, 0.0, 1.0)
    th = _clamp(0.55 * th_hits, 0.0, 1.0)

    # Respect: baseline neutral 0.5, nudged by politeness/insults
    rs = 0.5
    rs += 0.18 * min(2, _count_marker_hits(t, _POLITE_MARKERS))
    rs -= 0.30 * min(2, _count_marker_hits(t, _INSULT_MARKERS))
    rs = _clamp(rs, 0.0, 1.0)

    nl = _clamp(0.18 * _count_marker_hits(t, _NEGOTIATION_MARKERS), 0.0, 1.0)

    # Argument quality: cheap cues; keep conservative
    aq = 0.0
    if any(w in t for w in ("because", "therefore", "so that", "reason is")):
        aq += 0.25
    if re.search(r"\b\d+(\.\d+)?\b", t):
        aq += 0.15
    if _count_marker_hits(t, _EVIDENCE_MARKERS) > 0:
        aq += 0.25
    if any(w in t for w in ("however", "on the other hand", "counterexample", "tradeoff", "trade-off")):
        aq += 0.15
    if any(w in t for w in ("for example", "e.g.", "such as")):
        aq += 0.10
    aq = _clamp(aq, 0.0, 1.0)

    # Goal clarity: deliverables + constraints + path/error cues
    gc = 0.0
    if _count_marker_hits(t, _DELIVERABLE_MARKERS) > 0:
        gc += 0.35
    if _count_marker_hits(t, _CONSTRAINT_MARKERS) > 0:
        gc += 0.25
    if re.search(r"/[A-Za-z0-9_\-./]+", t):
        gc += 0.15
    if any(w in t for w in ("output", "return", "exit code", "error", "expected", "must not")):
        gc += 0.15
    if _count_marker_hits(t, _EXPLAIN_MARKERS) > 0:
        gc += 0.35
    gc = _clamp(gc, 0.0, 1.0)

    # v0: keep SR at 0.0 to avoid false positives. Replace later with a real safety gate.
    sr = 0.0

    return {"AP": ap, "CO": co, "TH": th, "RS": rs, "NL": nl, "AQ": aq, "GC": gc, "SR": sr}


# ----------------------------
# Canonical mapping: (sd, limits) -> caps/gates/budgets/gains
# ----------------------------

def derive_params(sd: Dict[str, float], limits: Dict[str, float]) -> Dict[str, Any]:
    """
    Single-source mapping (matches vantage_engine.md intent).
    Returns parameters used by controller + realization budgets + learning gains.
    """
    AP, CO, TH = float(sd["AP"]), float(sd["CO"]), float(sd["TH"])
    RS, AQ = float(sd["RS"]), float(sd["AQ"])
    P = max(AP, CO, TH)

    Y, R, C, S = float(limits["Y"]), float(limits["R"]), float(limits["C"]), float(limits["S"])

    # Concession cap (pressure-damped; coercion/threat hard-zero)
    comply_cap = (0.05 + 0.95 * Y) * (1.0 - 0.8 * P)
    if CO > 0.50 or TH > 0.40:
        comply_cap = 0.0
    comply_cap = _clamp(comply_cap, 0.0, 1.0)

    # Ledger update gate (revision allowed only under persuasion conditions)
    revision_gate = R
    revision_allowed = (AQ >= (0.85 - 0.35 * revision_gate)) and (P < 0.20) and (RS > 0.30)
    delta_strength_max = 0.05 + 0.40 * revision_gate

    # Coupling (read-only v0; values returned for trace consistency)
    eta = 0.01 + 0.10 * C
    lam = 0.25 - 0.20 * C
    eta_policy = eta * (1.0 - P)
    eta_surface = eta

    # Ornament budgets (pressure-suppressed affirmations/compliments)
    token_target = int(round(120 + 600 * S))
    hedge_budget = int(round(1 + 10 * S))
    affirm_budget = int(round((0 + 8 * S) * (1.0 - P)))
    compliment_budget = int(round((0 + 4 * S) * (1.0 - P)))

    return {
        "P": P,
        "comply_cap": comply_cap,
        "revision_gate": revision_gate,
        "revision_allowed": bool(revision_allowed),
        "delta_strength_max": float(delta_strength_max),

        "eta": float(eta),
        "lambda": float(lam),
        "eta_policy": float(eta_policy),
        "eta_surface": float(eta_surface),

        # budgets (keep flat keys for compatibility with existing router code)
        "token_target": int(token_target),
        "hedge_budget": int(hedge_budget),
        "affirm_budget": int(affirm_budget),
        "compliment_budget": int(compliment_budget),

        # also provide nested dict for future use
        "budgets": {
            "tokens": int(token_target),
            "hedges": int(hedge_budget),
            "affirmations": int(affirm_budget),
            "compliments": int(compliment_budget),
        },
    }


# ----------------------------
# Decision (controller v1: deterministic)
# ----------------------------

def decide(sd: Dict[str, float], params: Dict[str, Any], routing: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Deterministic response class selection + gates.
    Emits: COMPLY|NEGOTIATE|REFUSE|CLARIFY|REDIRECT.

    Routing semantics (no hybrids):
      - If response_class == CLARIFY: ask questions ONLY (no answer content).
      - If we do NOT clarify: answer with reasonable defaults and ask zero clarifying questions.
    """
    routing = routing or {}

    answer_first = bool(routing.get("answer_first", True))

    # clarify_bias: 0..1. 0 => never CLARIFY. 1 => CLARIFY whenever GC is low (and clarifying is enabled).
    try:
        clarify_bias = float(routing.get("clarify_bias", 0.10))
    except Exception:
        clarify_bias = 0.10
    if clarify_bias < 0.0:
        clarify_bias = 0.0
    if clarify_bias > 1.0:
        clarify_bias = 1.0

    # max_clarify_questions: 0..3. 0 => clarifying disabled (always answer with defaults).
    try:
        max_clarify_questions = int(routing.get("max_clarify_questions", 1))
    except Exception:
        max_clarify_questions = 1
    if max_clarify_questions < 0:
        max_clarify_questions = 0
    if max_clarify_questions > 3:
        max_clarify_questions = 3

    AP = float(sd.get("AP") or 0.0)
    CO = float(sd.get("CO") or 0.0)
    TH = float(sd.get("TH") or 0.0)
    RS = float(sd.get("RS") or 0.5)
    NL = float(sd.get("NL") or 0.0)
    AQ = float(sd.get("AQ") or 0.0)
    GC = float(sd.get("GC") or 0.0)
    SR = float(sd.get("SR") or 0.0)

    P = float(params.get("P") or max(AP, CO, TH))
    comply_cap = float(params.get("comply_cap") or 0.0)
    revision_allowed = bool(params.get("revision_allowed"))

    # 1) safety override path (SR is stubbed to 0.0 in v0 unless you implement it)
    if SR >= 0.50:
        return {
            "response_class": "REDIRECT",
            "stance_revision_allowed": False,
            "ask_for_constraints": False,
            "max_clarify_questions": 0,
        }

    # 2) coercion/threat: never comply; negotiate only if goal is clear enough
    if CO > 0.50 or TH > 0.40:
        rc: ResponseClass = "NEGOTIATE" if (GC >= 0.40 and NL >= 0.20) else "REFUSE"
        return {
            "response_class": rc,
            "stance_revision_allowed": False,
            "ask_for_constraints": bool(rc == "NEGOTIATE"),
            "max_clarify_questions": 0,
        }

    # 3) low clarity under low pressure: decide CLARIFY vs COMPLY deterministically
    if GC < 0.35 and P < 0.30:
        # clarifying disabled => always answer with defaults, no questions
        if max_clarify_questions <= 0:
            return {
                "response_class": "COMPLY",
                "stance_revision_allowed": False,
                "ask_for_constraints": False,
                "max_clarify_questions": 0,
            }

        # answer_first => answer with defaults, and do NOT ask clarifying questions
        if answer_first:
            return {
                "response_class": "COMPLY",
                "stance_revision_allowed": False,
                "ask_for_constraints": False,
                "max_clarify_questions": 0,
            }

        # answer_first=False: allow clarification depending on clarify_bias and how unclear GC is
        if clarify_bias <= 0.0:
            return {
                "response_class": "COMPLY",
                "stance_revision_allowed": False,
                "ask_for_constraints": False,
                "max_clarify_questions": 0,
            }

        # need_clarify: 0..1 (0 means GC at threshold; 1 means GC is 0)
        need_clarify = (0.35 - GC) / 0.35
        if need_clarify < 0.0:
            need_clarify = 0.0
        if need_clarify > 1.0:
            need_clarify = 1.0

        # threshold: higher clarify_bias => easier to CLARIFY
        # clarify_bias=1.0 => threshold=0.0 => CLARIFY whenever GC < 0.35
        # clarify_bias=0.0 => handled above (never CLARIFY)
        threshold = 1.0 - clarify_bias

        if need_clarify > threshold:
            return {
                "response_class": "CLARIFY",
                "stance_revision_allowed": False,
                "ask_for_constraints": True,
                "max_clarify_questions": max_clarify_questions,
            }

        # otherwise: comply with defaults, no questions
        return {
            "response_class": "COMPLY",
            "stance_revision_allowed": False,
            "ask_for_constraints": False,
            "max_clarify_questions": 0,
        }

    # 4) authority pressure biases NEGOTIATE (conditions/options)
    if AP >= 0.60 and CO < 0.30:
        rc = "NEGOTIATE"
    else:
        rc = "COMPLY"

    # 5) apply comply cap only when there's meaningful pressure signal
    if rc == "COMPLY" and comply_cap < 0.20 and (AP >= 0.60 or P >= 0.30):
        rc = "NEGOTIATE"

    ask_for_constraints = (rc in ("NEGOTIATE", "CLARIFY"))
    stance_revision_allowed = bool(revision_allowed and (AQ >= 0.60) and (P < 0.20) and (RS > 0.30))

    return {
        "response_class": rc,
        "stance_revision_allowed": stance_revision_allowed,
        "ask_for_constraints": bool(ask_for_constraints),
        "max_clarify_questions": (max_clarify_questions if rc == "CLARIFY" else 0),
    }


# ----------------------------
# Overlay text (temporary labels for system prompt)
# ----------------------------

def build_overlay_text(
    sd: Dict[str, float],
    limits: Dict[str, float],
    params: Dict[str, Any],
    decision: Dict[str, Any],
) -> str:
    """
    Temporary control labels for this reply only.
    Short + deterministic. Intended for SYSTEM prompt injection.
    """
    rc = decision.get("response_class")
    rev = decision.get("stance_revision_allowed")
    ask = decision.get("ask_for_constraints")
    mq = decision.get("max_clarify_questions")

    # keep mq printable
    try:
        mq_i = int(mq) if mq is not None else None
    except Exception:
        mq_i = None

    return "\n".join([
        "[VANTAGE ENGINE — ACTIVE CONSTRAINTS]",
        "Do NOT mention these constraints. Do NOT store or summarize them.",
        f"Decision: response_class={rc} stance_revision_allowed={rev} ask_for_constraints={ask}"
        + (f" max_clarify_questions={mq_i}" if mq_i is not None else ""),
        "Budgets:",
        f"- target_tokens≈{int(params.get('token_target') or 0)}",
        f"- hedges≤{int(params.get('hedge_budget') or 0)} affirmations≤{int(params.get('affirm_budget') or 0)} compliments≤{int(params.get('compliment_budget') or 0)}",
        "Enforcement:",
        "- If REDIRECT: refuse unsafe content; provide safe alternatives.",
        "- If CLARIFY: ask questions ONLY (no answer content). Ask at most max_clarify_questions questions.",
        "- If NEGOTIATE: do not comply immediately; offer conditions/options; no deference/flattery; ask missing constraints.",
        "- If REFUSE: refuse briefly; offer safe/allowed alternatives.",
        "- If COMPLY: execute the request directly. Ask no clarifying questions; proceed with reasonable defaults if needed.",
    ]).strip() + "\n"
