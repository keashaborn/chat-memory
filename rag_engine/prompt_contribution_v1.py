from __future__ import annotations

"""Typed, provider-neutral prompt contributions.

This module is intentionally isolated from live prompt construction.  A future
adapter may create these values only after the owning subsystem has completed
its own authorization and validation boundary.
"""

import hashlib
import math
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONTRIBUTION_VERSION = "prompt_contribution_v1"
TOKEN_ESTIMATOR_VERSION = "prompt_contribution_utf8_bytes_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,159}$")


class PromptContributionError(RuntimeError):
    """Fail-closed error at the prompt-contribution boundary."""


class PromptContributionKind(str, Enum):
    SAFETY = "safety"
    RUNTIME_POLICY = "runtime_policy"
    PRESENTATION = "presentation"
    MEMORY = "memory"
    STRUCTURED_DATA = "structured_data"
    CORPUS = "corpus"
    FRACTAL_MONISM = "fractal_monism"


class PromptContributionAuthority(str, Enum):
    BACKEND_INSTRUCTION = "backend_instruction"
    PRESENTATION_PREFERENCE = "presentation_preference"
    REFERENCE_DATA = "reference_data"


KIND_CONTRACT: dict[
    PromptContributionKind, tuple[int, PromptContributionAuthority]
] = {
    PromptContributionKind.SAFETY: (10, PromptContributionAuthority.BACKEND_INSTRUCTION),
    PromptContributionKind.RUNTIME_POLICY: (
        20,
        PromptContributionAuthority.BACKEND_INSTRUCTION,
    ),
    PromptContributionKind.PRESENTATION: (
        30,
        PromptContributionAuthority.PRESENTATION_PREFERENCE,
    ),
    PromptContributionKind.MEMORY: (40, PromptContributionAuthority.REFERENCE_DATA),
    PromptContributionKind.STRUCTURED_DATA: (
        50,
        PromptContributionAuthority.REFERENCE_DATA,
    ),
    PromptContributionKind.CORPUS: (60, PromptContributionAuthority.REFERENCE_DATA),
    PromptContributionKind.FRACTAL_MONISM: (
        70,
        PromptContributionAuthority.REFERENCE_DATA,
    ),
}


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def canonical_json_bytes(value: Any) -> bytes:
    import json

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def estimate_text_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)


class PromptContributionV1(StrictFrozenModel):
    contract_version: Literal[CONTRIBUTION_VERSION] = CONTRIBUTION_VERSION
    contribution_id: str
    kind: PromptContributionKind
    authority: PromptContributionAuthority
    source_version: str
    order: int
    content: str = Field(repr=False)
    content_sha256: str
    estimated_tokens: int = Field(ge=1)
    token_estimator_version: Literal[TOKEN_ESTIMATOR_VERSION] = TOKEN_ESTIMATOR_VERSION

    @field_validator("contribution_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not ID_RE.fullmatch(value):
            raise ValueError("contribution_id is invalid")
        return value

    @field_validator("source_version")
    @classmethod
    def _valid_source_version(cls, value: str) -> str:
        if not VERSION_RE.fullmatch(value):
            raise ValueError("source_version is invalid")
        return value

    @field_validator("content")
    @classmethod
    def _valid_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _contract_matches_kind(self) -> "PromptContributionV1":
        expected_order, expected_authority = KIND_CONTRACT[self.kind]
        if self.order != expected_order:
            raise ValueError("order does not match contribution kind")
        if self.authority is not expected_authority:
            raise ValueError("authority does not match contribution kind")
        if self.content_sha256 != text_sha256(self.content):
            raise ValueError("content_sha256 does not match content")
        if self.estimated_tokens != estimate_text_tokens(self.content):
            raise ValueError("estimated_tokens does not match content")
        return self

    @classmethod
    def create(
        cls,
        *,
        contribution_id: str,
        kind: PromptContributionKind,
        source_version: str,
        content: str,
    ) -> "PromptContributionV1":
        order, authority = KIND_CONTRACT[kind]
        return cls(
            contribution_id=contribution_id,
            kind=kind,
            authority=authority,
            source_version=source_version,
            order=order,
            content=content,
            content_sha256=text_sha256(content),
            estimated_tokens=estimate_text_tokens(content),
        )


__all__ = [
    "CONTRIBUTION_VERSION",
    "KIND_CONTRACT",
    "PromptContributionAuthority",
    "PromptContributionError",
    "PromptContributionKind",
    "PromptContributionV1",
    "StrictFrozenModel",
    "canonical_json_bytes",
    "estimate_text_tokens",
    "text_sha256",
]
