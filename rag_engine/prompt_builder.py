from __future__ import annotations
from collections import OrderedDict
from typing import List, Dict, Any

from .persona_loader import build_persona_block, build_user_instructions_block
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


def build_system_prompt(
    user_id: str,
    memory_chunks: List[Dict[str, Any]],
    overlay_text: str = "",
    *,
    include_persona: bool = True,
    include_memory: bool = True,
    memory_header: str = "Relevant context from memory:",
      vantage_id: str | None = None,
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
    if include_memory:
        memory_block = format_memory_chunks(memory_chunks)
        if memory_block and memory_block.strip():
            pieces.append(f"{memory_header}\n{memory_block.strip()}")

    return "\n\n".join(pieces).strip() + "\n"
