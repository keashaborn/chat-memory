#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
    OpenAIResponsesProvider,
    OpenAIResponsesTransport,
    ProviderAdapterError,
    ResponsesResult,
    StaticResponsesTransport,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    validate_and_normalize,
)


REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "c1d613b16795c181780d60219860f8068cee1369b069735db94887b0c1b8b377"
)
CONTENT = "Synthetic fixture-only extraction source."
CONTENT_SHA256 = (
    "e7a9aa3849aee9aed7c5914fe1ff9140f7b30d5ee3e5b92e0553c821a436d29f"
)
ALLOWED_PROVIDER = {OPENAI_PROVIDER_ID: OPENAI_PROVIDER_VERSION}


def provider_output() -> dict[str, Any]:
    return {
        "entity_mentions": [],
        "observations": [],
        "comparison_hints": [],
        "deferrals": [
            {
                "reason_code": "insufficient_evidence",
                "memory_shape": "none",
                "source_spans": [
                    {
                        "start": 0,
                        "end": 41,
                        "quote": CONTENT,
                    }
                ],
                "sensitivity": "low",
            }
        ],
        "packet_findings": ["static_openai_provider_test"],
    }


def expect_error(callback, code: str) -> ProviderAdapterError:
    try:
        callback()
    except ProviderAdapterError as exc:
        if exc.code != code:
            raise AssertionError(
                f"expected {code}, received {exc.code}"
            ) from exc
        return exc
    raise AssertionError(f"{code} was not raised")


def expect_any_error(callback, label: str) -> None:
    try:
        callback()
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


