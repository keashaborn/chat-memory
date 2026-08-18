from __future__ import annotations

"""Typed OpenAI Chat Completions boundary for SeeBx conversation assemblies.

This module does not discover credentials, read environment variables, select
memory, select Fractal Monism content, or mutate live routing.  The live server
must inject its existing authenticated OpenAI client and an authenticated owner
UUID.  Reference context is serialized as deterministic user-role data below
the system-policy authority level.
"""

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.capabilities.conversation.prompt import (
    MODEL_CONTEXT_WINDOW_TOKENS,
    PER_MESSAGE_OVERHEAD_TOKENS,
    RESERVED_OUTPUT_TOKENS,
    AssembledPromptV1,
    PromptReferenceContextBlockV1,
)
from seebx.capabilities.conversation.orchestration import TrustedResponsePlanV0_2


OPENAI_CHAT_REQUEST_VERSION = "openai_chat_request_v2"
OPENAI_CHAT_RESPONSE_VERSION = "openai_chat_response_v2"
OPENAI_CHAT_ADAPTER_VERSION = "openai_chat_completions_gpt_5_6_sol_high_v1"
REFERENCE_DATA_MESSAGE_VERSION = "provider_reference_data_message_v1"
SAFETY_IDENTIFIER_VERSION = "vs1"

DEFAULT_CHAT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_MAX_COMPLETION_TOKENS = RESERVED_OUTPUT_TOKENS
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 85.0
MAX_PROVIDER_TIMEOUT_SECONDS = 120.0

_MESSAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SAFETY_IDENTIFIER_RE = re.compile(r"^vs1_[0-9a-f]{60}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SUPPORTED_CHAT_MODELS = frozenset(
    {
        DEFAULT_CHAT_MODEL,
    }
)


class OpenAIChatProviderContractError(RuntimeError):
    """A local typed request violates the provider boundary contract."""


class OpenAIChatProviderError(RuntimeError):
    """The provider call or provider response failed safely."""


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


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_hash(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _canonicalize_wire_json(value: str | bytes) -> bytes:
    parsed = json.loads(value, object_pairs_hook=_reject_duplicate_json_keys)
    return _canonical_json_bytes(parsed)


def _strict_assembly(value: AssembledPromptV1) -> AssembledPromptV1:
    if not isinstance(value, AssembledPromptV1):
        raise OpenAIChatProviderContractError(
            "provider builder requires AssembledPromptV1"
        )
    try:
        return AssembledPromptV1.from_wire_json(value.canonical_json_bytes())
    except Exception:
        raise OpenAIChatProviderContractError(
            "invalid assembled prompt at provider boundary"
        ) from None


def _strict_plan(value: TrustedResponsePlanV0_2) -> TrustedResponsePlanV0_2:
    if not isinstance(value, TrustedResponsePlanV0_2):
        raise OpenAIChatProviderContractError(
            "provider builder requires TrustedResponsePlanV0_2"
        )
    try:
        return TrustedResponsePlanV0_2.model_validate_json(value.model_dump_json())
    except Exception:
        raise OpenAIChatProviderContractError(
            "invalid trusted response plan at provider boundary"
        ) from None


def _canonical_actor_uuid(
    authenticated_actor_user_id: str | uuid.UUID,
) -> str:
    if isinstance(authenticated_actor_user_id, uuid.UUID):
        return str(authenticated_actor_user_id)
    if not isinstance(authenticated_actor_user_id, str):
        raise OpenAIChatProviderContractError(
            "authenticated actor user id must be a canonical UUID"
        )
    try:
        canonical = str(uuid.UUID(authenticated_actor_user_id))
    except (ValueError, AttributeError, TypeError):
        raise OpenAIChatProviderContractError(
            "authenticated actor user id must be a canonical UUID"
        ) from None
    if canonical != authenticated_actor_user_id:
        raise OpenAIChatProviderContractError(
            "authenticated actor user id must be a canonical UUID"
        )
    return canonical


def safety_identifier_v1(
    authenticated_actor_user_id: str | uuid.UUID,
) -> str:
    """Return a stable 64-character pseudonym without retaining the UUID."""

    canonical = _canonical_actor_uuid(authenticated_actor_user_id)
    digest = hashlib.sha256(
        f"verbalsage:openai-safety:v1:{canonical}".encode("utf-8")
    ).hexdigest()
    return f"{SAFETY_IDENTIFIER_VERSION}_{digest[:60]}"


class OpenAIChatGenerationConfigV1(_StrictFrozenModel):
    model: str = DEFAULT_CHAT_MODEL
    reasoning_effort: Literal["high"] = DEFAULT_REASONING_EFFORT
    max_completion_tokens: int = Field(
        default=DEFAULT_MAX_COMPLETION_TOKENS,
        ge=1,
        le=RESERVED_OUTPUT_TOKENS,
    )
    timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        ge=1.0,
        le=MAX_PROVIDER_TIMEOUT_SECONDS,
    )

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        if value not in SUPPORTED_CHAT_MODELS:
            raise ValueError("OpenAI model is outside the live allowlist")
        return value


