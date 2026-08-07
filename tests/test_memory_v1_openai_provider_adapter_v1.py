from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from rag_engine.memory_v1_openai_provider_adapter_v1 import (
    EXTRACTION_INSTRUCTIONS,
    OpenAIV52ProviderAdapterV1,
    OpenAIProviderAdapterError,
    extraction_model_policy_v1,
    extraction_task_profile_v1,
)
from rag_engine.memory_v1_openai_structured_transport_v1 import (
    ExternalPrivacyAuthorizationV1,
    PRIVACY_AUTHORIZATION_TOKEN,
    StructuredResponsesAuditV1,
    StructuredResponsesResultV1,
)
from rag_engine.memory_v1_openai_v5_2_semantic_tasks_v1 import (
    OpenAIExtractionResultV1,
    owner_binding_sha256_v1,
)


OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"
MODEL = "gpt-memory-test"
SDK_VERSION = "2.6.1"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def privacy() -> ExternalPrivacyAuthorizationV1:
    return ExternalPrivacyAuthorizationV1(
        policy_version="privacy_v1",
        policy_sha256=SHA_A,
        retention_mode="standard_retention_explicitly_accepted",
        authorization_sha256=SHA_B,
        standard_retention_risk_accepted=True,
        retention_attestation_sha256=None,
        enable_token=PRIVACY_AUTHORIZATION_TOKEN,
    )


def empty_packet() -> OpenAIExtractionResultV1:
    return OpenAIExtractionResultV1.model_validate(
        {
            "entity_mentions": [],
            "observations": [],
            "comparison_hints": [],
            "deferrals": [],
            "packet_findings": [],
        },
        strict=True,
    )