class FakeResponses:
    def __init__(
        self,
        *,
        response: Any | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def parse(
        self,
        *,
        model: str,
        instructions: str,
        input: str,
        text_format: type[ProviderPacket],
        store: bool,
        metadata: dict[str, str],
        max_output_tokens: int,
        timeout: float,
    ) -> Any:
        self.calls.append(
            {
                "model": model,
                "instructions": instructions,
                "input": input,
                "text_format": text_format,
                "store": store,
                "metadata": metadata,
                "max_output_tokens": max_output_tokens,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("redacted")
        self.status_code = status_code


def response(
    *,
    parsed: Any = None,
    status: str = "completed",
    refusal: bool = False,
    incomplete_reason: str | None = None,
) -> SimpleNamespace:
    content = (
        [SimpleNamespace(type="refusal", refusal="redacted")]
        if refusal
        else [SimpleNamespace(type="output_text")]
    )
    return SimpleNamespace(
        id="resp_static_test",
        status=status,
        output=[SimpleNamespace(type="message", content=content)],
        output_parsed=parsed,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason)
            if incomplete_reason
            else None
        ),
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    registry = load_registry(
        repo_root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        repo_root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    source = TrustedExtractionSource.create(
        job_id="e4444444-4444-4444-8444-444444444444",
        source_system="public.chat_log",
        source_external_id="e3333333-3333-4333-8333-333333333333",
        source_sha256=CONTENT_SHA256,
        source_recorded_at="2026-07-16T20:30:00Z",
        content=CONTENT,
    )

    static_transport = StaticResponsesTransport(
        result=ResponsesResult(
            response_id="resp_static",
            status="completed",
            parsed=provider_output(),
            refusal=False,
            incomplete_reason=None,
        )
    )
    static_provider = OpenAIResponsesProvider(
        model="test-model-static",
        registry=registry,
        transport=static_transport,
    )
    expect_any_error(
        lambda: validate_and_normalize(
            static_provider,
            source=source,
            registry=registry,
            schema=schema,
        ),
        "default provider allowlist bypass",
    )
    if static_transport.requests:
        raise AssertionError("disabled provider was called before allowlist approval")
    validated = validate_and_normalize(
        static_provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions=ALLOWED_PROVIDER,
        max_external_model_calls=0,
    )
    if validated.external_model_calls != 0:
        raise AssertionError("static transport reported an external call")
    request = static_transport.requests[0]
    if request.store is not False:
        raise AssertionError("Responses request is stateful")
    if request.text_format is not ProviderPacket:
        raise AssertionError("Responses request lost its Pydantic output type")
    if source.job_id in request.input_text:
        raise AssertionError("job ID leaked into provider input")
    if source.source_external_id in request.input_text:
        raise AssertionError("source external ID leaked into provider input")
    if source.source_sha256 in request.input_text:
        raise AssertionError("source hash leaked into provider input")
    if "owner_user_id" in request.input_text or "vantage_id" in request.input_text:
        raise AssertionError("owner namespace leaked into provider input")
    if request.metadata != {
        "pipeline": "memory_v1_relational_extraction_v5",
        "provider": "v1",
    }:
        raise AssertionError("provider metadata contains unexpected identifiers")
    if len(request.output_schema_sha256) != 64:
        raise AssertionError("provider output schema was not fingerprinted")

    disabled_fake = FakeResponses(
        response=response(parsed=provider_output())
    )
    disabled_transport = OpenAIResponsesTransport(
        client=FakeClient(disabled_fake)
    )
    disabled_provider = OpenAIResponsesProvider(
        model="test-model-disabled",
        registry=registry,
        transport=disabled_transport,
    )
    expect_any_error(
        lambda: validate_and_normalize(
            disabled_provider,
            source=source,
            registry=registry,
            schema=schema,
            allowed_provider_versions=ALLOWED_PROVIDER,
            max_external_model_calls=0,
        ),
        "zero-call budget bypass",
    )
    if disabled_fake.calls:
        raise AssertionError("zero-call budget reached the transport")
    expect_error(
        lambda: disabled_provider.extract(source),
        "external_provider_disabled",
    )
    if disabled_fake.calls:
        raise AssertionError("disabled transport reached the fake client")

    enabled_fake = FakeResponses(
        response=response(parsed=provider_output())
    )
    enabled_transport = OpenAIResponsesTransport(
        enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
        client=FakeClient(enabled_fake),
    )
    enabled_provider = OpenAIResponsesProvider(
        model="test-model-enabled",
        registry=registry,
        transport=enabled_transport,
        max_output_tokens=4000,
        timeout_seconds=30,
    )
    live_shape = validate_and_normalize(
        enabled_provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions=ALLOWED_PROVIDER,
        max_external_model_calls=1,
    )
    if live_shape.external_model_calls != 1:
        raise AssertionError("enabled fake transport call was not audited")
    if len(enabled_fake.calls) != 1:
        raise AssertionError("enabled fake transport call count changed")
    call = enabled_fake.calls[0]
    if call["store"] is not False or call["text_format"] is not ProviderPacket:
        raise AssertionError("SDK-shaped request lost store/schema controls")
    if call["max_output_tokens"] != 4000 or call["timeout"] != 30.0:
        raise AssertionError("SDK-shaped request limits changed")
    if enabled_provider.last_audit != {
        "provider_id": "openai_responses",
        "provider_version": "v1",
        "model": "test-model-enabled",
        "store": False,
        "output_schema_sha256": enabled_provider.request(
            source
        ).output_schema_sha256,
        "response_id": "resp_static_test",
        "response_status": "completed",
        "incomplete_reason": None,
        "refusal": False,
        "error_code": None,
    }:
        raise AssertionError("successful provider audit changed")

    refusal_provider = OpenAIResponsesProvider(
        model="test-model-refusal",
        registry=registry,
        transport=StaticResponsesTransport(
            result=ResponsesResult(
                response_id="resp_refusal",
                status="completed",
                parsed=None,
                refusal=True,
                incomplete_reason=None,
            )
        ),
    )
    expect_error(
        lambda: refusal_provider.extract(source),
        "model_refusal",
    )
    incomplete_provider = OpenAIResponsesProvider(
        model="test-model-incomplete",
        registry=registry,
        transport=StaticResponsesTransport(
            result=ResponsesResult(
                response_id="resp_incomplete",
                status="incomplete",
                parsed=None,
                refusal=False,
                incomplete_reason="max_output_tokens",
            )
        ),
    )
    error = expect_error(
        lambda: incomplete_provider.extract(source),
        "incomplete_response",
    )
    if error.retryable is not True:
        raise AssertionError("incomplete response was classified non-retryable")
    missing_provider = OpenAIResponsesProvider(
        model="test-model-missing",
        registry=registry,
        transport=StaticResponsesTransport(
            result=ResponsesResult(
                response_id="resp_missing",
                status="completed",
                parsed=None,
                refusal=False,
                incomplete_reason=None,
            )
        ),
    )
    expect_error(
        lambda: missing_provider.extract(source),
        "missing_parsed_output",
    )
    invalid = provider_output()
    invalid["owner_user_id"] = source.job_id
    invalid_provider = OpenAIResponsesProvider(
        model="test-model-invalid",
        registry=registry,
        transport=StaticResponsesTransport(
            result=ResponsesResult(
                response_id="resp_invalid",
                status="completed",
                parsed=invalid,
                refusal=False,
                incomplete_reason=None,
            )
        ),
    )
    expect_error(
        lambda: invalid_provider.extract(source),
        "invalid_structured_output",
    )

    timeout_fake = FakeResponses(error=TimeoutError("redacted"))
    timeout_provider = OpenAIResponsesProvider(
        model="test-model-timeout",
        registry=registry,
        transport=OpenAIResponsesTransport(
            enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
            client=FakeClient(timeout_fake),
        ),
    )
    error = expect_error(
        lambda: timeout_provider.extract(source),
        "transport_timeout",
    )
    if error.retryable is not True:
        raise AssertionError("timeout was classified non-retryable")
    if timeout_provider.last_audit is None or (
        timeout_provider.last_audit["error_code"] != "transport_timeout"
    ):
        raise AssertionError("timeout audit did not retain the error class")

    rate_fake = FakeResponses(error=StatusError(429))
    rate_provider = OpenAIResponsesProvider(
        model="test-model-rate",
        registry=registry,
        transport=OpenAIResponsesTransport(
            enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
            client=FakeClient(rate_fake),
        ),
    )
    error = expect_error(
        lambda: rate_provider.extract(source),
        "transport_rate_limited",
    )
    if error.retryable is not True or error.http_status != 429:
        raise AssertionError("rate limit classification changed")

    expect_any_error(
        lambda: OpenAIResponsesProvider(
            model="",
            registry=registry,
            transport=static_transport,
        ),
        "empty model identifier",
    )
    expect_any_error(
        lambda: OpenAIResponsesProvider(
            model="test-model",
            registry=registry,
            transport=static_transport,
            timeout_seconds=0,
        ),
        "zero timeout",
    )

    print("memory_v1_relational_extraction_v5_openai_provider_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
