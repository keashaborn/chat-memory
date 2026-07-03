from __future__ import annotations
from collections import OrderedDict
from typing import List, Dict, Any

from .persona_loader import build_persona_block, build_user_instructions_block, build_vantage_preference_cards_block, build_vantage_profile_cards_block
def format_memory_chunks(chunks: List[Dict[str, Any]]) -> str:
    """
    Render retrieved chunks into a compact bullet list.
    Dedupes by content (Q/A or text), even across collections, but preserves provenance.
    """
    if not chunks:
        return ""

    # key: normalized content text -> {"text": original_text, "sources": ["[coll][kind]", ...]}
    merged = OrderedDict()

    for item in chunks:
        payload = item.get("payload", {}) or {}
        coll = (item.get("collection") or "unknown").strip()
        kind = (payload.get("kind") or "").strip()

        text = payload.get("text") or payload.get("content") or ""
        if not text:
            q = (payload.get("question") or "").strip()
            a = (payload.get("answer") or "").strip()
            if q and a:
                text = f"Q: {q}\nA: {a}"
            elif q:
                text = f"Q: {q}"
            elif a:
                text = a

        text = (text or "").strip()
        if not text:
            continue

        prefix = f"[{coll}]"
        if kind:
            prefix += f"[{kind}]"

        key = text.strip().lower()
        if key not in merged:
            merged[key] = {"text": text, "sources": [prefix]}
        else:
            if prefix not in merged[key]["sources"]:
                merged[key]["sources"].append(prefix)

    formatted: List[str] = []
    for it in merged.values():
        sources = it["sources"]
        main = sources[0]
        extra = ""
        if len(sources) > 1:
            extra = " (also: " + ", ".join(sources[1:]) + ")"
        formatted.append(f"- {main} {it['text']}{extra}")

    return "\n".join(formatted)




def _looks_like_technical_admin_turn(text: str | None) -> bool:
    """
    Conservative first-pass technical detector.
    Used only as one input to profile-card gating.
    """
    t = (text or "").lower().strip()
    if not t:
        return False

    technical_cues = (
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
    return any(cue in t for cue in technical_cues)


def _looks_like_specific_recall_turn(text: str | None) -> bool:
    """
    Specific recall should search archive/vector memory, not inject the full
    durable biography/profile card block.
    """
    t = (text or "").lower().strip()
    if not t:
        return False

    recall_cues = (
        "what is my",
        "what was my",
        "who is my",
        "who was my",
        "what did i tell you",
        "what have i told you",
        "do you remember",
        "remind me",
        "my dad's name",
        "my dad’s name",
        "my father's name",
        "my father’s name",
        "my mom's name",
        "my mom’s name",
        "my mother's name",
        "my mother’s name",
        "marker animal",
        "memory qa",
    )
    return any(cue in t for cue in recall_cues)


def _looks_like_broad_profile_turn(text: str | None) -> bool:
    """
    Broad personal integration questions may use profile cards.
    """
    t = (text or "").lower().strip()
    if not t:
        return False

    broad_cues = (
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
    return any(cue in t for cue in broad_cues)


def _should_include_profile_cards(text: str | None, turn_intent: str | None = None) -> bool:
    """
    Runtime mention gate v0:
    - PROFILE_SUMMARY: include profile cards
    - TECH: suppress profile cards
    - SPECIFIC_RECALL: suppress profile cards; archive/vector retrieval should answer
    - GENERAL: fall back to conservative text heuristics for compatibility
    """
    ti = (turn_intent or "").strip().upper()
    if ti == "PROFILE_SUMMARY":
        return True
    if ti in ("TECH", "SPECIFIC_RECALL"):
        return False

    if _looks_like_broad_profile_turn(text):
        return True
    if _looks_like_technical_admin_turn(text):
        return False
    if _looks_like_specific_recall_turn(text):
        return False
    return True

def build_system_prompt(
    user_id: str,
    memory_chunks: List[Dict[str, Any]],
    overlay_text: str = "",
    *,
    include_persona: bool = True,
    include_memory: bool = True,
    memory_header: str = "Relevant context from memory:",
      vantage_id: str | None = None,
      current_message: str | None = None,
      turn_intent: str | None = None,
) -> str:
    """
    Combine:
      - persistent policy block (legacy persona) [optional]
      - temporary overlay text (request-scoped; MUST NOT be stored)
      - retrieved memory context [optional]
    into a single SYSTEM prompt.

    Backwards compatible:
      build_system_prompt(user_id, memory_chunks, overlay_text="...") behaves the same
      as before (persona included, memory included, same header).
    """
    pieces: List[str] = []

    if include_persona:
        persona_block = build_persona_block(user_id, vantage_id=vantage_id)
        if persona_block and persona_block.strip():
            pieces.append(persona_block.strip())

    if overlay_text and overlay_text.strip():
        pieces.append(overlay_text.strip())


    if not include_persona:
        instr = build_user_instructions_block(user_id, vantage_id=vantage_id)
        if instr and instr.strip():
            pieces.append(instr.strip())

    vantage_pref_block = build_vantage_preference_cards_block(user_id, vantage_id=vantage_id)
    if vantage_pref_block and vantage_pref_block.strip():
        pieces.append(vantage_pref_block.strip())

    include_profile_cards = _should_include_profile_cards(current_message, turn_intent=turn_intent)
    if include_profile_cards:
        vantage_profile_block = build_vantage_profile_cards_block(user_id, vantage_id=vantage_id)
        if vantage_profile_block and vantage_profile_block.strip():
            pieces.append(vantage_profile_block.strip())

    if include_memory:
        memory_block = format_memory_chunks(memory_chunks)
        if memory_block and memory_block.strip():
            pieces.append(f"{memory_header}\n{memory_block.strip()}")

    return "\n\n".join(pieces).strip() + "\n"
