from __future__ import annotations

"""Strict shared identifier contracts for JSON request bodies."""

from typing import Annotated, Any
from uuid import UUID

from pydantic import BeforeValidator


def _canonical_json_uuid(value: Any) -> UUID:
    if type(value) is not str:
        raise ValueError("UUID must be a canonical JSON string")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "UUID must be canonical lowercase hyphenated text"
        ) from exc
    if str(parsed) != value:
        raise ValueError(
            "UUID must be canonical lowercase hyphenated text"
        )
    return parsed


CanonicalJsonUUID = Annotated[
    UUID,
    BeforeValidator(_canonical_json_uuid),
]


__all__ = ["CanonicalJsonUUID"]
