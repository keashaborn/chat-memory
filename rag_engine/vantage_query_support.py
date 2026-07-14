from __future__ import annotations

import re
from typing import Any, Dict, List

import requests

from .gravity import compute_misalignment, load_gravity_profile
from .retriever_unified import infer_query_tags


def is_pure_reentry_greeting(message: str) -> bool:
    """Return true only for short greeting/re-entry messages with no task."""
    msg = (message or "").strip().lower()
    if not msg or len(msg) > 40:
        return False

    if re.match(r"^(hey|hi|hello|yo)\b", msg):
        pass
    elif msg.startswith("i'm back") or msg.startswith("im back") or msg.startswith("back again"):
        pass
    else:
        return False

    request_markers = [
        "give me",
        "show me",
        "help me",
        "explain",
        "how do",
        "steps",
        "outline",
        "bulleted",
        "write",
        "generate",
        "tell me",
    ]
    return not any(marker in msg for marker in request_markers)


def build_meta_explanation(
    user_id: str,
    message: str,
    memory_chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the current Vantage response/retrieval diagnostic metadata."""
    query_tags = set(infer_query_tags((message or "").strip()))
    total_pos = 0
    total_neg = 0
    topic_tags: set[str] = set()

    for memory in memory_chunks:
        payload = memory.get("payload") or {}
        feedback = payload.get("feedback") or {}
        total_pos += int(feedback.get("positive_signals") or 0)
        total_neg += int(feedback.get("negative_signals") or 0)
        for tag in payload.get("tags") or []:
            if isinstance(tag, str) and tag.startswith("topic:"):
                topic_tags.add(tag.split(":", 1)[1])

    format_bits: list[str] = []
    if "format:skeleton" in query_tags:
        format_bits.append("user explicitly asked for skeleton / outline style")
    if "format:prose" in query_tags:
        format_bits.append("user explicitly asked for narrative / prose style")

    summary_parts: list[str] = []
    if format_bits:
        summary_parts.append("format: " + "; ".join(format_bits))
    if total_pos or total_neg:
        summary_parts.append(f"related memories have feedback +{total_pos} / -{total_neg}")
    if topic_tags:
        summary_parts.append("topics seen in used memories: " + ", ".join(sorted(topic_tags)))

    personal = [m for m in memory_chunks if m.get("collection") == "memory_raw"]
    format_counts = {"format:skeleton": 0, "format:prose": 0}
    for memory in personal:
        tags = (memory.get("payload") or {}).get("tags") or []
        for tag in tags:
            if tag in format_counts:
                format_counts[tag] += 1

    if format_counts["format:skeleton"] > format_counts["format:prose"]:
        historical_format = "skeleton-leaning"
    elif format_counts["format:prose"] > format_counts["format:skeleton"]:
        historical_format = "prose-leaning"
    else:
        historical_format = "undetermined"

    if "format:skeleton" in query_tags:
        current_format = "skeleton"
    elif "format:prose" in query_tags:
        current_format = "prose"
    else:
        current_format = "unspecified"

    if current_format == "skeleton" and historical_format == "prose-leaning":
        format_shift = "user_now_requesting_skeleton_vs_historical_prose"
    elif current_format == "prose" and historical_format == "skeleton-leaning":
        format_shift = "user_now_requesting_prose_vs_historical_skeleton"
    else:
        format_shift = "aligned_or_unknown"

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

    temporal: Dict[str, Any] = {
        "seconds_since_last_user_message": None,
        "bucket": "unknown",
    }
    try:
        response = requests.get(f"http://127.0.0.1:8088/temporal/{user_id}", timeout=1.0)
        if response.ok:
            body = response.json() or {}
            temporal["seconds_since_last_user_message"] = body.get(
                "seconds_since_last_user_message"
            )
            temporal["bucket"] = body.get("bucket") or "unknown"
    except Exception as exc:
        print(f"[temporal] error fetching temporal info: {exc}")

    return {
        "query_tags": sorted(query_tags),
        "feedback_summary": {"positive": total_pos, "negative": total_neg},
        "topic_tags": sorted(topic_tags),
        "summary": " ".join(summary_parts),
        "consistency": {
            "historical_format": historical_format,
            "current_request_format": current_format,
            "format_shift": format_shift,
        },
        "gravity": {
            "misalignment": misalignment,
            "label": misalignment_label,
        },
        "temporal": temporal,
    }
