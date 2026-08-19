"""Deterministic transcript tagging for the conversation capability."""

from __future__ import annotations

import os


def infer_vb_tags(text: str, source: str = "user") -> list[str]:
    """Return lightweight verbal-behavior tags for one transcript entry."""
    value = (text or "").lower()
    tags: list[str] = []

    if any(
        word in value
        for word in (
            "can you",
            "could you",
            "please",
            "i want",
            "i need",
            "show me",
            "help me",
        )
    ):
        tags.append("vb_desire:explicit_request")

    if any(
        word in value
        for word in (
            "pattern",
            "field",
            "vantage",
            "identity",
            "system",
            "constraint",
            "fractal",
        )
    ):
        tags.append("vb_ontology:high_abstraction")
    elif any(
        word in value for word in ("thing", "stuff", "that one", "it is like")
    ):
        tags.append("vb_ontology:low_abstraction")

    if any(
        word in value for word in ("i think", "maybe", "sort of", "kinda", "possibly")
    ):
        tags.append("vb_stance:hedged")
    if any(
        word in value for word in ("clearly", "obviously", "definitely", "for sure")
    ):
        tags.append("vb_stance:high_certainty")

    if any(word in value for word in ("because", "so", "therefore", "thus")):
        tags.append("vb_relation:causal")
    if any(word in value for word in ("but", "however", "yet")):
        tags.append("vb_relation:contrast")

    if any(
        word in value
        for word in (
            "lazy",
            "unmotivated",
            "wired this way",
            "i can't help",
            "that's just who i am",
        )
    ):
        tags.append("vb_fiction:mentalistic_term")

    if source != "user":
        tags = [
            tag
            for tag in tags
            if not tag.startswith("vb_desire:")
            and not tag.startswith("vb_fiction:")
        ]
    return tags


def normalize_vb_source(source: str | None) -> str:
    value = (source or "").lower()
    if value == "user" or value.endswith(":user") or "chat:user" in value:
        return "user"
    if (
        value == "assistant"
        or value.endswith(":assistant")
        or "chat:assistant" in value
    ):
        return "assistant"
    return value or "unknown"


def infer_transcript_tags(text: str, source: str = "frontend") -> list[str]:
    """Return the existing ordered heuristic and verbal-behavior tags."""
    value = (text or "").lower()
    extra: list[str] = []

    if any(word in value for word in ("bullet", "bulleted", "outline", "skeleton")):
        extra.append("format:skeleton")
    if any(word in value for word in ("paragraph", "prose", "narrative", "story")):
        extra.append("format:prose")

    if (
        "testing memory" in value
        or "see how memory" in value
        or ("shape" in value and "behavior" in value)
    ):
        extra.append("tone:meta")
    if "design" in value and "rag" in value:
        extra.append("tone:design")

    if any(
        word in value
        for word in (
            "hammer strength",
            "hammer plate",
            "hammer equipment",
            "workout",
            "lift weights",
            "lifting weights",
            "gym routine",
        )
    ):
        extra.append("topic:workout")

    if any(
        word in value
        for word in (
            "fractal monism",
            "fm axioms",
            "fm_",
            "monistic field",
            "undivided field",
            "differentiation",
            "lucifer",
            "self-deception",
        )
    ):
        extra.append("topic:fm")

    if any(
        word in value
        for word in (
            "human vantage",
            "hv axioms",
            "hv-",
            "identity is enacted",
            "agency lives in the next act",
        )
    ):
        extra.append("topic:hv")

    if any(
        word in value
        for word in ("explain", "what is", "why is", "how does", "could you describe")
    ):
        extra.append("intent:explain")
    if any(
        word in value
        for word in ("how do i", "show me how", "step by step", "steps", "instructions")
    ):
        extra.append("intent:instruct")
    if any(word in value for word in ("summary", "summarize", "short version")):
        extra.append("intent:summarize")
    if any(word in value for word in ("analyze", "analysis", "break down")):
        extra.append("intent:analyze")
    if any(word in value for word in ("compare", "difference between", "vs.")):
        extra.append("intent:compare")
    if any(
        word in value
        for word in (
            "i feel",
            "why do i",
            "help me understand",
            "reflect on",
            "what does it mean for me",
            "in my life",
        )
    ):
        extra.append("intent:reflect")
    if any(
        word in value
        for word in ("write", "create", "make a", "generate", "draft", "compose")
    ):
        extra.append("intent:generate")
    if any(word in value for word in ("rewrite", "edit this", "make this better")):
        extra.append("intent:rewrite")
    if any(
        word in value
        for word in ("evaluate", "critique", "what do you think of", "rate this")
    ):
        extra.append("intent:evaluate")

    vb_source = source
    if os.getenv("VB_TAG_SOURCE_NORMALIZE", "0") == "1":
        vb_source = normalize_vb_source(source)
    extra.extend(infer_vb_tags(text, source=vb_source))
    return extra


__all__ = [
    "infer_transcript_tags",
    "infer_vb_tags",
    "normalize_vb_source",
]
