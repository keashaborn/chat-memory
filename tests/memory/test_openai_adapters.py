"""OpenAI adapter contract tests with an injected fake transport only."""

from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any
import unittest

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
    sha256_hex,
)
from rag_engine.governed_memory.extraction import build_provider_request
from rag_engine.governed_memory.projection import DEFAULT_DIMENSIONS, EMBEDDING_MODEL
from rag_engine.governed_memory.runtime.https_transport import (
    HttpsOutcomeUnknown,
    HttpsRequest,
    HttpsRequestRejectedBeforeSend,
    HttpsResponse,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    DispatchReceipt,
    EMBEDDING_ENDPOINT_SHA256,
    EXTRACTION_SCHEMA_KEY,
    OpenAIBeforeSendFailure,
    OpenAIEmbeddingAdapter,
    OpenAIEndpointConfig,
    OpenAIOutcomeUnknownFailure,
    OpenAIResponsesAdapter,
    OpenAITerminalFailure,
    ProviderAssets,
    embedding_request_body_sha256,
    embedding_request_sha256,
    load_provider_assets,
)
from tests.memory._fixtures import (
    OWNER_A,
    PREDICATE_CATALOG,
    SOURCE_TEXT,
    make_extraction_job,
    make_provider_output,
    make_selected_evidence,
)


MODEL = "synthetic-extraction-model"
SYNTHETIC_TOKEN = "synthetic-not-a-real-openai-key"
ROOT = Path(__file__).resolve().parents[2]


