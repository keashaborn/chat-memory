# rag_engine/temporal_policy.py

from typing import Dict, Optional


def should_add_reentry_line(
    temporal: Dict,
    user_message: str,
    query_tags: Optional[list[str]] = None,
) -> bool:
    """
    Return True when it would feel natural to acknowledge a long gap.

    V1 policy (conservative):
      - Only consider if bucket is days_gap or long_gap
      - Do NOT interrupt hard task requests (workouts, code, "give me", "steps", etc.)
      - Prefer when message looks conversational / reflective
    """
    bucket = (temporal or {}).get("bucket") or "unknown"
    if bucket not in ("days_gap", "long_gap"):
        return False

    msg = (user_message or "").strip().lower()
    if not msg:
        return False

    # If it's clearly a task request, don't do a re-entry line
    task_markers = [
        "give me", "show me", "write", "generate", "make a", "draft",
        "steps", "step by step", "outline", "bulleted", "bullet",
        "code", "fix", "debug", "implement", "create",
    ]
    if any(m in msg for m in task_markers):
        return False

    # If tags are available and this is a strong "instruct" / "generate" request, skip
    if query_tags:
        qt = set(query_tags)
        if "intent:instruct" in qt or "intent:generate" in qt:
            return False

    # Conversational/reflection markers → allow re-entry line
    conversational_markers = [
        "hey", "hi", "hello", "so", "anyway",
        "i was thinking", "i've been thinking", "i wanted to",
        "i feel", "it's been", "been a while", "catch up",
        "what's been going on", "how have you been",
    ]
    if any(m in msg for m in conversational_markers):
        return True

    # Default: be conservative (don’t inject unless clearly conversational)
    return False


def build_reentry_line(temporal: Dict) -> str:
    """
    Build a short, friendly re-entry line based on the bucket.
    """
    bucket = (temporal or {}).get("bucket") or "unknown"
    if bucket == "days_gap":
        return "It’s been a couple days since we last talked — what’s been going on?\n\n"
    if bucket == "long_gap":
        return "It’s been a little while since we last talked — what’s been going on?\n\n"
    return ""