class OpenAIChatMessageV1(_StrictFrozenModel):
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
    def reference_names_are_lower_authority(self) -> "OpenAIChatMessageV1":
        if self.name is not None:
            if self.role != "user" or self.name not in {
                "governed_memory_successor_v1",
                "relational_monism_v0_4",
                "chat_attachments_v1",
                "prior_web_provenance_v1",
            }:
                raise ValueError("named provider messages must be reference data")
        return self


def _reference_message_content(block: PromptReferenceContextBlockV1) -> str:
    if block.block_id == "governed_memory_successor_v1":
        # The successor renderer already emits a typed, data-only JSON block.
        # Keeping it as the exact message content lets the answer-binding
        # contract prove that its canonical escaped bytes occur once in the
        # exact provider kwargs payload.
        return block.content
    payload = {
        "authority": "reference_data",
        "block_id": block.block_id,
        "block_manifest_sha256": _sha256(block),
        "content": block.content,
        "content_sha256": block.content_sha256,
        "contract_version": REFERENCE_DATA_MESSAGE_VERSION,
        "kind": block.kind.value,
        "query_sha256": block.query_sha256,
        "request_id_sha256": block.request_id_sha256,
        "source_contract_version": block.source_contract_version,
        "source_manifest_sha256": block.source_manifest_sha256,
    }
    return _canonical_json_bytes(payload).decode("utf-8")


def _messages_from_assembly(
    assembly: AssembledPromptV1,
) -> tuple[OpenAIChatMessageV1, ...]:
    conversation = assembly.conversation
    if not conversation or conversation[-1].role != "user":
        raise OpenAIChatProviderContractError(
            "assembled conversation must end with a user message"
        )
    messages: list[OpenAIChatMessageV1] = [
        OpenAIChatMessageV1(role="system", content=assembly.system_prompt)
    ]
    messages.extend(
        OpenAIChatMessageV1(role=item.role, content=item.content)
        for item in conversation[:-1]
    )
    messages.extend(
        OpenAIChatMessageV1(
            role="user",
            name=block.block_id,
            content=_reference_message_content(block),
        )
        for block in assembly.context_blocks
    )
    messages.append(
        OpenAIChatMessageV1(
            role="user",
            content=conversation[-1].content,
        )
    )
    return tuple(messages)


