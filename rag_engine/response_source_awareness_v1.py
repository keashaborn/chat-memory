from __future__ import annotations

"""Backend-owned, turn-specific source awareness for answer generation."""

from enum import Enum
from typing import Literal


class MemorySourceStatusV1(str, Enum):
    SELECTED = "SELECTED"
    CHECKED_EMPTY = "CHECKED_EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


class WebSourceStatusV1(str, Enum):
    AUTHORIZED_BOUNDED = "AUTHORIZED_BOUNDED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"


class LifeSwitchSourceStatusV1(str, Enum):
    SELECTED = "SELECTED"
    PARTIAL = "PARTIAL"
    CHECKED_EMPTY = "CHECKED_EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


LifeSwitchPreparationStatusV1 = Literal[
    "OFF",
    "TIMEZONE_UNAVAILABLE",
    "EMPTY",
    "SELECTED",
    "PARTIAL",
]


def web_source_status_v1(*, authorized: bool) -> WebSourceStatusV1:
    if type(authorized) is not bool:
        raise TypeError("web authorization status must be boolean")
    return (
        WebSourceStatusV1.AUTHORIZED_BOUNDED
        if authorized
        else WebSourceStatusV1.NOT_AUTHORIZED
    )


def lifeswitch_source_status_v1(
    status: LifeSwitchPreparationStatusV1,
) -> LifeSwitchSourceStatusV1:
    mapping = {
        "OFF": LifeSwitchSourceStatusV1.NOT_APPLICABLE,
        "TIMEZONE_UNAVAILABLE": LifeSwitchSourceStatusV1.UNAVAILABLE,
        "EMPTY": LifeSwitchSourceStatusV1.CHECKED_EMPTY,
        "SELECTED": LifeSwitchSourceStatusV1.SELECTED,
        "PARTIAL": LifeSwitchSourceStatusV1.PARTIAL,
    }
    try:
        return mapping[status]
    except (KeyError, TypeError):
        raise ValueError("unsupported LifeSwitch preparation status") from None


_MEMORY_DESCRIPTIONS = {
    MemorySourceStatusV1.SELECTED: (
        "Saved Memory was checked and relevant saved claims are supplied as "
        "reference data for this response."
    ),
    MemorySourceStatusV1.CHECKED_EMPTY: (
        "Saved Memory was checked, but no relevant saved claims were selected "
        "for this response."
    ),
    MemorySourceStatusV1.NOT_APPLICABLE: (
        "Saved Memory was intentionally not consulted for this response type."
    ),
    MemorySourceStatusV1.UNAVAILABLE: (
        "Saved Memory could not be checked because its runtime was unavailable "
        "for this response."
    ),
}

_WEB_DESCRIPTIONS = {
    WebSourceStatusV1.AUTHORIZED_BOUNDED: (
        "Bounded server-mediated research is authorized for this response; "
        "authorization alone does not prove that a search ran."
    ),
    WebSourceStatusV1.NOT_AUTHORIZED: (
        "No web research was authorized for this response; this turn-specific "
        "state does not mean the application has no research capability."
    ),
}

_LIFESWITCH_DESCRIPTIONS = {
    LifeSwitchSourceStatusV1.SELECTED: (
        "LifeSwitch was checked and relevant structured records are supplied "
        "as reference data for this response."
    ),
    LifeSwitchSourceStatusV1.PARTIAL: (
        "LifeSwitch was checked and partial relevant structured records are "
        "supplied as reference data for this response."
    ),
    LifeSwitchSourceStatusV1.CHECKED_EMPTY: (
        "LifeSwitch was checked, but no relevant structured records were "
        "returned for this response."
    ),
    LifeSwitchSourceStatusV1.NOT_APPLICABLE: (
        "LifeSwitch was not consulted for this response."
    ),
    LifeSwitchSourceStatusV1.UNAVAILABLE: (
        "LifeSwitch could not complete a safe bounded lookup because the "
        "owner timezone was unavailable; no LifeSwitch records are supplied."
    ),
}


def render_base_source_awareness_v1(
    *,
    memory_status: MemorySourceStatusV1,
    web_status: WebSourceStatusV1,
) -> str:
    if not isinstance(memory_status, MemorySourceStatusV1):
        raise TypeError("memory source status must be typed")
    if not isinstance(web_status, WebSourceStatusV1):
        raise TypeError("web source status must be typed")
    return (
        "Source awareness for this response (backend-owned status):\n"
        f"- Saved Memory: {_MEMORY_DESCRIPTIONS[memory_status]}\n"
        f"- Web research: {_WEB_DESCRIPTIONS[web_status]}\n"
        "Use these turn-specific states when describing source access. Answer "
        "naturally and mention a source check only when it matters or the user "
        "asks. Do not say information must appear in the visible chat when "
        "Saved Memory was checked. Do not claim the application globally lacks "
        "Memory, LifeSwitch, or web research because a source is empty, not "
        "applicable, unavailable, or unauthorized for this response. Do not "
        "claim a check or search occurred unless its state says it did."
    )


def append_lifeswitch_source_awareness_v1(
    system_prompt: str,
    *,
    status: LifeSwitchSourceStatusV1,
) -> str:
    if not isinstance(system_prompt, str) or not system_prompt:
        raise TypeError("system prompt must be non-empty")
    if not isinstance(status, LifeSwitchSourceStatusV1):
        raise TypeError("LifeSwitch source status must be typed")
    return (
        f"{system_prompt}\n\nAdditional source status:\n"
        f"- LifeSwitch data: {_LIFESWITCH_DESCRIPTIONS[status]}"
    )


__all__ = [
    "LifeSwitchPreparationStatusV1",
    "LifeSwitchSourceStatusV1",
    "MemorySourceStatusV1",
    "WebSourceStatusV1",
    "append_lifeswitch_source_awareness_v1",
    "lifeswitch_source_status_v1",
    "render_base_source_awareness_v1",
    "web_source_status_v1",
]
