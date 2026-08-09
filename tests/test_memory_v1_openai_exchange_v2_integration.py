from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    build_memory_evidence_context_envelope_v2,
)
from rag_engine.memory_v1_openai_provider_adapter_v1 import (
    OpenAIV52ProviderAdapterV1,
    extraction_model_policy_v1,
    extraction_task_profile_v1,
)
from rag_engine.memory_v1_openai_structured_transport_v1 import (
    ExternalPrivacyAuthorizationV1,
    PRIVACY_AUTHORIZATION_TOKEN,
    StructuredTransportError,
)


OWNER = "00000000-0000-4000-8000-000000000001"
THREAD = "2f6e6a6c-f43e-45e2-8137-8b1b01e67976"
SOURCE = "ed91f3b9-a4b8-4e63-aac7-53e370412483"
TARGET = "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964"
REQUEST = "bfca3e63-e670-4601-a06d-6345c18554f4"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def context_for(target_text: str, assistant_question: str):
    sibling = build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=OWNER,
        target_evidence_id=TARGET,
        expected_target_content_sha256=sha(target_text),
        source_row={
            "id": SOURCE,
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "request_id": REQUEST,
            "created_at": "2026-08-07T12:00:00+00:00",
            "text": target_text,
        },
        evidence_rows=[{
            "evidence_id": TARGET,
            "owner_user_id": OWNER,
            "source_system": "public.chat_log",
            "content": target_text,
            "content_sha256": sha(target_text),
            "metadata": {
                "source_id": SOURCE,
                "thread_id": THREAD,
                "request_id": REQUEST,
                "source_content_sha256": sha(target_text),
                "source_char_start": 0,
                "source_char_end": len(target_text),
                "primary_lane": "personal_history",
                "epistemic_role": "user_report",
                "span_origin": "atomic",
            },
        }],
    )
    return build_memory_evidence_context_envelope_v2(
        sibling_context=sibling,
        prior_turn_rows=[{
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "request_id": "request-2",
            "created_at": "2026-08-07T11:59:00+00:00",
            "source": "frontend/chat:assistant",
            "text": assistant_question,
        }],
    )


def adapter() -> OpenAIV52ProviderAdapterV1:
    return OpenAIV52ProviderAdapterV1(
        transport=None,
        task_profile=extraction_task_profile_v1(),
        model_policy=extraction_model_policy_v1(
            model="gpt-memory-test",
            sdk_package_version="2.6.1",
        ),
        privacy_authorization=ExternalPrivacyAuthorizationV1(
            policy_version="privacy_v1",
            policy_sha256="a" * 64,
            retention_mode="standard_retention_explicitly_accepted",
            authorization_sha256="b" * 64,
            standard_retention_risk_accepted=True,
            retention_attestation_sha256=None,
            enable_token=PRIVACY_AUTHORIZATION_TOKEN,
        ),
        budget_policy_version="budget_v1",
        budget_policy_sha256="c" * 64,
        pricing_policy_version="pricing_v1",
        pricing_policy_sha256="d" * 64,
        max_output_tokens=128,
        timeout_seconds=20.0,
        max_attempts=1,
    )


class OpenAIExchangeV2IntegrationTest(unittest.TestCase):
    def test_contextual_answer_uses_canonical_compiled_payload(self) -> None:
        text = "Three."
        context = context_for(text, "How many sisters do you have?")
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text=text,
            operation_id="op-context-1",
            exchange_eligibility=True,
            evidence_context=context,
        )
        self.assertIsNotNone(prepared)
        assert prepared is not None
        prepared.request.validate()
        payload = json.loads(prepared.request.selected_input_text)
        self.assertEqual(
            payload["contract_version"],
            "memory_v1_openai_v5_2_extraction_payload_v1",
        )
        self.assertEqual(
            payload["context_binding_sha256"],
            context.envelope_sha256,
        )
        self.assertEqual(
            payload["selected_source_spans"][0]["content"],
            text,
        )
        self.assertEqual(len(payload["context"]["items"]), 1)
        context_item = payload["context"]["items"][0]
        self.assertEqual(context_item["speaker_role"], "assistant")
        self.assertFalse(context_item["assertion_origin_allowed"])
        self.assertFalse(context_item["instruction_capability"])

    def test_context_removal_breaks_gate_authority(self) -> None:
        text = "Three."
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text=text,
            operation_id="op-context-2",
            exchange_eligibility=True,
            evidence_context=context_for(
                text,
                "How many sisters do you have?",
            ),
        )
        assert prepared is not None
        tampered = replace(
            prepared.request,
            evidence_context_authority=None,
        )
        with self.assertRaisesRegex(
            StructuredTransportError,
            "compiled_input_authority_mismatch",
        ):
            tampered.validate()

    def test_structured_payload_cannot_diverge_from_compiler_authority(
        self,
    ) -> None:
        text = "Three."
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text=text,
            operation_id="op-context-2b",
            exchange_eligibility=True,
            evidence_context=context_for(
                text,
                "How many sisters do you have?",
            ),
        )
        assert prepared is not None
        payload = json.loads(prepared.request.structured_input_text or "")
        payload["context"]["items"] = []
        tampered = replace(
            prepared.request,
            structured_input_text=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        with self.assertRaisesRegex(
            StructuredTransportError,
            "compiled_input_authority_mismatch",
        ):
            tampered.validate()

    def test_indirect_proposition_uses_v2_without_context(self) -> None:
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text="Short answers work best for me.",
            operation_id="op-context-3",
            exchange_eligibility=True,
        )
        self.assertIsNotNone(prepared)
        assert prepared is not None
        prepared.request.validate()
        self.assertIsNone(prepared.gate_result.context_envelope_sha256)

    def test_high_recall_disposition_stays_outside_request_receipt(self) -> None:
        text = "My childhood summers were mostly spent near the lake."
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text=text,
            operation_id="op-high-recall-1",
            exchange_eligibility=True,
        )
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertEqual(
            prepared.gate_result.reason_codes,
            ("high_recall_owner_authored_candidate",),
        )
        receipt = prepared.content_free_receipt()
        self.assertNotIn("eligibility_disposition", receipt)
        self.assertEqual(len(receipt), 29)
        self.assertNotIn(text, repr(receipt))

    def test_v1_direct_path_remains_wire_compatible(self) -> None:
        text = "I have three sisters: Cindy, Lori, and Heidi."
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text=text,
            operation_id="op-message-v1",
        )
        assert prepared is not None
        payload = json.loads(prepared.request.selected_input_text)
        self.assertEqual(
            payload["contract_version"],
            "memory_v1_openai_selected_spans_v2",
        )

    def test_pure_information_still_prepares_no_request(self) -> None:
        prepared = adapter().prepare(
            owner_user_id=OWNER,
            source_text="Who won America's Next Top Model in 1964?",
            operation_id="op-zero-call",
            exchange_eligibility=True,
        )
        self.assertIsNone(prepared)


if __name__ == "__main__":
    unittest.main()