class OpenAIChatRequestV1(_StrictFrozenModel):
    contract_version: Literal[OPENAI_CHAT_REQUEST_VERSION] = OPENAI_CHAT_REQUEST_VERSION
    adapter_version: Literal[OPENAI_CHAT_ADAPTER_VERSION] = OPENAI_CHAT_ADAPTER_VERSION
    api_surface: Literal["chat.completions"] = "chat.completions"
    source_assembly: AssembledPromptV1 = Field(repr=False)
    generation_config: OpenAIChatGenerationConfigV1
    messages: tuple[OpenAIChatMessageV1, ...] = Field(
        min_length=2,
        max_length=260,
        repr=False,
    )
    safety_identifier: str
    store: Literal[False] = False
    source_assembly_sha256: str
    messages_sha256: str
    provider_input_bytes: int = Field(ge=1)
    conservative_input_token_bound: int = Field(ge=1)
    conservative_context_token_commitment: int = Field(ge=1)
    request_sha256: str

    @field_validator("safety_identifier")
    @classmethod
    def valid_safety_identifier(cls, value: str) -> str:
        if not _SAFETY_IDENTIFIER_RE.fullmatch(value):
            raise ValueError("safety_identifier is invalid")
        return value

    @field_validator(
        "source_assembly_sha256",
        "messages_sha256",
        "request_sha256",
    )
    @classmethod
    def valid_hashes(cls, value: str, info: Any) -> str:
        return _validate_hash(value, info.field_name)

    @model_validator(mode="after")
    def exact_provider_request(self) -> "OpenAIChatRequestV1":
        try:
            assembly = AssembledPromptV1.from_wire_json(
                self.source_assembly.canonical_json_bytes()
            )
            expected_messages = _messages_from_assembly(assembly)
        except Exception:
            raise ValueError("provider source assembly is invalid") from None
        if self.messages != expected_messages:
            raise ValueError("provider messages differ from typed assembly")
        if len(self.messages) != assembly.manifest.total_message_count:
            raise ValueError("provider message count differs from assembly")
        expected_assembly_hash = assembly.manifest.assembly_sha256
        expected_messages_hash = _sha256(self.messages)
        provider_input_bytes = len(_canonical_json_bytes(self.messages))
        # A UTF-8 byte count is intentionally used as an upper bound, not as
        # exact tokenizer accounting.  Provider-reported usage is authoritative
        # after execution and is bound into OpenAIChatResponseV1.
        conservative_input_bound = (
            provider_input_bytes + len(self.messages) * PER_MESSAGE_OVERHEAD_TOKENS
        )
        conservative_commitment = (
            conservative_input_bound + self.generation_config.max_completion_tokens
        )
        copied = (
            (self.source_assembly_sha256, expected_assembly_hash),
            (self.messages_sha256, expected_messages_hash),
            (self.provider_input_bytes, provider_input_bytes),
            (self.conservative_input_token_bound, conservative_input_bound),
            (
                self.conservative_context_token_commitment,
                conservative_commitment,
            ),
        )
        if any(actual != expected for actual, expected in copied):
            raise ValueError("provider request manifest does not reconcile")
        if conservative_commitment > MODEL_CONTEXT_WINDOW_TOKENS:
            raise ValueError("provider request exceeds the context window")
        payload = self.model_dump(mode="json", exclude={"request_sha256"})
        if self.request_sha256 != _sha256(payload):
            raise ValueError("provider request hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        trusted_plan: TrustedResponsePlanV0_2,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> "OpenAIChatRequestV1":
        plan = _strict_plan(trusted_plan)
        assembly = _strict_assembly(plan.assembled_prompt)
        try:
            config = OpenAIChatGenerationConfigV1.model_validate_json(
                (generation_config or OpenAIChatGenerationConfigV1()).model_dump_json()
            )
        except Exception:
            raise OpenAIChatProviderContractError(
                "invalid OpenAI generation configuration"
            ) from None
        messages = _messages_from_assembly(assembly)
        provider_input_bytes = len(_canonical_json_bytes(messages))
        conservative_input_bound = (
            provider_input_bytes + len(messages) * PER_MESSAGE_OVERHEAD_TOKENS
        )
        conservative_commitment = (
            conservative_input_bound + config.max_completion_tokens
        )
        payload: dict[str, Any] = {
            "contract_version": OPENAI_CHAT_REQUEST_VERSION,
            "adapter_version": OPENAI_CHAT_ADAPTER_VERSION,
            "api_surface": "chat.completions",
            "source_assembly": assembly.model_dump(mode="json"),
            "generation_config": config.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in messages],
            "safety_identifier": safety_identifier_v1(
                plan.authenticated_actor_user_id
            ),
            "store": False,
            "source_assembly_sha256": assembly.manifest.assembly_sha256,
            "messages_sha256": _sha256(messages),
            "provider_input_bytes": provider_input_bytes,
            "conservative_input_token_bound": conservative_input_bound,
            "conservative_context_token_commitment": conservative_commitment,
        }
        payload["request_sha256"] = _sha256(payload)
        try:
            return cls.model_validate_json(_canonical_json_bytes(payload))
        except Exception:
            raise OpenAIChatProviderContractError(
                "assembled prompt cannot be represented as a provider request"
            ) from None

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "OpenAIChatRequestV1":
        try:
            return cls.model_validate_json(_canonicalize_wire_json(value))
        except Exception:
            raise OpenAIChatProviderContractError(
                "invalid OpenAI chat request wire"
            ) from None

    def provider_kwargs(self) -> dict[str, Any]:
        verified = self.from_wire_json(self.canonical_json_bytes())
        config = verified.generation_config
        return {
            "model": config.model,
            "messages": [
                message.model_dump(mode="json", exclude_none=True)
                for message in verified.messages
            ],
            "reasoning_effort": config.reasoning_effort,
            "max_completion_tokens": config.max_completion_tokens,
            "safety_identifier": verified.safety_identifier,
            "store": False,
            "timeout": config.timeout_seconds,
        }

    def provider_kwargs_json_bytes(self) -> bytes:
        """Canonical bytes of the exact kwargs passed to the SDK boundary."""

        return _canonical_json_bytes(self.provider_kwargs())


