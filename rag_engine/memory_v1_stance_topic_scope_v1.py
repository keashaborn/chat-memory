from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .memory_v1_selection_envelope import (
    MemorySelectionRequestBindingV1,
    MemorySelectionRequestV1,
)


CONTRACT_VERSION = "memory_claim_stance_topic_scope_v1"
MATCH_POLICY_VERSION = "stance_topic_token_overlap_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
ABOUT_RE = re.compile(r"\b(?:about|regarding|concerning)\s+(.+)$")
STANCE_ON_RE = re.compile(
    r"\b(?:stance|view|views|opinion|opinions|position)\s+on\s+(.+)$"
)
MAX_TOPIC_TOKENS = 24
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "believe",
        "believes",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "may",
        "me",
        "might",
        "my",
        "of",
        "on",
        "opinion",
        "opinions",
        "or",
        "our",
        "position",
        "said",
        "say",
        "shared",
        "stance",
        "stances",
        "that",
        "the",
        "their",
        "think",
        "thinks",
        "this",
        "to",
        "view",
        "views",
        "was",
        "we",
        "were",
        "what",
        "will",
        "with",
        "would",
        "you",
    }
)


class StanceTopicScopeError(RuntimeError):
    """Fail-closed error at the governed stance-topic boundary."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _token_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stem(token: str) -> str:
    if len(token) > 5 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _normalized_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = {
        _stem(token)
        for token in TOKEN_RE.findall(normalized)
        if token not in STOP_WORDS
    }
    tokens.discard("")
    return tuple(sorted(tokens))


def stance_topic_tokens(query: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(query or "")).casefold().strip()
    match = ABOUT_RE.search(normalized) or STANCE_ON_RE.search(normalized)
    if match is None:
        return ()
    tokens = _normalized_tokens(match.group(1).rstrip("?.! "))
    if len(tokens) > MAX_TOPIC_TOKENS:
        raise StanceTopicScopeError("stance topic exceeds the token limit")
    return tokens


class _StanceTopicScopePayloadV1(StrictFrozenModel):
    contract_version: Literal[CONTRACT_VERSION]
    owner_user_id: UUID
    selection_trace_id: UUID
    request_binding_sha256: str
    predicate: Literal["stance.reported"]
    match_policy_version: Literal[MATCH_POLICY_VERSION]
    broad_recall: bool
    topic_token_sha256s: tuple[str, ...]
    minimum_overlap: int = Field(ge=0, le=2)

    @field_validator("request_binding_sha256")
    @classmethod
    def request_hash(cls, value: str) -> str:
        if SHA256_RE.fullmatch(value) is None:
            raise ValueError("request binding must be a lowercase SHA-256")
        return value

    @field_validator("topic_token_sha256s")
    @classmethod
    def topic_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_TOPIC_TOKENS or value != tuple(sorted(set(value))):
            raise ValueError("topic hashes must be sorted, unique, and bounded")
        if any(SHA256_RE.fullmatch(item) is None for item in value):
            raise ValueError("topic hashes must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def reconcile_mode(self) -> "_StanceTopicScopePayloadV1":
        if self.broad_recall:
            if self.topic_token_sha256s or self.minimum_overlap != 0:
                raise ValueError("broad stance recall cannot carry topic constraints")
        else:
            if not self.topic_token_sha256s:
                raise ValueError("specific stance recall requires topic constraints")
            expected = 1 if len(self.topic_token_sha256s) == 1 else 2
            if self.minimum_overlap != expected:
                raise ValueError("specific stance overlap policy changed")
        return self


class StanceTopicScopeV1(_StanceTopicScopePayloadV1):
    scope_manifest_sha256: str

    @field_validator("scope_manifest_sha256")
    @classmethod
    def manifest_hash(cls, value: str) -> str:
        if SHA256_RE.fullmatch(value) is None:
            raise ValueError("scope manifest must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def verify_manifest(self) -> "StanceTopicScopeV1":
        payload = self.model_dump(mode="json", exclude={"scope_manifest_sha256"})
        if self.scope_manifest_sha256 != _sha256(payload):
            raise ValueError("stance topic scope manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        request: MemorySelectionRequestV1,
        topic_tokens: tuple[str, ...],
    ) -> "StanceTopicScopeV1":
        request = request.strict_revalidated()
        binding = MemorySelectionRequestBindingV1.from_request(request)
        tokens = tuple(sorted(set(topic_tokens)))
        if len(tokens) > MAX_TOPIC_TOKENS:
            raise ValueError("stance topic exceeds the token limit")
        hashes = tuple(sorted(_token_sha256(item) for item in tokens))
        payload = _StanceTopicScopePayloadV1(
            contract_version=CONTRACT_VERSION,
            owner_user_id=request.owner_user_id,
            selection_trace_id=request.selection_trace_id,
            request_binding_sha256=binding.request_binding_sha256,
            predicate="stance.reported",
            match_policy_version=MATCH_POLICY_VERSION,
            broad_recall=not bool(hashes),
            topic_token_sha256s=hashes,
            minimum_overlap=0 if not hashes else (1 if len(hashes) == 1 else 2),
        )
        value = payload.model_dump(mode="json")
        return cls(**payload.model_dump(), scope_manifest_sha256=_sha256(value))

    def strict_revalidated(self) -> "StanceTopicScopeV1":
        return type(self).model_validate_json(self.model_dump_json())


def resolve_stance_topic_scope_v1(
    *,
    request: MemorySelectionRequestV1,
    claim_context: Mapping[str, Any],
) -> StanceTopicScopeV1 | None:
    if str(claim_context.get("domain") or "") != "stance_recall":
        return None
    allowed = tuple(
        sorted(
            {
                str(item).strip().casefold()
                for item in claim_context.get("allowed_predicates", ())
                if str(item).strip()
            }
        )
    )
    if allowed != ("stance.reported",):
        raise StanceTopicScopeError(
            "stance topic scope requires the exact stance.reported permission"
        )
    return StanceTopicScopeV1.create(
        request=request,
        topic_tokens=stance_topic_tokens(request.query_text),
    )


def _row_topic_tokens(row: Mapping[str, object]) -> tuple[str, ...]:
    canonical_text = row.get("canonical_text")
    if not isinstance(canonical_text, str) or not canonical_text.strip():
        raise StanceTopicScopeError(
            "stance row lacks canonical text"
        )
    literal = row.get("object_literal")
    if isinstance(literal, str):
        try:
            literal = json.loads(literal)
        except (TypeError, ValueError):
            literal = None
    value = literal.get("value") if isinstance(literal, Mapping) else {}
    if not isinstance(value, Mapping):
        value = {}
    material = " ".join(
        str(item or "")
        for item in (
            value.get("topic_key"),
            value.get("topic_text"),
            value.get("position"),
            canonical_text,
        )
    )
    return _normalized_tokens(material)


def claim_row_matches_stance_topic_scope_v1(
    row: Mapping[str, object],
    scope: StanceTopicScopeV1,
) -> bool:
    try:
        scope = scope.strict_revalidated()
        owner = row["owner_user_id"]
        predicate = row["predicate"]
    except Exception as exc:
        raise StanceTopicScopeError(
            "stance row is missing typed topic-scope fields"
        ) from exc
    if not isinstance(owner, UUID) or owner != scope.owner_user_id:
        raise StanceTopicScopeError("stance row crossed the owner boundary")
    if predicate != scope.predicate:
        return False
    if scope.broad_recall:
        return True
    row_hashes = {_token_sha256(item) for item in _row_topic_tokens(row)}
    overlap = row_hashes.intersection(scope.topic_token_sha256s)
    return len(overlap) >= scope.minimum_overlap


__all__ = [
    "CONTRACT_VERSION",
    "MATCH_POLICY_VERSION",
    "StanceTopicScopeError",
    "StanceTopicScopeV1",
    "claim_row_matches_stance_topic_scope_v1",
    "resolve_stance_topic_scope_v1",
    "stance_topic_tokens",
]
