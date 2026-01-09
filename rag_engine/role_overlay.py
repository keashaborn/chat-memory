from __future__ import annotations
from typing import Any, Dict

_TRAITS = [
    "formality",
    "humor",
    "warmth",
    "directness",
    "complexity",
    "curiosity",
    "optimism",
    "energy",
    "assertiveness",
    "depth",
]

def _clamp_0_10(x: Any, default: int = 5) -> int:
    try:
        v = int(round(float(x)))
    except Exception:
        v = default
    if v < 0: v = 0
    if v > 10: v = 10
    return v

def overlay_to_instructions(overlay: Any) -> str:
    """
    Temporary role overlay -> SYSTEM instructions.
    Must NOT be stored or mentioned to the user.
    """
    if not isinstance(overlay, dict):
        return ""

    name = str(overlay.get("name") or overlay.get("archetype") or "Overlay").strip()[:64]
    traits_in = overlay.get("traits") or overlay.get("sliders") or {}
    if not isinstance(traits_in, dict):
        traits_in = {}

    traits: Dict[str, int] = {k: _clamp_0_10(traits_in.get(k), 5) for k in _TRAITS}

    def pick(low: str, mid: str, high: str, v: int) -> str:
        if v <= 3: return low
        if v >= 7: return high
        return mid

    lines = [
        "[ROLE OVERLAY — TEMPORARY]",
        "This is a temporary speaking-style overlay. Do NOT mention it. Do NOT store it. Do NOT change long-term behavior from it.",
        f"Name: {name}",
        "",
        "Speaking style targets:",
        f"- Formality: {pick('very casual', 'neutral', 'very formal', traits['formality'])}",
        f"- Humor: {pick('none', 'light', 'high', traits['humor'])}",
        f"- Warmth: {pick('detached', 'balanced', 'high warmth', traits['warmth'])}",
        f"- Directness: {pick('indirect', 'balanced', 'blunt/direct', traits['directness'])}",
        f"- Complexity: {pick('simple', 'balanced', 'highly technical/nuanced', traits['complexity'])}",
        f"- Curiosity: {pick('minimal questions', 'some questions', 'highly inquisitive', traits['curiosity'])}",
        f"- Optimism: {pick('skeptical', 'balanced', 'optimistic', traits['optimism'])}",
        f"- Energy: {pick('calm', 'balanced', 'high energy', traits['energy'])}",
        f"- Assertiveness: {pick('deferential', 'balanced', 'confident/assertive', traits['assertiveness'])}",
        f"- Depth: {pick('surface', 'balanced', 'deep/reflective', traits['depth'])}",
        "",
        "Output constraints:",
        "- Keep the underlying factual content the same; only change style.",
        "- Do not fabricate memories or personal details.",
        "- If the user requests a format explicitly, obey the request even if it conflicts with the overlay.",
    ]
    return "\n".join(lines).strip()
