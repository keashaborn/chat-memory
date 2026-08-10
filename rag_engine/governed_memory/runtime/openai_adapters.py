from __future__ import annotations

"""Dormant OpenAI Responses and embeddings adapters with injected transport."""

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
import json
import math
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..contracts import (
    ContractViolation,
    canonical_json_bytes,
    require_bounded_text,
    require_exact_int,
    require_sha256,
    sha256_hex,
    sha256_text,
)
from ..extraction import (
    ProviderRequestBinding,
    validate_provider_request_binding,
    validate_provider_result,
)
from ..projection import DEFAULT_DIMENSIONS, EMBEDDING_MODEL
from .https_transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    HttpsOutcomeUnknown,
    HttpsRequest,
    HttpsRequestRejectedBeforeSend,
    HttpsResponse,
    HttpsTransport,
    canonical_json_request_bytes,
    content_type_is_json,
    json_headers,
)


RESPONSES_URL = "https://api.openai.com/v1/responses"
EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
EXTRACTION_SCHEMA_KEY = "governed-memory-extraction"
EXTRACTION_FORMAT_NAME = "governed_memory_extraction_v1"
MAX_PROVIDER_OUTPUT_BYTES = 131_072
MAX_EMBEDDING_INPUT_BYTES = 32_000


class OpenAIAdapterFailure(RuntimeError):
    disposition = "terminal_failure"

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class OpenAIBeforeSendFailure(OpenAIAdapterFailure):
    disposition = "not_executed"


class OpenAIOutcomeUnknownFailure(OpenAIAdapterFailure):
    disposition = "outcome_unknown"


class OpenAITerminalFailure(OpenAIAdapterFailure):
    disposition = "terminal_failure"


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIEndpointConfig:
    api_key: str = field(repr=False)
    extraction_model: str
    responses_url: str = RESPONSES_URL
    embeddings_url: str = EMBEDDINGS_URL
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        # json_headers performs the closed token validation without exposing it.
        json_headers(bearer_token=self.api_key)
        require_bounded_text(
            self.extraction_model,
            code="invalid_openai_extraction_model",
            maximum_bytes=160,
            strip=True,
        )
        require_exact_int(
            self.max_response_bytes,
            code="invalid_openai_response_limit",
            minimum=1,
            maximum=4_194_304,
        )
        if self.responses_url != RESPONSES_URL or self.embeddings_url != EMBEDDINGS_URL:
            raise ContractViolation("openai_endpoint_mismatch")


def _closed_json_object(raw: bytes | str, *, code: str) -> dict[str, Any]:
    def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("nonfinite_json_number")
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ) as exc:
        raise ContractViolation(code) from exc
    if not isinstance(value, dict):
        raise ContractViolation(code)
    return value


def _deep_freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze_json(item) for item in value)
    if value is None or type(value) in {bool, int, str}:
        return value
    raise ContractViolation("provider_schema_json_invalid")