class FakeTransport:
    def __init__(
        self,
        *,
        response: HttpsResponse | None = None,
        failure: Exception | None = None,
        events: list[object] | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.events = events if events is not None else []
        self.requests: list[HttpsRequest] = []

    def post(self, request: HttpsRequest) -> HttpsResponse:
        self.events.append("post")
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if self.response is None:
            raise AssertionError("fake_response_missing")
        return self.response


def response(body: dict[str, Any], *, status: int = 200) -> HttpsResponse:
    return HttpsResponse(
        status=status,
        headers=(("content-type", "application/json; charset=utf-8"),),
        body=json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def model_output(*, include_usage: bool = False) -> dict[str, Any]:
    output = make_provider_output()
    output.pop("usage")
    if include_usage:
        output["usage"] = {"input_tokens": 1, "output_tokens": 1}
    return output


def responses_body(
    output: dict[str, Any] | str,
    *,
    status: str = "completed",
    content_type: str = "output_text",
) -> dict[str, Any]:
    text = output if isinstance(output, str) else json.dumps(output, separators=(",", ":"))
    return {
        "id": "resp_synthetic_1",
        "incomplete_details": None,
        "model": MODEL,
        "object": "response",
        "output": [
            {
                "content": [{"text": text, "type": content_type}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            }
        ],
        "status": status,
        "usage": {
            "input_tokens": 37,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 19,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 56,
        },
    }


class OpenAIResponsesAdapterTests(unittest.TestCase):
    def request(self) -> dict[str, object]:
        return build_provider_request(
            make_extraction_job(),
            make_selected_evidence(),
            MODEL,
            EXTRACTION_SCHEMA_KEY,
            PREDICATE_CATALOG,
        )

    def config(self) -> OpenAIEndpointConfig:
        return OpenAIEndpointConfig(
            api_key=SYNTHETIC_TOKEN,
            extraction_model=MODEL,
        )

    def test_assets_are_actual_files_with_stable_raw_and_material_hashes(self) -> None:
        assets = load_provider_assets()
        asset_root = (
            ROOT
            / "rag_engine"
            / "governed_memory"
            / "provider_assets"
        )
        instruction_bytes = (asset_root / "extraction_instructions.txt").read_bytes()
        schema_bytes = (asset_root / "extraction_output.schema.json").read_bytes()
        self.assertEqual(assets.instructions_asset_sha256, sha256_hex(instruction_bytes))
        self.assertEqual(assets.output_schema_asset_sha256, sha256_hex(schema_bytes))
        self.assertEqual(
            assets.output_schema_material_sha256,
            sha256_hex(canonical_json_bytes(json.loads(schema_bytes))),
        )
        self.assertEqual(assets.output_schema_asset_bytes, schema_bytes)
        self.assertNotIn("usage", assets.output_schema["properties"])
        with self.assertRaises(TypeError):
            assets.output_schema["properties"]["facts"] = {}  # type: ignore[index]
        required = assets.output_schema["properties"]["facts"]["items"][  # type: ignore[index]
            "required"
        ]
        self.assertIsInstance(required, tuple)
        with self.assertRaises(AttributeError):
            required.append("mutated")  # type: ignore[attr-defined]

    def test_assets_detach_from_mutable_input_and_reject_hash_tampering(self) -> None:
        asset_root = ROOT / "rag_engine" / "governed_memory" / "provider_assets"
        instruction_bytes = (asset_root / "extraction_instructions.txt").read_bytes()
        schema_bytes = (asset_root / "extraction_output.schema.json").read_bytes()
        mutable_schema = json.loads(schema_bytes)
        assets = ProviderAssets(
            instructions=instruction_bytes.decode("utf-8"),
            output_schema=mutable_schema,
            output_schema_asset_bytes=schema_bytes,
            instructions_asset_sha256=sha256_hex(instruction_bytes),
            output_schema_asset_sha256=sha256_hex(schema_bytes),
            output_schema_material_sha256=sha256_hex(
                canonical_json_bytes(mutable_schema)
            ),
        )
        mutable_schema["properties"].clear()
        self.assertEqual(set(assets.output_schema["properties"]), {"facts", "schema"})

        object.__setattr__(
            assets,
            "instructions",
            f"{assets.instructions}\nmutated-after-validation",
        )
        events: list[object] = []
        adapter = OpenAIResponsesAdapter(
            config=self.config(),
            transport=FakeTransport(events=events),
            assets=assets,
        )
        with self.assertRaisesRegex(
            OpenAIBeforeSendFailure,
            "adapter_rejected_before_send",
        ):
            adapter.invoke(
                self.request(),
                mark_dispatched=lambda receipt: events.append(receipt),
            )
        self.assertEqual(events, [])

        with self.assertRaisesRegex(
            ContractViolation,
            "extraction_schema_asset_sha256_mismatch",
        ):
            ProviderAssets(
                instructions=instruction_bytes.decode("utf-8"),
                output_schema=json.loads(schema_bytes),
                output_schema_asset_bytes=schema_bytes,
                instructions_asset_sha256=sha256_hex(instruction_bytes),
                output_schema_asset_sha256="0" * 64,
                output_schema_material_sha256=sha256_hex(
                    canonical_json_bytes(json.loads(schema_bytes))
                ),
            )

    def test_prepared_responses_body_is_canonical_closed_and_external_only(self) -> None:
        adapter = OpenAIResponsesAdapter(
            config=self.config(),
            transport=FakeTransport(),
        )
        internal = self.request()
        prepared = adapter.prepare(internal)
        material = json.loads(prepared.request.body)
        self.assertEqual(prepared.request.body, canonical_json_bytes(material))
        self.assertEqual(
            set(material),
            {
                "input",
                "instructions",
                "max_output_tokens",
                "model",
                "store",
                "text",
                "tool_choice",
                "tools",
                "truncation",
            },
        )
        self.assertFalse(material["store"])
        self.assertEqual(material["tools"], [])
        self.assertEqual(material["tool_choice"], "none")
        self.assertEqual(material["truncation"], "disabled")
        self.assertTrue(material["text"]["format"]["strict"])
        self.assertEqual(material["text"]["format"]["type"], "json_schema")
        external = json.loads(material["input"][0]["content"][0]["text"])
        self.assertEqual(
            external,
            json.loads(canonical_json_bytes(internal["external_payload"])),
        )
        self.assertEqual(external["result_contract"]["top_level_keys"], ["facts", "schema"])
        self.assertNotIn("usage_keys", external["result_contract"])
        wire_text = prepared.request.body.decode("utf-8")
        for forbidden in (
            str(OWNER_A),
            str(internal["job_id"]),
            str(internal["provider_call_id"]),
            "owner_user_id",
            "operation_id",
        ):
            self.assertNotIn(forbidden, wire_text)
        self.assertRegex(prepared.receipt.request_body_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            prepared.receipt.instructions_asset_sha256,
            sha256_hex(material["instructions"].encode("utf-8")),
        )
        self.assertEqual(
            prepared.receipt.output_schema_material_sha256,
            sha256_hex(canonical_json_bytes(material["text"]["format"]["schema"])),
        )
        self.assertEqual(
            prepared.receipt.request_body_sha256,
            sha256_hex(prepared.request.body),
        )
        self.assertNotIn(SYNTHETIC_TOKEN, repr(prepared.request))

    def test_completed_output_uses_api_usage_and_validates_before_return(self) -> None:
        events: list[object] = []
        transport = FakeTransport(
            response=response(responses_body(model_output())),
            events=events,
        )
        adapter = OpenAIResponsesAdapter(config=self.config(), transport=transport)

        def marker(receipt: object) -> None:
            events.append(("marked", receipt))

        completion = adapter.invoke(self.request(), mark_dispatched=marker)
        self.assertEqual(events[0][0], "marked")
        self.assertEqual(events[1], "post")
        self.assertEqual(
            completion.normalized_output["usage"],
            {"input_tokens": 37, "output_tokens": 19},
        )
        self.assertEqual(completion.validated_result["receipt"]["proposal_count"], 1)
        self.assertEqual(completion.validated_result["receipt"]["input_tokens"], 37)

    def test_model_generated_usage_is_rejected_by_the_closed_output_boundary(self) -> None:
        adapter = OpenAIResponsesAdapter(
            config=self.config(),
            transport=FakeTransport(
                response=response(responses_body(model_output(include_usage=True)))
            ),
        )
        with self.assertRaisesRegex(OpenAITerminalFailure, "response_schema_violation"):
            adapter.invoke(self.request(), mark_dispatched=lambda _receipt: None)

    def test_returned_model_must_exactly_equal_configured_snapshot(self) -> None:
        mismatched = responses_body(model_output())
        mismatched["model"] = f"{MODEL}-different-snapshot"
        adapter = OpenAIResponsesAdapter(
            config=self.config(),
            transport=FakeTransport(response=response(mismatched)),
        )
        with self.assertRaisesRegex(OpenAITerminalFailure, "response_schema_violation"):
            adapter.invoke(self.request(), mark_dispatched=lambda _receipt: None)

    def test_refusal_incomplete_malformed_and_invalid_fact_fail_terminal(self) -> None:
        invalid_fact = model_output()
        invalid_fact["facts"][0]["predicate"] = "old.memory.predicate"
        inconsistent_completed = responses_body(model_output())
        inconsistent_completed["incomplete_details"] = {
            "reason": "max_output_tokens"
        }
        cases = (
            responses_body(model_output(), content_type="refusal"),
            responses_body(model_output(), status="incomplete"),
            inconsistent_completed,
            responses_body("not-json"),
            responses_body(invalid_fact),
        )
        for raw in cases:
            with self.subTest(raw=raw.get("status")):
                adapter = OpenAIResponsesAdapter(
                    config=self.config(),
                    transport=FakeTransport(response=response(raw)),
                )
                with self.assertRaises(OpenAITerminalFailure):
                    adapter.invoke(
                        self.request(),
                        mark_dispatched=lambda _receipt: None,
                    )

    def test_recursive_provider_json_is_a_stable_terminal_failure(self) -> None:
        nested = "[" * 2_000 + "0" + "]" * 2_000
        direct_response = HttpsResponse(
            status=200,
            headers=(("content-type", "application/json"),),
            body=(f'{{"nested":{nested}}}').encode("utf-8"),
        )
        model_response = response(responses_body(nested))
        for raw in (direct_response, model_response):
            with self.subTest(body_size=len(raw.body)):
                adapter = OpenAIResponsesAdapter(
                    config=self.config(),
                    transport=FakeTransport(response=raw),
                )
                with self.assertRaisesRegex(
                    OpenAITerminalFailure,
                    "response_schema_violation",
                ) as raised:
                    adapter.invoke(
                        self.request(),
                        mark_dispatched=lambda _receipt: None,
                    )
                self.assertEqual(raised.exception.disposition, "terminal_failure")

    def test_local_rejection_precedes_marker_and_transport(self) -> None:
        events: list[object] = []
        transport = FakeTransport(events=events)
        config = replace(self.config(), extraction_model="different-model")
        adapter = OpenAIResponsesAdapter(config=config, transport=transport)
        with self.assertRaisesRegex(OpenAIBeforeSendFailure, "adapter_rejected_before_send"):
            adapter.invoke(
                self.request(),
                mark_dispatched=lambda _receipt: events.append("marked"),
            )
        self.assertEqual(events, [])

    def test_transport_failure_after_marker_is_outcome_unknown_and_not_retried(self) -> None:
        events: list[object] = []
        transport = FakeTransport(
            failure=HttpsOutcomeUnknown("synthetic_after_dispatch"),
            events=events,
        )
        adapter = OpenAIResponsesAdapter(config=self.config(), transport=transport)
        with self.assertRaisesRegex(
            OpenAIOutcomeUnknownFailure, "provider_timeout_after_dispatch"
        ):
            adapter.invoke(
                self.request(),
                mark_dispatched=lambda _receipt: events.append("marked"),
            )
        self.assertEqual(events, ["marked", "post"])
        self.assertEqual(len(transport.requests), 1)

    def test_transport_before_send_rejection_after_marker_is_outcome_unknown(self) -> None:
        events: list[object] = []
        transport = FakeTransport(
            failure=HttpsRequestRejectedBeforeSend("synthetic_before_send_claim"),
            events=events,
        )
        adapter = OpenAIResponsesAdapter(config=self.config(), transport=transport)
        with self.assertRaisesRegex(OpenAIOutcomeUnknownFailure, "dispatch_crash") as raised:
            adapter.invoke(
                self.request(),
                mark_dispatched=lambda _receipt: events.append("marked"),
            )
        self.assertEqual(raised.exception.disposition, "outcome_unknown")
        self.assertEqual(events, ["marked", "post"])
        self.assertEqual(len(transport.requests), 1)


class OpenAIEmbeddingAdapterTests(unittest.TestCase):
    def test_embedding_body_hash_is_exact_canonical_request(self) -> None:
        self.assertEqual(
            embedding_request_body_sha256(
                "synthetic canonical relational fact"
            ),
            "acfdbf52a9201f941fcd897bc6b6a303e2c7e4f6820e9aa0465b01cc8a06ca54",
        )

    def test_embedding_request_hash_is_exact_content_free_contract(self) -> None:
        receipt = DispatchReceipt(
            operation="embeddings.create",
            model=EMBEDDING_MODEL,
            endpoint_sha256=EMBEDDING_ENDPOINT_SHA256,
            input_sha256="a" * 64,
            request_body_sha256="b" * 64,
        )
        self.assertEqual(
            embedding_request_sha256(receipt),
            "4b859f4be3de2906f3555f17af32f3d60559a95c66f2efb1d6ff2d622b89af1c",
        )

    def config(self) -> OpenAIEndpointConfig:
        return OpenAIEndpointConfig(
            api_key=SYNTHETIC_TOKEN,
            extraction_model=MODEL,
        )

    def embedding_response(
        self,
        *,
        vector: list[float] | None = None,
        model: str = EMBEDDING_MODEL,
        data_count: int = 1,
    ) -> HttpsResponse:
        selected = vector if vector is not None else [0.25] * DEFAULT_DIMENSIONS
        items = [
            {"embedding": selected, "index": index, "object": "embedding"}
            for index in range(data_count)
        ]
        return response(
            {
                "data": items,
                "model": model,
                "object": "list",
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            }
        )

    def test_embedding_request_is_exactly_one_fixed_3072_float_input(self) -> None:
        transport = FakeTransport(response=self.embedding_response())
        adapter = OpenAIEmbeddingAdapter(config=self.config(), transport=transport)
        completion = adapter.invoke(
            "canonical relational fact",
            mark_dispatched=lambda _receipt: None,
        )
        body = json.loads(transport.requests[0].body)
        self.assertEqual(
            body,
            {
                "dimensions": 3072,
                "encoding_format": "float",
                "input": ["canonical relational fact"],
                "model": "text-embedding-3-large",
            },
        )
        self.assertEqual(transport.requests[0].body, canonical_json_bytes(body))
        self.assertEqual(completion.model, EMBEDDING_MODEL)
        self.assertEqual(completion.dimensions, DEFAULT_DIMENSIONS)
        self.assertEqual(len(completion.vector), DEFAULT_DIMENSIONS)
        self.assertTrue(all(math.isfinite(value) for value in completion.vector))
        self.assertNotIn("owner", transport.requests[0].body.decode("utf-8"))

    def test_wrong_count_dimension_model_and_nonfinite_vector_fail_terminal(self) -> None:
        cases = (
            self.embedding_response(data_count=2),
            self.embedding_response(vector=[0.25] * (DEFAULT_DIMENSIONS - 1)),
            self.embedding_response(model="text-embedding-3-small"),
            self.embedding_response(
                vector=[float("nan")] + [0.25] * (DEFAULT_DIMENSIONS - 1)
            ),
        )
        for raw in cases:
            with self.subTest(body_size=len(raw.body)):
                adapter = OpenAIEmbeddingAdapter(
                    config=self.config(),
                    transport=FakeTransport(response=raw),
                )
                with self.assertRaises(OpenAITerminalFailure):
                    adapter.invoke("bounded input", mark_dispatched=lambda _receipt: None)

    def test_huge_embedding_number_is_a_stable_terminal_failure(self) -> None:
        huge_vector = [10**1_000] + [0.25] * (DEFAULT_DIMENSIONS - 1)
        adapter = OpenAIEmbeddingAdapter(
            config=self.config(),
            transport=FakeTransport(
                response=self.embedding_response(
                    vector=huge_vector  # type: ignore[arg-type]
                )
            ),
        )
        with self.assertRaisesRegex(
            OpenAITerminalFailure,
            "response_schema_violation",
        ) as raised:
            adapter.invoke("bounded input", mark_dispatched=lambda _receipt: None)
        self.assertEqual(raised.exception.disposition, "terminal_failure")


if __name__ == "__main__":
    unittest.main()
