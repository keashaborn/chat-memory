from __future__ import annotations

"""Versioned prompt augmentation for independently governed LifeSwitch data.

The existing V1 prompt assembly remains byte-for-byte and hash compatible.
This boundary accepts a fully validated V1 assembly plus an independently
selected LifeSwitch envelope and produces a new V3 assembly.  It performs no
database access and does not place LifeSwitch data inside Memory V1.
"""

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchDomainContextEnvelopeV1,
    LifeSwitchRenderedContextV1,
    render_lifeswitch_context_v1,
)
from rag_engine.prior_lifeswitch_provenance_v1 import (
    MAX_PROVENANCE_CONTENT_BYTES,
    MAX_PROVENANCE_CONTENT_TOKENS,
    PriorLifeSwitchProvenanceEnvelopeV1,
)
from rag_engine.prompt_assembler_v1 import (
    HARD_MAX_MESSAGE_BYTES,
    HARD_MAX_MESSAGE_TOKENS,
    HARD_MAX_TOTAL_INPUT_BYTES,
    HARD_MAX_TOTAL_INPUT_TOKENS,
    MODEL_CONTEXT_WINDOW_TOKENS,
    PER_MESSAGE_OVERHEAD_TOKENS,
    RESERVED_OUTPUT_TOKENS,
    AssembledPromptV1,
    ConversationMessageV1,
)


LIFESWITCH_PROMPT_REQUEST_V2_VERSION = "lifeswitch_prompt_augmentation_request_v2"
LIFESWITCH_PROMPT_BLOCK_V3_VERSION = "prompt_reference_context_block_v3"
LIFESWITCH_PROMPT_MANIFEST_V3_VERSION = "prompt_assembly_manifest_v7"
LIFESWITCH_ASSEMBLED_PROMPT_V3_VERSION = "assembled_prompt_v4"
LIFESWITCH_PROMPT_ASSEMBLER_V2_VERSION = "typed_prompt_assembler_v6"

LIFESWITCH_MAX_TOKENS = 1_000
WEB_PROVENANCE_MAX_BYTES = 16_384
WEB_PROVENANCE_MAX_TOKENS = 4_096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LifeSwitchPromptIntegrationError(RuntimeError):
    """Fail-closed error at the LifeSwitch prompt integration boundary."""


class ContextKindV3(str, Enum):
    MEMORY = "memory"
    ATTACHMENT = "attachment"
    LIFESWITCH = "lifeswitch"
    PRIOR_LIFESWITCH_PROVENANCE = "prior_lifeswitch_provenance"
    FRACTAL_MONISM = "fractal_monism"
    WEB_PROVENANCE = "web_provenance"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: str | UUID) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)


