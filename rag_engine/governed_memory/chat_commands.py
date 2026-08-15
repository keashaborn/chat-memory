from __future__ import annotations

"""Pure, deterministic parsers for explicit owner memory lifecycle commands."""

from dataclasses import dataclass
import re
import unicodedata


MAX_MESSAGE_BYTES = 2_048

_RETRACTION_SENTENCE_RE = re.compile(
    r"(?:^|[.!]\s+)(?:please\s+)?retract\s+(?:the|my)\s+"
    r"(?P<target>[^.!?\n]{1,160}?)\s+preference\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
_CORRECTION_SENTENCE_RE = re.compile(
    r"^\s*my\s+"
    r"(?P<label>(?:preferred|favorite)\s+[^,!?\n]{1,120}?)\s+"
    r"is\s+now\s+(?P<replacement>[^,!?\n]{1,160}?)\s*,\s*"
    r"replacing\s+(?P<previous>[^,!?\n]{1,160}?)\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
_LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExplicitPreferenceCorrectionCommandV1:
    preference_label: str
    replacement_literal: str
    previous_literal: str


def normalized_chat_text_v1(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def normalized_preference_value_v1(value: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", normalized_chat_text_v1(value), count=1)


def _bounded_message(message: object) -> str | None:
    if not isinstance(message, str):
        return None
    try:
        size = len(message.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if not message or size > MAX_MESSAGE_BYTES or "\x00" in message:
        return None
    if "?" in message or "\n" in message or "\r" in message:
        return None
    return unicodedata.normalize("NFC", message)


def explicit_preference_retraction_target_v1(message: object) -> str | None:
    """Return a normalized retraction target, or None for an ordinary turn."""

    bounded = _bounded_message(message)
    if bounded is None:
        return None
    matches = tuple(_RETRACTION_SENTENCE_RE.finditer(bounded.strip()))
    if len(matches) != 1:
        return None
    target = normalized_chat_text_v1(matches[0].group("target"))
    if not target or len(target) > 160:
        return None
    return target


def explicit_preference_correction_command_v1(
    message: object,
) -> ExplicitPreferenceCorrectionCommandV1 | None:
    """Parse only a complete `now X, replacing Y` owner preference command."""

    bounded = _bounded_message(message)
    if bounded is None:
        return None
    matched = _CORRECTION_SENTENCE_RE.fullmatch(bounded)
    if matched is None:
        return None
    label = " ".join(matched.group("label").split())
    replacement = " ".join(matched.group("replacement").split())
    previous = " ".join(matched.group("previous").split())
    if not all((label, replacement, previous)):
        return None
    if not normalized_preference_value_v1(replacement):
        return None
    if not normalized_preference_value_v1(previous):
        return None
    return ExplicitPreferenceCorrectionCommandV1(
        preference_label=label,
        replacement_literal=replacement,
        previous_literal=previous,
    )


__all__ = [
    "ExplicitPreferenceCorrectionCommandV1",
    "explicit_preference_correction_command_v1",
    "explicit_preference_retraction_target_v1",
    "normalized_chat_text_v1",
    "normalized_preference_value_v1",
]