def _provider_schema_root(schema: Mapping[str, Any]) -> None:
    if set(schema) - {
        "$schema",
        "additionalProperties",
        "properties",
        "required",
        "type",
    }:
        raise ContractViolation("provider_schema_root_invalid")
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise ContractViolation("provider_schema_root_invalid")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != {"facts", "schema"}:
        raise ContractViolation("provider_schema_root_invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderAssets:
    instructions: str = field(repr=False)
    output_schema: Mapping[str, Any] = field(repr=False)
    output_schema_asset_bytes: bytes = field(repr=False)
    instructions_asset_sha256: str
    output_schema_asset_sha256: str
    output_schema_material_sha256: str

    def __post_init__(self) -> None:
        instructions = require_bounded_text(
            self.instructions,
            code="invalid_extraction_instructions",
            maximum_bytes=16_000,
        )
        schema_asset_bytes = self.output_schema_asset_bytes
        if (
            not isinstance(schema_asset_bytes, bytes)
            or not schema_asset_bytes
            or len(schema_asset_bytes) > 131_072
        ):
            raise ContractViolation("provider_schema_asset_bytes_invalid")
        schema_from_asset = _closed_json_object(
            schema_asset_bytes,
            code="provider_schema_json_invalid",
        )
        _provider_schema_root(schema_from_asset)
        try:
            schema_material = canonical_json_bytes(schema_from_asset)
            supplied_material = canonical_json_bytes(self.output_schema)
            frozen_schema = _deep_freeze_json(schema_from_asset)
        except ContractViolation:
            raise
        except (RecursionError, RuntimeError, TypeError) as exc:
            raise ContractViolation("provider_schema_json_invalid") from exc
        if supplied_material != schema_material:
            raise ContractViolation("provider_schema_material_mismatch")

        instruction_hash = require_sha256(
            self.instructions_asset_sha256,
            "invalid_extraction_instructions_sha256",
        )
        schema_asset_hash = require_sha256(
            self.output_schema_asset_sha256,
            "invalid_extraction_schema_asset_sha256",
        )
        schema_material_hash = require_sha256(
            self.output_schema_material_sha256,
            "invalid_extraction_schema_material_sha256",
        )
        if instruction_hash != sha256_text(instructions):
            raise ContractViolation("extraction_instructions_sha256_mismatch")
        if schema_asset_hash != sha256_hex(schema_asset_bytes):
            raise ContractViolation("extraction_schema_asset_sha256_mismatch")
        if schema_material_hash != sha256_hex(schema_material):
            raise ContractViolation("extraction_schema_material_sha256_mismatch")

        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(self, "output_schema", frozen_schema)
        object.__setattr__(
            self,
            "output_schema_asset_bytes",
            bytes(schema_asset_bytes),
        )


def _provider_assets_snapshot(assets: ProviderAssets) -> ProviderAssets:
    if not isinstance(assets, ProviderAssets):
        raise ContractViolation("invalid_provider_assets")
    return ProviderAssets(
        instructions=assets.instructions,
        output_schema=assets.output_schema,
        output_schema_asset_bytes=assets.output_schema_asset_bytes,
        instructions_asset_sha256=assets.instructions_asset_sha256,
        output_schema_asset_sha256=assets.output_schema_asset_sha256,
        output_schema_material_sha256=assets.output_schema_material_sha256,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchReceipt:
    operation: str
    model: str
    endpoint_sha256: str
    input_sha256: str
    request_body_sha256: str
    instructions_asset_sha256: str | None = None
    output_schema_asset_sha256: str | None = None
    output_schema_material_sha256: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedOpenAICall:
    request: HttpsRequest = field(repr=False)
    receipt: DispatchReceipt


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionCompletion:
    normalized_output: Mapping[str, Any] = field(repr=False)
    validated_result: Mapping[str, Any] = field(repr=False)
    dispatch_receipt: DispatchReceipt
    provider_response_id_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EmbeddingCompletion:
    vector: tuple[float, ...] = field(repr=False)
    dispatch_receipt: DispatchReceipt
    model: str
    dimensions: int
    input_tokens: int


DispatchMarker = Callable[[DispatchReceipt], None]


@lru_cache(maxsize=1)
def load_provider_assets() -> ProviderAssets:
    package = resources.files("rag_engine.governed_memory.provider_assets")
    instruction_bytes = package.joinpath("extraction_instructions.txt").read_bytes()
    schema_bytes = package.joinpath("extraction_output.schema.json").read_bytes()
    if (
        not instruction_bytes
        or len(instruction_bytes) > 16_000
        or not schema_bytes
        or len(schema_bytes) > 131_072
    ):
        raise ContractViolation("provider_asset_size_invalid")
    try:
        instructions = instruction_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation("provider_instruction_utf8_invalid") from exc
    schema = _closed_json_object(schema_bytes, code="provider_schema_json_invalid")
    return ProviderAssets(
        instructions=instructions,
        output_schema=schema,
        output_schema_asset_bytes=schema_bytes,
        instructions_asset_sha256=sha256_hex(instruction_bytes),
        output_schema_asset_sha256=sha256_hex(schema_bytes),
        output_schema_material_sha256=sha256_hex(canonical_json_bytes(schema)),
    )


def _dispatch(
    *,
    transport: HttpsTransport,
    prepared: PreparedOpenAICall,
    mark_dispatched: DispatchMarker,
) -> HttpsResponse:
    if not callable(mark_dispatched):
        raise OpenAIBeforeSendFailure("adapter_rejected_before_send")
    try:
        mark_dispatched(prepared.receipt)
    except Exception as exc:
        raise OpenAIBeforeSendFailure("adapter_rejected_before_send") from exc
    try:
        return transport.post(prepared.request)
    except HttpsRequestRejectedBeforeSend as exc:
        # The durable dispatch marker has already succeeded. A transport-level
        # claim that nothing was sent cannot move the operation back across
        # that boundary or make retry safe.
        raise OpenAIOutcomeUnknownFailure("dispatch_crash") from exc
    except HttpsOutcomeUnknown as exc:
        raise OpenAIOutcomeUnknownFailure(
            "provider_timeout_after_dispatch"
        ) from exc
    except Exception as exc:
        # An injected transport that violates the protocol cannot prove whether
        # it sent bytes. The only safe state is outcome_unknown.
        raise OpenAIOutcomeUnknownFailure("dispatch_crash") from exc


def _response_json(response: HttpsResponse) -> dict[str, Any]:
    if not isinstance(response, HttpsResponse):
        raise OpenAIOutcomeUnknownFailure("dispatch_crash")
    if response.status >= 500:
        raise OpenAIOutcomeUnknownFailure("provider_timeout_after_dispatch")
    if response.status != 200:
        reason = (
            "provider_auth_rejected"
            if response.status in {401, 403}
            else "provider_request_rejected"
        )
        raise OpenAITerminalFailure(reason)
    try:
        if not content_type_is_json(response):
            raise OpenAITerminalFailure("response_schema_violation")
    except ContractViolation as exc:
        raise OpenAITerminalFailure("response_schema_violation") from exc
    try:
        return _closed_json_object(
            response.body,
            code="openai_response_json_invalid",
        )
    except ContractViolation as exc:
        raise OpenAITerminalFailure("response_schema_violation") from exc


def _trusted_usage(
    response: Mapping[str, Any],
    *,
    max_output_tokens: int,
) -> dict[str, int]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise OpenAITerminalFailure("provider_usage_contract_violation")
    try:
        input_tokens = require_exact_int(
            usage.get("input_tokens"),
            code="invalid_openai_input_tokens",
            maximum=1_000_000,
        )
        output_tokens = require_exact_int(
            usage.get("output_tokens"),
            code="invalid_openai_output_tokens",
            maximum=max_output_tokens,
        )
    except ContractViolation as exc:
        raise OpenAITerminalFailure("provider_usage_contract_violation") from exc
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _completed_output_text(
    response: Mapping[str, Any],
    *,
    expected_model: str,
) -> tuple[str, str]:
    if response.get("object") != "response" or response.get("status") != "completed":
        raise OpenAITerminalFailure("response_schema_violation")
    if response.get("model") != expected_model:
        raise OpenAITerminalFailure("response_schema_violation")
    if response.get("incomplete_details") is not None:
        raise OpenAITerminalFailure("response_schema_violation")
    response_id = response.get("id")
    if not isinstance(response_id, str) or not 1 <= len(response_id) <= 256:
        raise OpenAITerminalFailure("response_schema_violation")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise OpenAITerminalFailure("response_schema_violation")
    messages = 0
    output_texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            raise OpenAITerminalFailure("response_schema_violation")
        if item.get("type") == "reasoning":
            continue
        if (
            item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("status") != "completed"
        ):
            raise OpenAITerminalFailure("response_schema_violation")
        messages += 1
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise OpenAITerminalFailure("response_schema_violation")
        for part in content:
            if not isinstance(part, Mapping):
                raise OpenAITerminalFailure("response_schema_violation")
            if part.get("type") == "refusal" or part.get("refusal") is not None:
                raise OpenAITerminalFailure("response_schema_violation")
            if part.get("type") != "output_text":
                raise OpenAITerminalFailure("response_schema_violation")
            text = part.get("text")
            if (
                not isinstance(text, str)
                or not text
                or len(text.encode("utf-8")) > MAX_PROVIDER_OUTPUT_BYTES
            ):
                raise OpenAITerminalFailure("response_schema_violation")
            output_texts.append(text)
    if messages != 1 or len(output_texts) != 1:
        raise OpenAITerminalFailure("response_schema_violation")
    return response_id, output_texts[0]


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        config: OpenAIEndpointConfig,
        transport: HttpsTransport,
        assets: ProviderAssets | None = None,
    ) -> None:
        if not isinstance(config, OpenAIEndpointConfig):
            raise ContractViolation("invalid_openai_config")
        if assets is not None and not isinstance(assets, ProviderAssets):
            raise ContractViolation("invalid_provider_assets")
        self._config = config
        self._transport = transport
        self._assets = assets if assets is not None else load_provider_assets()

    def prepare(self, request: Mapping[str, Any]) -> PreparedOpenAICall:
        try:
            assets = _provider_assets_snapshot(self._assets)
            binding = validate_provider_request_binding(request)
            if binding.model != self._config.extraction_model:
                raise ContractViolation("openai_extraction_model_mismatch")
            if binding.schema != EXTRACTION_SCHEMA_KEY:
                raise ContractViolation("openai_extraction_schema_mismatch")
            external_payload = request.get("external_payload")
            if not isinstance(external_payload, Mapping):
                raise ContractViolation("invalid_external_payload")
            result_contract = external_payload.get("result_contract")
            if not isinstance(result_contract, Mapping) or (
                result_contract.get("top_level_keys") != ["facts", "schema"]
                or "usage_keys" in result_contract
            ):
                raise ContractViolation("external_result_contract_mismatch")
            external_bytes = canonical_json_bytes(external_payload)
            external_text = external_bytes.decode("utf-8")
            body_material = {
                "input": [
                    {
                        "content": [{"text": external_text, "type": "input_text"}],
                        "role": "user",
                    }
                ],
                "instructions": assets.instructions,
                "max_output_tokens": binding.max_output_tokens,
                "model": binding.model,
                "store": False,
                "text": {
                    "format": {
                        "name": EXTRACTION_FORMAT_NAME,
                        "schema": assets.output_schema,
                        "strict": True,
                        "type": "json_schema",
                    }
                },
                "tool_choice": "none",
                "tools": [],
                "truncation": "disabled",
            }
            body = canonical_json_request_bytes(body_material)
            https_request = HttpsRequest(
                url=self._config.responses_url,
                headers=json_headers(bearer_token=self._config.api_key),
                body=body,
                max_response_bytes=self._config.max_response_bytes,
            )
        except (ContractViolation, HttpsRequestRejectedBeforeSend) as exc:
            raise OpenAIBeforeSendFailure("adapter_rejected_before_send") from exc
        receipt = DispatchReceipt(
            operation="responses.extraction",
            model=binding.model,
            endpoint_sha256=sha256_text(self._config.responses_url),
            input_sha256=require_sha256(
                request["external_payload_sha256"],
                "invalid_external_payload_sha256",
            ),
            request_body_sha256=sha256_hex(body),
            instructions_asset_sha256=assets.instructions_asset_sha256,
            output_schema_asset_sha256=assets.output_schema_asset_sha256,
            output_schema_material_sha256=assets.output_schema_material_sha256,
        )
        return PreparedOpenAICall(request=https_request, receipt=receipt)

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        mark_dispatched: DispatchMarker,
    ) -> ExtractionCompletion:
        prepared = self.prepare(request)
        response = _dispatch(
            transport=self._transport,
            prepared=prepared,
            mark_dispatched=mark_dispatched,
        )
        response_json = _response_json(response)
        response_id, output_text = _completed_output_text(
            response_json,
            expected_model=self._config.extraction_model,
        )
        try:
            model_output = _closed_json_object(
                output_text,
                code="openai_structured_output_invalid",
            )
        except ContractViolation as exc:
            raise OpenAITerminalFailure("response_schema_violation") from exc
        if set(model_output) != {"facts", "schema"}:
            # This explicitly rejects model-produced usage or any other field.
            raise OpenAITerminalFailure("response_schema_violation")
        normalized_output: dict[str, Any] = {
            "schema": model_output["schema"],
            "facts": model_output["facts"],
            "usage": _trusted_usage(
                response_json,
                max_output_tokens=validate_provider_request_binding(
                    request
                ).max_output_tokens,
            ),
        }
        try:
            validated = validate_provider_result(request, normalized_output)
        except ContractViolation as exc:
            raise OpenAITerminalFailure("response_schema_violation") from exc
        return ExtractionCompletion(
            normalized_output=normalized_output,
            validated_result=validated,
            dispatch_receipt=prepared.receipt,
            provider_response_id_sha256=sha256_text(response_id),
        )