class PromptReferenceContextBlockV3(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PROMPT_BLOCK_V3_VERSION] = (
        LIFESWITCH_PROMPT_BLOCK_V3_VERSION
    )
    block_id: Literal[
        "governed_memory_successor_v1",
        "chat_attachments_v1",
        "lifeswitch_domain_context_v1",
        "prior_lifeswitch_provenance_v1",
        "fractal_monism_v0_2",
        "prior_web_provenance_v1",
    ]
    kind: ContextKindV3
    source_contract_version: str = Field(min_length=1, max_length=160)
    source_manifest_sha256: str
    request_id_sha256: str
    query_sha256: str
    content: str = Field(min_length=1, max_length=96_000, repr=False)
    content_sha256: str
    content_bytes: int = Field(ge=1, le=96_000)
    estimated_tokens: int = Field(ge=1, le=32_000)
    block_manifest_sha256: str

    @field_validator(
        "source_manifest_sha256",
        "request_id_sha256",
        "query_sha256",
        "content_sha256",
        "block_manifest_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_block(self) -> "PromptReferenceContextBlockV3":
        expected_id = {
            ContextKindV3.MEMORY: "governed_memory_successor_v1",
            ContextKindV3.ATTACHMENT: "chat_attachments_v1",
            ContextKindV3.LIFESWITCH: "lifeswitch_domain_context_v1",
            ContextKindV3.PRIOR_LIFESWITCH_PROVENANCE: "prior_lifeswitch_provenance_v1",
            ContextKindV3.FRACTAL_MONISM: "fractal_monism_v0_2",
            ContextKindV3.WEB_PROVENANCE: "prior_web_provenance_v1",
        }[self.kind]
        if self.block_id != expected_id:
            raise ValueError("context block id differs from kind")
        raw = self.content.encode("utf-8")
        if self.content_sha256 != _text_sha256(self.content):
            raise ValueError("context content hash mismatch")
        if self.content_bytes != len(raw):
            raise ValueError("context byte count mismatch")
        if self.estimated_tokens != _tokens(self.content):
            raise ValueError("context token estimate mismatch")
        payload = self.model_dump(mode="json", exclude={"block_manifest_sha256"})
        if self.block_manifest_sha256 != _sha256(payload):
            raise ValueError("context block manifest hash mismatch")
        return self


class LifeSwitchPromptAugmentationRequestV2(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PROMPT_REQUEST_V2_VERSION] = (
        LIFESWITCH_PROMPT_REQUEST_V2_VERSION
    )
    trusted_thread_id: UUID = Field(repr=False)
    conversation_snapshot_sha256: str
    base_assembly: AssembledPromptV1 = Field(repr=False)
    lifeswitch_envelope: LifeSwitchDomainContextEnvelopeV1 | None = Field(
        default=None,
        repr=False,
    )
    lifeswitch_rendered: LifeSwitchRenderedContextV1 | None = Field(
        default=None,
        repr=False,
    )
    prior_lifeswitch_provenance: PriorLifeSwitchProvenanceEnvelopeV1 | None = Field(
        default=None,
        repr=False,
    )
    request_manifest_sha256: str

    @field_validator("conversation_snapshot_sha256", "request_manifest_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_request(self) -> "LifeSwitchPromptAugmentationRequestV2":
        try:
            base = AssembledPromptV1.from_wire_json(
                self.base_assembly.canonical_json_bytes()
            )
        except Exception as error:
            raise ValueError("base prompt assembly is invalid") from error
        envelope = self.lifeswitch_envelope
        rendered = self.lifeswitch_rendered
        if (envelope is None) != (rendered is None):
            if envelope is None or envelope.status in {"SELECTED", "PARTIAL"}:
                raise ValueError("LifeSwitch envelope and render must be paired")
        if envelope is not None:
            policy = base.source_request.policy_input
            if envelope.request_id != policy.request_id:
                raise ValueError("LifeSwitch request id differs from prompt request")
            if envelope.query_sha256 != policy.current_message_sha256:
                raise ValueError("LifeSwitch query differs from current message")
            if envelope.thread_id != self.trusted_thread_id:
                raise ValueError("LifeSwitch thread differs from trusted thread")
            if (
                envelope.conversation_snapshot_sha256
                != self.conversation_snapshot_sha256
            ):
                raise ValueError("LifeSwitch snapshot differs from trusted snapshot")
            expected_render = render_lifeswitch_context_v1(envelope)
            if expected_render != rendered:
                raise ValueError("LifeSwitch render is not canonical")
        prior = self.prior_lifeswitch_provenance
        if prior is not None:
            if prior.thread_id_sha256 != _text_sha256(self.trusted_thread_id):
                raise ValueError("prior LifeSwitch thread differs from trusted thread")
            if prior.conversation_snapshot_sha256 != self.conversation_snapshot_sha256:
                raise ValueError("prior LifeSwitch snapshot differs from trusted snapshot")
            policy = base.source_request.policy_input
            if prior.current_request_id_sha256 != _text_sha256(policy.request_id):
                raise ValueError("prior LifeSwitch request differs from prompt request")
            if prior.current_query_sha256 != policy.current_message_sha256:
                raise ValueError("prior LifeSwitch query differs from current message")
        payload = self.model_dump(mode="json", exclude={"request_manifest_sha256"})
        if self.request_manifest_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch augmentation request hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        trusted_thread_id: UUID,
        conversation_snapshot_sha256: str,
        base_assembly: AssembledPromptV1,
        lifeswitch_envelope: LifeSwitchDomainContextEnvelopeV1 | None,
        lifeswitch_rendered: LifeSwitchRenderedContextV1 | None,
        prior_lifeswitch_provenance: PriorLifeSwitchProvenanceEnvelopeV1 | None,
    ) -> "LifeSwitchPromptAugmentationRequestV2":
        payload = {
            "contract_version": LIFESWITCH_PROMPT_REQUEST_V2_VERSION,
            "trusted_thread_id": trusted_thread_id,
            "conversation_snapshot_sha256": conversation_snapshot_sha256,
            "base_assembly": base_assembly,
            "lifeswitch_envelope": lifeswitch_envelope,
            "lifeswitch_rendered": lifeswitch_rendered,
            "prior_lifeswitch_provenance": prior_lifeswitch_provenance,
        }
        return cls(**payload, request_manifest_sha256=_sha256(payload))


class PromptAssemblyManifestV3(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PROMPT_MANIFEST_V3_VERSION] = (
        LIFESWITCH_PROMPT_MANIFEST_V3_VERSION
    )
    assembler_version: Literal[LIFESWITCH_PROMPT_ASSEMBLER_V2_VERSION] = (
        LIFESWITCH_PROMPT_ASSEMBLER_V2_VERSION
    )
    source_v1_assembly_sha256: str
    augmentation_request_sha256: str
    context_order: tuple[str, ...]
    context_block_count: int = Field(ge=0, le=6)
    lifeswitch_envelope_sha256: str | None = None
    lifeswitch_render_sha256: str | None = None
    lifeswitch_record_count: int = Field(ge=0, le=500)
    lifeswitch_estimated_tokens: int = Field(ge=0, le=LIFESWITCH_MAX_TOKENS)
    prior_lifeswitch_provenance_manifest_sha256: str | None = None
    prior_lifeswitch_response_count: int = Field(ge=0, le=3)
    prior_lifeswitch_estimated_tokens: int = Field(
        ge=0,
        le=MAX_PROVENANCE_CONTENT_TOKENS,
    )
    total_input_bytes: int = Field(ge=1, le=HARD_MAX_TOTAL_INPUT_BYTES)
    total_input_tokens: int = Field(ge=1, le=HARD_MAX_TOTAL_INPUT_TOKENS)
    total_message_count: int = Field(ge=2, le=263)
    context_window_committed_tokens: int = Field(
        ge=1,
        le=MODEL_CONTEXT_WINDOW_TOKENS,
    )
    assembly_sha256: str

    @field_validator(
        "source_v1_assembly_sha256",
        "augmentation_request_sha256",
        "lifeswitch_envelope_sha256",
        "lifeswitch_render_sha256",
        "prior_lifeswitch_provenance_manifest_sha256",
        "assembly_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value


class AssembledPromptV3(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_ASSEMBLED_PROMPT_V3_VERSION] = (
        LIFESWITCH_ASSEMBLED_PROMPT_V3_VERSION
    )
    source_request: LifeSwitchPromptAugmentationRequestV2 = Field(repr=False)
    system_prompt: str = Field(min_length=1, max_length=96_000, repr=False)
    context_blocks: tuple[PromptReferenceContextBlockV3, ...] = Field(
        max_length=5,
        repr=False,
    )
    conversation: tuple[ConversationMessageV1, ...] = Field(
        min_length=1,
        max_length=256,
        repr=False,
    )
    manifest: PromptAssemblyManifestV3

    @model_validator(mode="after")
    def exact_assembly(self) -> "AssembledPromptV3":
        request = LifeSwitchPromptAugmentationRequestV2.model_validate_json(
            _canonical_json_bytes(self.source_request)
        )
        base = request.base_assembly
        if self.system_prompt != base.system_prompt or self.conversation != base.conversation:
            raise ValueError("V3 changed the V1 system prompt or conversation")
        order = tuple(block.block_id for block in self.context_blocks)
        if order != self.manifest.context_order:
            raise ValueError("V3 context order differs from manifest")
        if len(order) != len(set(order)):
            raise ValueError("V3 context blocks are duplicated")
        payload = self.manifest.model_dump(mode="json", exclude={"assembly_sha256"})
        if self.manifest.assembly_sha256 != _sha256(payload):
            raise ValueError("V3 assembly manifest hash mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "AssembledPromptV3":
        try:
            return cls.model_validate_json(value)
        except Exception:
            raise LifeSwitchPromptIntegrationError(
                "invalid assembled prompt V3 wire"
            ) from None


def _copy_base_block(block: Any) -> PromptReferenceContextBlockV3:
    kind = ContextKindV3(block.kind.value)
    payload = {
        "contract_version": LIFESWITCH_PROMPT_BLOCK_V3_VERSION,
        "block_id": block.block_id,
        "kind": kind,
        "source_contract_version": block.source_contract_version,
        "source_manifest_sha256": block.source_manifest_sha256,
        "request_id_sha256": block.request_id_sha256,
        "query_sha256": block.query_sha256,
        "content": block.content,
        "content_sha256": block.content_sha256,
        "content_bytes": block.content_bytes,
        "estimated_tokens": _tokens(block.content),
    }
    return PromptReferenceContextBlockV3(
        **payload,
        block_manifest_sha256=_sha256(payload),
    )


def _lifeswitch_block(
    envelope: LifeSwitchDomainContextEnvelopeV1,
    rendered: LifeSwitchRenderedContextV1,
) -> PromptReferenceContextBlockV3:
    payload = {
        "contract_version": LIFESWITCH_PROMPT_BLOCK_V3_VERSION,
        "block_id": "lifeswitch_domain_context_v1",
        "kind": ContextKindV3.LIFESWITCH,
        "source_contract_version": envelope.contract_version,
        "source_manifest_sha256": envelope.envelope_sha256,
        "request_id_sha256": _text_sha256(envelope.request_id),
        "query_sha256": envelope.query_sha256,
        "content": rendered.content,
        "content_sha256": rendered.content_sha256,
        "content_bytes": rendered.content_bytes,
        "estimated_tokens": rendered.estimated_tokens,
    }
    return PromptReferenceContextBlockV3(
        **payload,
        block_manifest_sha256=_sha256(payload),
    )


def _prior_lifeswitch_block(
    envelope: PriorLifeSwitchProvenanceEnvelopeV1,
) -> PromptReferenceContextBlockV3:
    payload = {
        "contract_version": LIFESWITCH_PROMPT_BLOCK_V3_VERSION,
        "block_id": "prior_lifeswitch_provenance_v1",
        "kind": ContextKindV3.PRIOR_LIFESWITCH_PROVENANCE,
        "source_contract_version": envelope.contract_version,
        "source_manifest_sha256": envelope.manifest_sha256,
        "request_id_sha256": envelope.current_request_id_sha256,
        "query_sha256": envelope.current_query_sha256,
        "content": envelope.content,
        "content_sha256": envelope.content_sha256,
        "content_bytes": envelope.content_bytes,
        "estimated_tokens": envelope.estimated_tokens,
    }
    return PromptReferenceContextBlockV3(
        **payload,
        block_manifest_sha256=_sha256(payload),
    )


def assemble_prompt_with_lifeswitch_v2(
    request: LifeSwitchPromptAugmentationRequestV2,
) -> AssembledPromptV3:
    """Insert LifeSwitch after Memory and before FM without changing V1."""

    try:
        source = LifeSwitchPromptAugmentationRequestV2.model_validate_json(
            _canonical_json_bytes(request)
        )
        base = AssembledPromptV1.from_wire_json(
            source.base_assembly.canonical_json_bytes()
        )
    except Exception:
        raise LifeSwitchPromptIntegrationError(
            "invalid LifeSwitch prompt augmentation input"
        ) from None

    by_id = {block.block_id: _copy_base_block(block) for block in base.context_blocks}
    envelope = source.lifeswitch_envelope
    rendered = source.lifeswitch_rendered
    if envelope is not None and rendered is not None:
        if rendered.estimated_tokens > LIFESWITCH_MAX_TOKENS:
            raise LifeSwitchPromptIntegrationError("LifeSwitch token budget exceeded")
        by_id["lifeswitch_domain_context_v1"] = _lifeswitch_block(
            envelope,
            rendered,
        )
    prior = source.prior_lifeswitch_provenance
    if prior is not None:
        if prior.content_bytes > MAX_PROVENANCE_CONTENT_BYTES:
            raise LifeSwitchPromptIntegrationError(
                "prior LifeSwitch provenance byte budget exceeded"
            )
        if prior.estimated_tokens > MAX_PROVENANCE_CONTENT_TOKENS:
            raise LifeSwitchPromptIntegrationError(
                "prior LifeSwitch provenance token budget exceeded"
            )
        by_id["prior_lifeswitch_provenance_v1"] = _prior_lifeswitch_block(prior)

    ordered_ids = (
        "governed_memory_successor_v1",
        "chat_attachments_v1",
        "lifeswitch_domain_context_v1",
        "prior_lifeswitch_provenance_v1",
        "fractal_monism_v0_2",
        "prior_web_provenance_v1",
    )
    blocks = tuple(by_id[item] for item in ordered_ids if item in by_id)
    web = by_id.get("prior_web_provenance_v1")
    if web is not None and (
        web.content_bytes > WEB_PROVENANCE_MAX_BYTES
        or web.estimated_tokens > WEB_PROVENANCE_MAX_TOKENS
    ):
        raise LifeSwitchPromptIntegrationError("web provenance budget exceeded")

    contents = (
        base.system_prompt,
        *(item.content for item in blocks),
        *(item.content for item in base.conversation),
    )
    if any(
        len(content.encode("utf-8")) > HARD_MAX_MESSAGE_BYTES
        or _tokens(content) > HARD_MAX_MESSAGE_TOKENS
        for content in contents
    ):
        raise LifeSwitchPromptIntegrationError("one V3 prompt message is oversized")
    total_bytes = sum(len(content.encode("utf-8")) for content in contents)
    total_tokens = sum(_tokens(content) for content in contents)
    if (
        total_bytes > HARD_MAX_TOTAL_INPUT_BYTES
        or total_tokens > HARD_MAX_TOTAL_INPUT_TOKENS
    ):
        raise LifeSwitchPromptIntegrationError("one V3 prompt input budget exceeded")
    message_count = len(contents)
    committed = (
        total_bytes
        + message_count * PER_MESSAGE_OVERHEAD_TOKENS
        + RESERVED_OUTPUT_TOKENS
    )
    if committed > MODEL_CONTEXT_WINDOW_TOKENS:
        raise LifeSwitchPromptIntegrationError("one V3 prompt leaves no output budget")

    record_count = sum(section.record_count for section in envelope.sections) if envelope else 0
    values = {
        "contract_version": LIFESWITCH_PROMPT_MANIFEST_V3_VERSION,
        "assembler_version": LIFESWITCH_PROMPT_ASSEMBLER_V2_VERSION,
        "source_v1_assembly_sha256": base.manifest.assembly_sha256,
        "augmentation_request_sha256": source.request_manifest_sha256,
        "context_order": tuple(item.block_id for item in blocks),
        "context_block_count": len(blocks),
        "lifeswitch_envelope_sha256": envelope.envelope_sha256 if envelope else None,
        "lifeswitch_render_sha256": rendered.content_sha256 if rendered else None,
        "lifeswitch_record_count": record_count,
        "lifeswitch_estimated_tokens": rendered.estimated_tokens if rendered else 0,
        "prior_lifeswitch_provenance_manifest_sha256": (
            prior.manifest_sha256 if prior is not None else None
        ),
        "prior_lifeswitch_response_count": (
            len(prior.responses) if prior is not None else 0
        ),
        "prior_lifeswitch_estimated_tokens": (
            prior.estimated_tokens if prior is not None else 0
        ),
        "total_input_bytes": total_bytes,
        "total_input_tokens": total_tokens,
        "total_message_count": message_count,
        "context_window_committed_tokens": committed,
    }
    manifest = PromptAssemblyManifestV3(
        **values,
        assembly_sha256=_sha256(values),
    )
    return AssembledPromptV3(
        source_request=source,
        system_prompt=base.system_prompt,
        context_blocks=blocks,
        conversation=base.conversation,
        manifest=manifest,
    )


__all__ = [
    "AssembledPromptV3",
    "ContextKindV3",
    "LifeSwitchPromptAugmentationRequestV2",
    "LifeSwitchPromptIntegrationError",
    "PromptAssemblyManifestV3",
    "PromptReferenceContextBlockV3",
    "assemble_prompt_with_lifeswitch_v2",
]
