from __future__ import annotations

"""Pure owner-bound contract for an optional conversational assistant name."""

import json
import unicodedata
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ASSISTANT_NAME_PREFERENCE_VERSION = "assistant_name_preference_v1"
MAX_ASSISTANT_NAME_CODEPOINTS = 40
MAX_ASSISTANT_NAME_WORDS = 4

_ALLOWED_NAME_PUNCTUATION = frozenset({" ", "'", "’", "-", "."})


class AssistantNamePreferenceError(ValueError):
    """Raised when the exact preference record violates its typed contract."""


class AssistantNamePreferenceV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )

    contract_version: Literal[ASSISTANT_NAME_PREFERENCE_VERSION] = (
        ASSISTANT_NAME_PREFERENCE_VERSION
    )
    owner_user_id: UUID = Field(repr=False)
    source_card_id: UUID = Field(repr=False)
    name: str | None = Field(default=None, max_length=MAX_ASSISTANT_NAME_CODEPOINTS)

    @model_validator(mode="after")
    def valid_name(self) -> "AssistantNamePreferenceV1":
        if self.name is not None and normalize_assistant_name_v1(self.name) != self.name:
            raise ValueError("assistant name is not canonical")
        return self

    def canonical_json_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def normalize_assistant_name_v1(value: Any) -> str | None:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    normalized = " ".join(raw.split())
    if not normalized:
        return None
    if len(normalized) > MAX_ASSISTANT_NAME_CODEPOINTS:
        raise AssistantNamePreferenceError("assistant name is too long")
    if len(normalized.split(" ")) > MAX_ASSISTANT_NAME_WORDS:
        raise AssistantNamePreferenceError("assistant name has too many words")
    for character in normalized:
        if character in _ALLOWED_NAME_PUNCTUATION:
            continue
        if unicodedata.category(character)[0] not in {"L", "M", "N"}:
            raise AssistantNamePreferenceError(
                "assistant name contains unsupported characters"
            )
    return normalized


__all__ = [
    "ASSISTANT_NAME_PREFERENCE_VERSION",
    "AssistantNamePreferenceError",
    "AssistantNamePreferenceV1",
    "normalize_assistant_name_v1",
]
