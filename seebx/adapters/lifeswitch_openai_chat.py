from __future__ import annotations

"""Versioned OpenAI request projection for LifeSwitch-aware prompt assemblies."""

import asyncio
import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.adapters.openai_chat import (
    OpenAIChatGenerationConfigV1,
    OpenAIChatProviderContractError,
    OpenAIChatProviderError,
    _field,
    _nonempty_optional_text,
    _optional_usage_detail,
    _returned_model_is_compatible,
    _usage_count,
    safety_identifier_v1,
)
from rag_engine.prompt_assembler_v1 import (
    MODEL_CONTEXT_WINDOW_TOKENS,
    PER_MESSAGE_OVERHEAD_TOKENS,
)
from rag_engine.response_lifeswitch_integration_v2 import (
    TrustedLifeSwitchResponsePlanV2,
)


OPENAI_CHAT_REQUEST_V4 = "openai_chat_request_v4"
# Keep the V3 response wire compatible with the existing usage ledger.
OPENAI_CHAT_ADAPTER_V3 = "openai_chat_completions_lifeswitch_v1"
OPENAI_CHAT_RESPONSE_V3 = "openai_chat_response_v3"
REFERENCE_DATA_MESSAGE_V3 = "provider_reference_data_message_v3"

_MESSAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    raise TypeError(type(value).__name__)


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


class OpenAIChatMessageV2(_StrictFrozenModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000, repr=False)
    name: str | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is not None and not _MESSAGE_NAME_RE.fullmatch(value):
            raise ValueError("provider message name is invalid")
        return value

    @model_validator(mode="after")
    def lower_authority_names_only(self) -> "OpenAIChatMessageV2":
        if self.name is not None and (
            self.role != "user"
            or self.name
            not in {
                "chat_attachments_v1",
                "governed_memory_successor_v1",
                "zep_memory_v1",
                "lifeswitch_domain_context_v1",
                "prior_lifeswitch_provenance_v1",
                "relational_monism_v0_4",
                "prior_web_provenance_v1",
            }
        ):
            raise ValueError("named provider messages must be reference data")
        return self


def _messages(
    plan: TrustedLifeSwitchResponsePlanV2,
) -> tuple[OpenAIChatMessageV2, ...]:
    assembly = plan.assembled_prompt
    conversation = assembly.conversation
    if not conversation or conversation[-1].role != "user":
        raise ValueError("assembled conversation must end with a user message")
    result = [OpenAIChatMessageV2(role="system", content=assembly.system_prompt)]
    result.extend(
        OpenAIChatMessageV2(role=item.role, content=item.content)
        for item in conversation[:-1]
    )
    for block in assembly.context_blocks:
        if block.block_id == "governed_memory_successor_v1":
            content = block.content
        else:
            reference = {
                "authority": "reference_data",
                "block_id": block.block_id,
                "block_manifest_sha256": block.block_manifest_sha256,
                "content": block.content,
                "content_sha256": block.content_sha256,
                "contract_version": REFERENCE_DATA_MESSAGE_V3,
                "kind": block.kind.value,
                "query_sha256": block.query_sha256,
                "request_id_sha256": block.request_id_sha256,
                "source_contract_version": block.source_contract_version,
                "source_manifest_sha256": block.source_manifest_sha256,
            }
            content = _canonical_json_bytes(reference).decode("utf-8")
        result.append(
            OpenAIChatMessageV2(
                role="user",
                name=block.block_id,
                content=content,
            )
        )
    result.append(OpenAIChatMessageV2(role="user", content=conversation[-1].content))
    return tuple(result)