class OpenAIEmbeddingAdapter:
    def __init__(
        self,
        *,
        config: OpenAIEndpointConfig,
        transport: HttpsTransport,
    ) -> None:
        if not isinstance(config, OpenAIEndpointConfig):
            raise ContractViolation("invalid_openai_config")
        self._config = config
        self._transport = transport

    def prepare(self, text: str) -> PreparedOpenAICall:
        try:
            bounded = require_bounded_text(
                text,
                code="invalid_embedding_input",
                maximum_bytes=MAX_EMBEDDING_INPUT_BYTES,
            )
            body = canonical_json_request_bytes(
                {
                    "dimensions": DEFAULT_DIMENSIONS,
                    "encoding_format": "float",
                    "input": [bounded],
                    "model": EMBEDDING_MODEL,
                }
            )
            request = HttpsRequest(
                url=self._config.embeddings_url,
                headers=json_headers(bearer_token=self._config.api_key),
                body=body,
                max_response_bytes=self._config.max_response_bytes,
            )
        except (ContractViolation, HttpsRequestRejectedBeforeSend) as exc:
            raise OpenAIBeforeSendFailure("adapter_rejected_before_send") from exc
        receipt = DispatchReceipt(
            operation="embeddings.create",
            model=EMBEDDING_MODEL,
            endpoint_sha256=sha256_text(self._config.embeddings_url),
            input_sha256=sha256_text(bounded),
            request_body_sha256=sha256_hex(body),
        )
        return PreparedOpenAICall(request=request, receipt=receipt)

    def invoke(
        self,
        text: str,
        *,
        mark_dispatched: DispatchMarker,
    ) -> EmbeddingCompletion:
        prepared = self.prepare(text)
        response = _dispatch(
            transport=self._transport,
            prepared=prepared,
            mark_dispatched=mark_dispatched,
        )
        response_json = _response_json(response)
        if response_json.get("object") != "list":
            raise OpenAITerminalFailure("response_schema_violation")
        if response_json.get("model") != EMBEDDING_MODEL:
            raise OpenAITerminalFailure("response_schema_violation")
        data = response_json.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise OpenAITerminalFailure("response_schema_violation")
        item = data[0]
        if (
            not isinstance(item, Mapping)
            or item.get("object") != "embedding"
            or item.get("index") != 0
        ):
            raise OpenAITerminalFailure("response_schema_violation")
        vector = item.get("embedding")
        if not isinstance(vector, list) or len(vector) != DEFAULT_DIMENSIONS:
            raise OpenAITerminalFailure("response_schema_violation")
        checked: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OpenAITerminalFailure("response_schema_violation")
            try:
                numeric = float(value)
            except (OverflowError, ValueError) as exc:
                raise OpenAITerminalFailure("response_schema_violation") from exc
            if not math.isfinite(numeric):
                raise OpenAITerminalFailure("response_schema_violation")
            checked.append(numeric)
        usage = response_json.get("usage")
        if not isinstance(usage, Mapping):
            raise OpenAITerminalFailure("provider_usage_contract_violation")
        try:
            input_tokens = require_exact_int(
                usage.get("prompt_tokens"),
                code="invalid_embedding_input_tokens",
                maximum=1_000_000,
            )
            total_tokens = require_exact_int(
                usage.get("total_tokens"),
                code="invalid_embedding_total_tokens",
                maximum=1_000_000,
            )
        except ContractViolation as exc:
            raise OpenAITerminalFailure(
                "provider_usage_contract_violation"
            ) from exc
        if total_tokens != input_tokens:
            raise OpenAITerminalFailure("provider_usage_contract_violation")
        return EmbeddingCompletion(
            vector=tuple(checked),
            dispatch_receipt=prepared.receipt,
            model=EMBEDDING_MODEL,
            dimensions=DEFAULT_DIMENSIONS,
            input_tokens=input_tokens,
        )


__all__ = [
    "DispatchReceipt",
    "EmbeddingCompletion",
    "EMBEDDINGS_URL",
    "ExtractionCompletion",
    "EXTRACTION_FORMAT_NAME",
    "EXTRACTION_SCHEMA_KEY",
    "load_provider_assets",
    "OpenAIAdapterFailure",
    "OpenAIBeforeSendFailure",
    "OpenAIEmbeddingAdapter",
    "OpenAIEndpointConfig",
    "OpenAIOutcomeUnknownFailure",
    "OpenAIResponsesAdapter",
    "OpenAITerminalFailure",
    "PreparedOpenAICall",
    "ProviderAssets",
    "RESPONSES_URL",
]