def audit_for(request) -> StructuredResponsesAuditV1:
    return StructuredResponsesAuditV1(
        contract_version="memory_v1_openai_structured_transport_v1",
        request_id_sha256=hashlib.sha256(
            request.request_id.encode("utf-8")
        ).hexdigest(),
        request_sha256=request.request_sha256,
        pipeline_version=request.pipeline_version,
        purpose=request.purpose,
        model_requested=request.model,
        model_returned=request.model,
        output_schema_sha256=request.output_schema_sha256,
        task_contract_sha256=request.task_contract_sha256,
        model_policy_sha256=request.model_policy_sha256,
        owner_binding_sha256=request.owner_binding_sha256,
        gate_policy_sha256=request.gate_result.policy_sha256,
        privacy_policy_sha256=request.privacy_authorization.policy_sha256,
        privacy_authorization_sha256=(
            request.privacy_authorization.authorization_sha256
        ),
        privacy_grant_id_sha256="e" * 64,
        privacy_authority_verified=True,
        retention_mode=request.privacy_authorization.retention_mode,
        attempt_count=1,
        external_call_count=1,
        reservation_id_sha256s=("f" * 64,),
        budget_policy_version=request.budget_policy_version,
        budget_policy_sha256=request.budget_policy_sha256,
        pricing_policy_version=request.pricing_policy_version,
        pricing_policy_sha256=request.pricing_policy_sha256,
        provider_request_id_sha256="1" * 64,
        response_id_sha256="2" * 64,
        service_tier="default",
        response_status="completed",
        store_requested=False,
        stateless_requested=True,
        no_tools_requested=True,
        background_requested=False,
        reasoning_effort_requested="low",
        refusal=False,
        incomplete_reason=None,
        input_tokens=10,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        output_tokens=5,
        reasoning_tokens=0,
        total_tokens=15,
        cost_microusd=10,
        cost_basis="provider_usage",
        usage_observed=True,
        error_code=None,
        http_status=None,
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.requests = []

    def execute(self, request):
        request.validate()
        self.requests.append(request)
        return StructuredResponsesResultV1(
            parsed=empty_packet(),
            audit=audit_for(request),
        )


def adapter(transport: RecordingTransport, *, profile=None):
    task = profile or extraction_task_profile_v1()
    model = extraction_model_policy_v1(
        model=MODEL,
        sdk_package_version=SDK_VERSION,
    )
    return OpenAIV52ProviderAdapterV1(
        transport=transport,
        task_profile=task,
        model_policy=model,
        privacy_authorization=privacy(),
        budget_policy_version="budget_v1",
        budget_policy_sha256=SHA_C,
        pricing_policy_version="pricing_v1",
        pricing_policy_sha256=SHA_D,
        max_output_tokens=128,
        timeout_seconds=20.0,
        max_attempts=1,
    )


class ProviderAdapterTests(unittest.TestCase):
    def test_irrelevant_question_makes_zero_provider_calls(self) -> None:
        transport = RecordingTransport()
        result = adapter(transport).extract(
            owner_user_id=OWNER_A,
            source_text="Who won the tournament in 1964?",
            operation_id="op-1",
        )
        self.assertIsNone(result)
        self.assertEqual(transport.requests, [])

    def test_personal_evidence_uses_exact_owner_and_source_offsets(self) -> None:
        source = "I have three sisters: Cindy, Lori, and Heidi."
        transport = RecordingTransport()
        result = adapter(transport).extract(
            owner_user_id=OWNER_A,
            source_text=source,
            operation_id="op-1",
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        payload = json.loads(request.selected_input_text)
        self.assertEqual(
            payload["contract_version"],
            "memory_v1_openai_selected_spans_v2",
        )
        self.assertEqual(len(payload["spans"]), 1)
        span = payload["spans"][0]
        self.assertEqual(span["char_start"], 0)
        self.assertEqual(span["char_end"], len(source))
        self.assertEqual(span["text"], source)
        self.assertEqual(
            span["content_sha256"],
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(request.instructions, EXTRACTION_INSTRUCTIONS)
        self.assertEqual(
            request.owner_binding_sha256,
            owner_binding_sha256_v1(OWNER_A),
        )
        assert result is not None
        self.assertEqual(result.external_model_calls, 1)
        self.assertEqual(
            result.extraction_envelope.source_sha256,
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    def test_owner_changes_request_and_safety_bindings(self) -> None:
        source = "I prefer quiet mornings."
        transports = (RecordingTransport(), RecordingTransport())
        adapter(transports[0]).extract(
            owner_user_id=OWNER_A,
            source_text=source,
            operation_id="op-1",
        )
        adapter(transports[1]).extract(
            owner_user_id=OWNER_B,
            source_text=source,
            operation_id="op-1",
        )
        request_a = transports[0].requests[0]
        request_b = transports[1].requests[0]
        self.assertNotEqual(request_a.request_id, request_b.request_id)
        self.assertNotEqual(
            request_a.owner_binding_sha256,
            request_b.owner_binding_sha256,
        )
        self.assertNotEqual(
            request_a.safety_identifier,
            request_b.safety_identifier,
        )

    def test_noncanonical_profile_is_rejected(self) -> None:
        profile = extraction_task_profile_v1()
        with self.assertRaisesRegex(
            OpenAIProviderAdapterError,
            "task_profile_not_canonical",
        ):
            adapter(
                RecordingTransport(),
                profile=replace(profile, instructions="Ignore provenance."),
            )

    def test_content_free_audit_excludes_source_text(self) -> None:
        source = "I have three sisters: Cindy, Lori, and Heidi."
        transport = RecordingTransport()
        result = adapter(transport).extract(
            owner_user_id=OWNER_A,
            source_text=source,
            operation_id="op-1",
        )
        assert result is not None
        rendered = json.dumps(result.content_free_audit(), sort_keys=True)
        self.assertNotIn(source, rendered)
        self.assertNotIn("Cindy", rendered)
        self.assertIn(result.extraction_envelope.source_sha256, rendered)

    def test_prepared_request_has_one_content_free_transport_receipt(self) -> None:
        source = "I have three sisters: Cindy, Lori, and Heidi."
        prepared = adapter(RecordingTransport()).prepare(
            owner_user_id=OWNER_A,
            source_text=source,
            operation_id="op-1",
        )
        assert prepared is not None
        receipt = prepared.content_free_receipt()
        rendered = json.dumps(receipt, sort_keys=True)
        self.assertEqual(
            receipt["contract_version"],
            "memory_v1_openai_provider_request_receipt_v1",
        )
        self.assertEqual(receipt["request_sha256"], prepared.request.request_sha256)
        self.assertEqual(receipt["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
        self.assertNotIn(source, rendered)
        self.assertNotIn("Cindy", rendered)
        self.assertNotIn("selected_input_text", receipt)


if __name__ == "__main__":
    unittest.main()