class OpenAIChatRequestV4(_StrictFrozenModel):
    contract_version: Literal[OPENAI_CHAT_REQUEST_V4] = OPENAI_CHAT_REQUEST_V4
    adapter_version: Literal[OPENAI_CHAT_ADAPTER_V3] = OPENAI_CHAT_ADAPTER_V3
    source_plan: TrustedLifeSwitchResponsePlanV2 = Field(repr=False)
    generation_config: OpenAIChatGenerationConfigV1
    messages: tuple[OpenAIChatMessageV2, ...] = Field(
        min_length=2,
        max_length=260,
        repr=False,
    )
    safety_identifier: str
    store: Literal[False] = False
    source_plan_sha256: str
    source_assembly_sha256: str
    messages_sha256: str
    provider_input_bytes: int = Field(ge=1)
    conservative_input_token_bound: int = Field(ge=1)
    conservative_context_token_commitment: int = Field(ge=1)
    request_sha256: str

    @field_validator(
        "source_plan_sha256",
        "source_assembly_sha256",
        "messages_sha256",
        "request_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_request(self) -> "OpenAIChatRequestV4":
        plan = TrustedLifeSwitchResponsePlanV2.model_validate_json(
            self.source_plan.model_dump_json()
        )
        expected_messages = _messages(plan)
        if self.messages != expected_messages:
            raise ValueError("provider messages differ from LifeSwitch plan")
        if self.source_plan_sha256 != plan.plan_sha256:
            raise ValueError("provider request differs from LifeSwitch plan")
        if self.source_assembly_sha256 != plan.assembled_prompt.manifest.assembly_sha256:
            raise ValueError("provider request differs from V2 assembly")
        raw_bytes = len(_canonical_json_bytes(expected_messages))
        bound = raw_bytes + len(expected_messages) * PER_MESSAGE_OVERHEAD_TOKENS
        commitment = bound + self.generation_config.max_completion_tokens
        if self.messages_sha256 != _sha256(expected_messages):
            raise ValueError("provider messages hash mismatch")
        if self.provider_input_bytes != raw_bytes:
            raise ValueError("provider byte count mismatch")
        if self.conservative_input_token_bound != bound:
            raise ValueError("provider input bound mismatch")
        if self.conservative_context_token_commitment != commitment:
            raise ValueError("provider context commitment mismatch")
        if commitment > MODEL_CONTEXT_WINDOW_TOKENS:
            raise ValueError("provider request exceeds model context")
        payload = self.model_dump(mode="json", exclude={"request_sha256"})
        if self.request_sha256 != _sha256(payload):
            raise ValueError("provider request hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        source_plan: TrustedLifeSwitchResponsePlanV2,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> "OpenAIChatRequestV4":
        plan = TrustedLifeSwitchResponsePlanV2.model_validate_json(
            source_plan.model_dump_json()
        )
        config = generation_config or OpenAIChatGenerationConfigV1()
        messages = _messages(plan)
        raw_bytes = len(_canonical_json_bytes(messages))
        bound = raw_bytes + len(messages) * PER_MESSAGE_OVERHEAD_TOKENS
        values = {
            "contract_version": OPENAI_CHAT_REQUEST_V4,
            "adapter_version": OPENAI_CHAT_ADAPTER_V3,
            "source_plan": plan,
            "generation_config": config,
            "messages": messages,
            "safety_identifier": safety_identifier_v1(
                plan.base_response_plan.authenticated_actor_user_id
            ),
            "store": False,
            "source_plan_sha256": plan.plan_sha256,
            "source_assembly_sha256": plan.assembled_prompt.manifest.assembly_sha256,
            "messages_sha256": _sha256(messages),
            "provider_input_bytes": raw_bytes,
            "conservative_input_token_bound": bound,
            "conservative_context_token_commitment": (
                bound + config.max_completion_tokens
            ),
        }
        return cls(**values, request_sha256=_sha256(values))

    def provider_kwargs(self) -> dict[str, Any]:
        return {
            "model": self.generation_config.model,
            "messages": [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.messages
            ],
            "reasoning_effort": self.generation_config.reasoning_effort,
            "max_completion_tokens": self.generation_config.max_completion_tokens,
            "safety_identifier": self.safety_identifier,
            "store": False,
            "timeout": self.generation_config.timeout_seconds,
        }

    def provider_kwargs_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.provider_kwargs())


class OpenAIChatResponseV3(_StrictFrozenModel):
    contract_version: Literal[OPENAI_CHAT_RESPONSE_V3] = OPENAI_CHAT_RESPONSE_V3
    adapter_version: Literal[OPENAI_CHAT_ADAPTER_V3] = OPENAI_CHAT_ADAPTER_V3
    provider_request_sha256: str
    response_id: str = Field(min_length=1, max_length=240)
    requested_model: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    finish_reason: Literal["stop"] = "stop"
    content: str | None = Field(default=None, min_length=1, max_length=500_000, repr=False)
    refusal: str | None = Field(default=None, min_length=1, max_length=500_000, repr=False)
    provider_input_tokens: int = Field(ge=0)
    provider_cached_input_tokens: int = Field(ge=0)
    provider_output_tokens: int = Field(ge=0)
    provider_reasoning_output_tokens: int = Field(ge=0)
    provider_total_tokens: int = Field(ge=0)
    request_max_completion_tokens: int = Field(ge=1)
    request_conservative_input_token_bound: int = Field(ge=1)
    content_sha256: str | None = None
    refusal_sha256: str | None = None
    response_sha256: str

    @field_validator(
        "provider_request_sha256",
        "content_sha256",
        "refusal_sha256",
        "response_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_response(self) -> "OpenAIChatResponseV3":
        if (self.content is None) == (self.refusal is None):
            raise ValueError("provider response requires exactly one output form")
        if not _returned_model_is_compatible(self.requested_model, self.model):
            raise ValueError("provider returned a different model family")
        if self.provider_total_tokens != self.provider_input_tokens + self.provider_output_tokens:
            raise ValueError("provider token totals do not reconcile")
        if self.provider_cached_input_tokens > self.provider_input_tokens:
            raise ValueError("cached input exceeds provider input")
        if self.provider_reasoning_output_tokens > self.provider_output_tokens:
            raise ValueError("reasoning output exceeds provider output")
        if self.provider_output_tokens > self.request_max_completion_tokens:
            raise ValueError("provider output exceeds request limit")
        if self.provider_input_tokens > self.request_conservative_input_token_bound:
            raise ValueError("provider input exceeds conservative bound")
        if self.provider_total_tokens > MODEL_CONTEXT_WINDOW_TOKENS:
            raise ValueError("provider usage exceeds model context")
        expected_content = (
            hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            if self.content is not None
            else None
        )
        expected_refusal = (
            hashlib.sha256(self.refusal.encode("utf-8")).hexdigest()
            if self.refusal is not None
            else None
        )
        if self.content_sha256 != expected_content or self.refusal_sha256 != expected_refusal:
            raise ValueError("provider output hash mismatch")
        payload = self.model_dump(mode="json", exclude={"response_sha256"})
        if self.response_sha256 != _sha256(payload):
            raise ValueError("provider response manifest hash mismatch")
        return self


def _response_from_provider_v3(
    request: OpenAIChatRequestV4,
    provider_response: Any,
) -> OpenAIChatResponseV3:
    choices = _field(provider_response, "choices")
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise ValueError("provider response must contain exactly one choice")
    choice = choices[0]
    if _field(choice, "index") != 0 or _field(choice, "finish_reason") != "stop":
        raise ValueError("provider response choice is incomplete")
    message = _field(choice, "message")
    if message is None or _field(message, "role") != "assistant":
        raise ValueError("provider response message is invalid")
    for unexpected in ("tool_calls", "function_call", "audio"):
        if _field(message, unexpected) is not None:
            raise ValueError("provider response contains an unexpected output form")
    content = _nonempty_optional_text(_field(message, "content"), "content")
    refusal = _nonempty_optional_text(_field(message, "refusal"), "refusal")
    if (content is None) == (refusal is None):
        raise ValueError("provider response requires exactly one output form")
    response_id = _field(provider_response, "id")
    model = _field(provider_response, "model")
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("provider response id is invalid")
    if not isinstance(model, str) or not _returned_model_is_compatible(
        request.generation_config.model,
        model,
    ):
        raise ValueError("provider response model is invalid")
    usage = _field(provider_response, "usage")
    if usage is None:
        raise ValueError("provider response usage is missing")
    input_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_count(usage, "total_tokens", "total_tokens")
    cached = _optional_usage_detail(
        usage,
        primary_container="prompt_tokens_details",
        alternate_container="input_tokens_details",
        field_name="cached_tokens",
    )
    reasoning = _optional_usage_detail(
        usage,
        primary_container="completion_tokens_details",
        alternate_container="output_tokens_details",
        field_name="reasoning_tokens",
    )
    values = {
        "contract_version": OPENAI_CHAT_RESPONSE_V3,
        "adapter_version": OPENAI_CHAT_ADAPTER_V3,
        "provider_request_sha256": request.request_sha256,
        "response_id": response_id,
        "requested_model": request.generation_config.model,
        "model": model,
        "finish_reason": "stop",
        "content": content,
        "refusal": refusal,
        "provider_input_tokens": input_tokens,
        "provider_cached_input_tokens": cached,
        "provider_output_tokens": output_tokens,
        "provider_reasoning_output_tokens": reasoning,
        "provider_total_tokens": total_tokens,
        "request_max_completion_tokens": request.generation_config.max_completion_tokens,
        "request_conservative_input_token_bound": request.conservative_input_token_bound,
        "content_sha256": (
            hashlib.sha256(content.encode("utf-8")).hexdigest() if content else None
        ),
        "refusal_sha256": (
            hashlib.sha256(refusal.encode("utf-8")).hexdigest() if refusal else None
        ),
    }
    return OpenAIChatResponseV3(**values, response_sha256=_sha256(values))


class OpenAIChatCompletionsAdapterV3:
    def __init__(self, client: Any) -> None:
        if client is None or not callable(getattr(client, "with_options", None)):
            raise OpenAIChatProviderContractError("a zero-retry OpenAI client is required")
        self._client = client

    def complete(
        self,
        plan: TrustedLifeSwitchResponsePlanV2,
        *,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> OpenAIChatResponseV3:
        request = OpenAIChatRequestV4.create(
            source_plan=plan,
            generation_config=generation_config,
        )
        try:
            client = self._client.with_options(
                max_retries=0,
                timeout=request.generation_config.timeout_seconds,
            )
            response = client.chat.completions.create(**request.provider_kwargs())
            return _response_from_provider_v3(request, response)
        except OpenAIChatProviderError:
            raise
        except Exception:
            raise OpenAIChatProviderError("OpenAI LifeSwitch chat completion failed") from None

    async def complete_async(
        self,
        plan: TrustedLifeSwitchResponsePlanV2,
        *,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> OpenAIChatResponseV3:
        config = generation_config or OpenAIChatGenerationConfigV1()
        return await asyncio.wait_for(
            asyncio.to_thread(self.complete, plan, generation_config=config),
            timeout=config.timeout_seconds + 5.0,
        )


__all__ = [
    "OpenAIChatCompletionsAdapterV3",
    "OpenAIChatMessageV2",
    "OpenAIChatRequestV4",
    "OpenAIChatResponseV3",
]