class OpenAIChatResponseV1(_StrictFrozenModel):
    contract_version: Literal[OPENAI_CHAT_RESPONSE_VERSION] = (
        OPENAI_CHAT_RESPONSE_VERSION
    )
    adapter_version: Literal[OPENAI_CHAT_ADAPTER_VERSION] = OPENAI_CHAT_ADAPTER_VERSION
    provider_request_sha256: str
    response_id: str = Field(min_length=1, max_length=240)
    requested_model: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    finish_reason: Literal["stop"] = "stop"
    content: str | None = Field(
        default=None,
        min_length=1,
        max_length=500_000,
        repr=False,
    )
    refusal: str | None = Field(
        default=None,
        min_length=1,
        max_length=500_000,
        repr=False,
    )
    provider_input_tokens: int = Field(ge=0)
    provider_cached_input_tokens: int = Field(ge=0)
    provider_output_tokens: int = Field(ge=0)
    provider_reasoning_output_tokens: int = Field(ge=0)
    provider_total_tokens: int = Field(ge=0)
    request_max_completion_tokens: int = Field(ge=1, le=RESERVED_OUTPUT_TOKENS)
    request_conservative_input_token_bound: int = Field(ge=1)
    model_context_window_tokens: Literal[MODEL_CONTEXT_WINDOW_TOKENS] = (
        MODEL_CONTEXT_WINDOW_TOKENS
    )
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
    def valid_hashes(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_hash(value, info.field_name)

    @model_validator(mode="after")
    def exact_response_manifest(self) -> "OpenAIChatResponseV1":
        if not self.content and not self.refusal:
            raise ValueError("provider response has no content or refusal")
        if self.content is not None and self.refusal is not None:
            raise ValueError("provider response cannot contain content and refusal")
        if not _returned_model_is_compatible(self.requested_model, self.model):
            raise ValueError("provider returned a different model family")
        if self.provider_total_tokens != (
            self.provider_input_tokens + self.provider_output_tokens
        ):
            raise ValueError("provider usage totals do not reconcile")
        if self.provider_cached_input_tokens > self.provider_input_tokens:
            raise ValueError("provider cached input usage exceeds input usage")
        if self.provider_reasoning_output_tokens > self.provider_output_tokens:
            raise ValueError("provider reasoning usage exceeds output usage")
        if self.provider_output_tokens > self.request_max_completion_tokens:
            raise ValueError("provider output usage exceeds the request limit")
        if self.provider_input_tokens > self.request_conservative_input_token_bound:
            raise ValueError("provider input usage exceeds the conservative bound")
        if self.provider_total_tokens > self.model_context_window_tokens:
            raise ValueError("provider usage exceeds the model context window")
        if self.content_sha256 != (
            _text_sha256(self.content) if self.content is not None else None
        ):
            raise ValueError("provider response content hash mismatch")
        if self.refusal_sha256 != (
            _text_sha256(self.refusal) if self.refusal is not None else None
        ):
            raise ValueError("provider response refusal hash mismatch")
        payload = self.model_dump(mode="json", exclude={"response_sha256"})
        if self.response_sha256 != _sha256(payload):
            raise ValueError("provider response hash mismatch")
        return self


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
        except Exception:
            return None
        if isinstance(result, Mapping):
            return result
    return None


def _field(value: Any, name: str) -> Any:
    mapped = _mapping(value)
    if mapped is not None and name in mapped:
        return mapped[name]
    return getattr(value, name, None)


def _nonempty_optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider {field_name} must be nonempty text")
    return value


def _returned_model_is_compatible(requested: str, returned: str) -> bool:
    if requested not in SUPPORTED_CHAT_MODELS:
        return False
    if returned == requested:
        return True
    prefix = f"{requested}-"
    if not returned.startswith(prefix):
        return False
    snapshot_date = returned[len(prefix):]
    try:
        return date.fromisoformat(snapshot_date).isoformat() == snapshot_date
    except ValueError:
        return False


def _usage_count(usage: Any, primary: str, alternate: str) -> int:
    primary_value = _field(usage, primary)
    alternate_value = _field(usage, alternate)
    if primary_value is None:
        value = alternate_value
    elif alternate_value is None:
        value = primary_value
    elif primary_value == alternate_value:
        value = primary_value
    else:
        raise ValueError("provider usage aliases disagree")
    if type(value) is not int or value < 0:
        raise ValueError("provider usage value is invalid")
    return value


def _optional_usage_detail(
    usage: Any,
    *,
    primary_container: str,
    alternate_container: str,
    field_name: str,
) -> int:
    primary = _field(usage, primary_container)
    alternate = _field(usage, alternate_container)
    if primary is not None and alternate is not None:
        primary_value = _field(primary, field_name)
        alternate_value = _field(alternate, field_name)
        if (
            primary_value is not None
            and alternate_value is not None
            and primary_value != alternate_value
        ):
            raise ValueError("provider usage detail aliases disagree")
        value = (
            primary_value
            if primary_value is not None
            else alternate_value
        )
    else:
        details = primary if primary is not None else alternate
        value = _field(details, field_name) if details is not None else None
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise ValueError("provider usage detail is invalid")
    return value


def _response_from_provider(
    request: OpenAIChatRequestV1,
    provider_response: Any,
) -> OpenAIChatResponseV1:
    choices = _field(provider_response, "choices")
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise ValueError("provider response must contain exactly one choice")
    choice = choices[0]
    choice_index = _field(choice, "index")
    if type(choice_index) is not int or choice_index != 0:
        raise ValueError("provider response choice index is invalid")
    message = _field(choice, "message")
    if message is None:
        raise ValueError("provider response choice has no message")
    for unexpected_field in ("tool_calls", "function_call", "audio"):
        if _field(message, unexpected_field) is not None:
            raise ValueError("provider response contains an unexpected output form")
    content = _nonempty_optional_text(_field(message, "content"), "content")
    refusal = _nonempty_optional_text(_field(message, "refusal"), "refusal")
    if (content is None) == (refusal is None):
        raise ValueError("provider response must contain exactly one output form")
    if _field(message, "role") != "assistant":
        raise ValueError("provider response message role is invalid")
    response_id = _field(provider_response, "id")
    model = _field(provider_response, "model")
    finish_reason = _field(choice, "finish_reason")
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("provider response id is invalid")
    if not isinstance(model, str) or not model:
        raise ValueError("provider response model is invalid")
    if finish_reason != "stop":
        raise ValueError("provider response is incomplete or filtered")
    if not _returned_model_is_compatible(request.generation_config.model, model):
        raise ValueError("provider returned a different model family")
    usage = _field(provider_response, "usage")
    if usage is None:
        # The typed live path requires authoritative usage binding.  Streaming
        # or providers that omit usage need a separate, explicit adapter.
        raise ValueError("provider response usage is missing")
    input_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_count(usage, "total_tokens", "total_tokens")
    cached_input_tokens = _optional_usage_detail(
        usage,
        primary_container="prompt_tokens_details",
        alternate_container="input_tokens_details",
        field_name="cached_tokens",
    )
    reasoning_output_tokens = _optional_usage_detail(
        usage,
        primary_container="completion_tokens_details",
        alternate_container="output_tokens_details",
        field_name="reasoning_tokens",
    )
    if total_tokens != input_tokens + output_tokens:
        raise ValueError("provider usage totals do not reconcile")
    if cached_input_tokens > input_tokens:
        raise ValueError("provider cached input usage exceeds input usage")
    if reasoning_output_tokens > output_tokens:
        raise ValueError("provider reasoning usage exceeds output usage")
    if output_tokens > request.generation_config.max_completion_tokens:
        raise ValueError("provider output usage exceeds the request limit")
    if input_tokens > request.conservative_input_token_bound:
        raise ValueError("provider input usage exceeds the conservative bound")
    if total_tokens > MODEL_CONTEXT_WINDOW_TOKENS:
        raise ValueError("provider usage exceeds the model context window")
    payload: dict[str, Any] = {
        "contract_version": OPENAI_CHAT_RESPONSE_VERSION,
        "adapter_version": OPENAI_CHAT_ADAPTER_VERSION,
        "provider_request_sha256": request.request_sha256,
        "response_id": response_id,
        "requested_model": request.generation_config.model,
        "model": model,
        "finish_reason": finish_reason,
        "content": content,
        "refusal": refusal,
        "provider_input_tokens": input_tokens,
        "provider_cached_input_tokens": cached_input_tokens,
        "provider_output_tokens": output_tokens,
        "provider_reasoning_output_tokens": reasoning_output_tokens,
        "provider_total_tokens": total_tokens,
        "request_max_completion_tokens": (
            request.generation_config.max_completion_tokens
        ),
        "request_conservative_input_token_bound": (
            request.conservative_input_token_bound
        ),
        "model_context_window_tokens": MODEL_CONTEXT_WINDOW_TOKENS,
        "content_sha256": _text_sha256(content) if content is not None else None,
        "refusal_sha256": _text_sha256(refusal) if refusal is not None else None,
    }
    payload["response_sha256"] = _sha256(payload)
    return OpenAIChatResponseV1.model_validate_json(_canonical_json_bytes(payload))


class OpenAIChatCompletionsAdapterV1:
    """Build and execute a request directly from a trusted response plan.

    ``OpenAIChatRequestV1`` is an internal audit DTO, not an executable input
    boundary.  This prevents a self-rehashed request received from an
    untrusted caller from bypassing the trusted-plan checks.
    """

    def __init__(self, client: Any) -> None:
        if client is None:
            raise OpenAIChatProviderContractError("an OpenAI client is required")
        if not callable(getattr(client, "with_options", None)):
            raise OpenAIChatProviderContractError(
                "OpenAI client must support zero-retry request options"
            )
        self._client = client

    def complete(
        self,
        trusted_plan: TrustedResponsePlanV0_2,
        *,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> OpenAIChatResponseV1:
        request = OpenAIChatRequestV1.create(
            trusted_plan=trusted_plan,
            generation_config=generation_config,
        )
        verified = OpenAIChatRequestV1.from_wire_json(request.canonical_json_bytes())
        kwargs = verified.provider_kwargs()
        try:
            call_client = self._client.with_options(
                max_retries=0,
                timeout=verified.generation_config.timeout_seconds,
            )
            response = call_client.chat.completions.create(**kwargs)
        except Exception:
            raise OpenAIChatProviderError("OpenAI chat completion failed") from None
        try:
            return _response_from_provider(verified, response)
        except Exception:
            raise OpenAIChatProviderError(
                "OpenAI chat completion response was invalid"
            ) from None

    async def complete_async(
        self,
        trusted_plan: TrustedResponsePlanV0_2,
        *,
        generation_config: OpenAIChatGenerationConfigV1 | None = None,
    ) -> OpenAIChatResponseV1:
        config = generation_config or OpenAIChatGenerationConfigV1()
        try:
            config = OpenAIChatGenerationConfigV1.model_validate_json(
                config.model_dump_json()
            )
        except Exception:
            raise OpenAIChatProviderContractError(
                "invalid OpenAI generation configuration"
            ) from None
        wall_timeout = config.timeout_seconds + 2.0
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.complete,
                    trusted_plan,
                    generation_config=config,
                ),
                timeout=wall_timeout,
            )
        except asyncio.TimeoutError:
            raise OpenAIChatProviderError(
                "OpenAI chat completion exceeded its wall timeout"
            ) from None


__all__ = [
    "DEFAULT_CHAT_MODEL",
    "DEFAULT_MAX_COMPLETION_TOKENS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "DEFAULT_REASONING_EFFORT",
    "OPENAI_CHAT_ADAPTER_VERSION",
    "OpenAIChatCompletionsAdapterV1",
    "OpenAIChatGenerationConfigV1",
    "OpenAIChatMessageV1",
    "OpenAIChatProviderContractError",
    "OpenAIChatProviderError",
    "OpenAIChatRequestV1",
    "OpenAIChatResponseV1",
    "REFERENCE_DATA_MESSAGE_VERSION",
    "SUPPORTED_CHAT_MODELS",
    "safety_identifier_v1",
]
