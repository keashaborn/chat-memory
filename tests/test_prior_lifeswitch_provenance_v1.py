from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID

from rag_engine.lifeswitch_answer_provenance_receipt_v1 import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
    LifeSwitchProvenanceSourceRefV1,
    _sha256 as receipt_sha256,
)
from rag_engine.openai_chat_request_v4 import OpenAIChatCompletionsAdapterV3
from rag_engine.prior_lifeswitch_provenance_v1 import (
    prior_lifeswitch_provenance_requested_v1,
    select_prior_lifeswitch_provenance_v1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    ATTESTED_ASSISTANT_SOURCE,
    ConversationSnapshotOutcome,
    create_current_only_conversation_snapshot_v1,
    _snapshot,
)
from rag_engine.response_finalization_v3 import finalize_trusted_response_v3
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import (
    ACTOR,
    ANSWER,
    NOW,
    new_plan,
)
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response
from tests.test_response_orchestration_v0_2 import THREAD


CURRENT_LOG = UUID("90000000-0000-4000-8000-000000000099")
QUESTION = "Where did you get those earlier protein numbers?"


class FetchFailConn:
    async def fetch(self, *_args, **_kwargs):
        raise AssertionError("database must not be read for an unbound snapshot")


class RowsConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fetch_calls = 0

    async def fetch(self, *_args, **_kwargs):
        self.fetch_calls += 1
        return self.rows


def bound_snapshot(message: str = QUESTION):
    return _snapshot(
        actor=ACTOR,
        thread=THREAD,
        request_id="prior-provenance-request",
        outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
        current_log_id=CURRENT_LOG,
        cutoff=NOW + timedelta(minutes=1),
        messages=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="What were my macros Monday?",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="Monday used your LifeSwitch nutrition log.",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
        candidate_count=2,
        dropped_count=0,
        message_limit_truncated=False,
    )


def receipt_with(
    receipt: FinalAnswerLifeSwitchProvenanceReceiptV1,
    **updates: Any,
) -> FinalAnswerLifeSwitchProvenanceReceiptV1:
    payload = receipt.model_dump(mode="python", exclude={"receipt_manifest_sha256"})
    payload.update(updates)
    return FinalAnswerLifeSwitchProvenanceReceiptV1(
        **payload,
        receipt_manifest_sha256=receipt_sha256(payload),
    )


def put_receipt(row: dict[str, Any], receipt) -> None:
    row.update(
        {
            "receipt_actor_user_id": receipt.authenticated_actor_user_id,
            "receipt_request_id_sha256": receipt.request_id_sha256,
            "receipt_snapshot_sha256": receipt.conversation_snapshot_sha256,
            "prepared_context_manifest_sha256": (
                receipt.lifeswitch_prepared_context_manifest_sha256
            ),
            "data_plan_sha256": receipt.data_plan_sha256,
            "receipt_binding_manifest_sha256": (
                receipt.lifeswitch_binding_manifest_sha256
            ),
            "receipt_source_assembly_sha256": receipt.source_assembly_sha256,
            "receipt_envelope_sha256": receipt.envelope_sha256,
            "receipt_assistant_text_sha256": receipt.assistant_text_sha256,
            "receipt_attestation_sha256": receipt.attestation_sha256,
            "receipt_answer_model_exposed": receipt.answer_model_exposed,
            "receipt_source_refs": [
                item.model_dump(mode="json") for item in receipt.source_refs
            ],
            "receipt_created_at": receipt.created_at,
            "receipt_manifest_sha256": receipt.receipt_manifest_sha256,
        }
    )


async def exact_row() -> tuple[dict[str, Any], Any, Any, Any]:
    plan = await new_plan("What were my macros Monday?")
    response = OpenAIChatCompletionsAdapterV3(
        FakeClient(provider_response(content="Monday used your LifeSwitch nutrition log."))
    ).complete(plan)
    finalized = finalize_trusted_response_v3(
        trusted_plan=plan,
        provider_response=response,
        answer_id=ANSWER,
        created_at=NOW,
    )
    binding = finalized.lifeswitch_binding
    receipt = finalized.lifeswitch_provenance_receipt
    attestation = finalized.attestation
    assert binding is not None and receipt is not None
    row: dict[str, Any] = {
        "context_id": THREAD,
        "owner_user_id": ACTOR,
        "thread_id": THREAD,
        "answer_id": ANSWER,
        "binding_actor_user_id": binding.authenticated_actor_user_id,
        "binding_request_id_sha256": binding.request_id_sha256,
        "binding_snapshot_sha256": binding.conversation_snapshot_sha256,
        "binding_source_assembly_sha256": binding.source_assembly_sha256,
        "binding_envelope_sha256": binding.envelope_sha256,
        "binding_rendered_content_sha256": binding.rendered_content_sha256,
        "binding_answer_model_exposed": binding.answer_model_exposed,
        "binding_record_count": binding.record_count,
        "binding_rendered_tokens": binding.rendered_tokens,
        "binding_record_refs": [
            item.model_dump(mode="json") for item in binding.record_refs
        ],
        "binding_manifest_sha256": binding.binding_manifest_sha256,
        "created_at": binding.created_at,
        "attestation_answer_id": attestation.answer_id,
        "attestation_request_id_sha256": attestation.request_id_sha256,
        "attestation_snapshot_sha256": attestation.conversation_snapshot_sha256,
        "attestation_assistant_text_sha256": attestation.assistant_text_sha256,
        "attestation_sha256": attestation.attestation_sha256,
        "attestation_created_at": attestation.created_at,
        "chat_log_id": ANSWER,
        "chat_source": ATTESTED_ASSISTANT_SOURCE,
    }
    put_receipt(row, receipt)
    return row, binding, receipt, attestation


def remove_receipt(row: dict[str, Any]) -> None:
    for key in tuple(row):
        if key.startswith("receipt_") or key in {
            "prepared_context_manifest_sha256",
            "data_plan_sha256",
        }:
            row[key] = None


class PriorLifeSwitchProvenanceV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_trigger_accepts_narrow_anaphoric_prior_source_followups(self) -> None:
        for message in (
            "Where did those numbers come from?",
            "And where'd you get all the information from?",
            "How do you know that?",
            "What was that based on?",
            "Which records supported that answer?",
        ):
            with self.subTest(message=message):
                self.assertTrue(prior_lifeswitch_provenance_requested_v1(message))

    def test_trigger_preserves_personal_domain_and_rejects_unrelated_turns(self) -> None:
        self.assertTrue(prior_lifeswitch_provenance_requested_v1(QUESTION))
        self.assertTrue(
            prior_lifeswitch_provenance_requested_v1(
                "Did you access my LifeSwitch training records?"
            )
        )
        self.assertFalse(prior_lifeswitch_provenance_requested_v1("What are my macros?"))
        self.assertFalse(
            prior_lifeswitch_provenance_requested_v1(
                "Where did that news come from?"
            )
        )
        for message in (
            "Where are you going?",
            "How are you?",
            "What is that based in?",
            "Which records should I keep?",
            "Tell me about those numbers.",
        ):
            with self.subTest(message=message):
                self.assertFalse(prior_lifeswitch_provenance_requested_v1(message))

    async def test_unbound_snapshot_reads_nothing(self) -> None:
        result = await select_prior_lifeswitch_provenance_v1(
            FetchFailConn(),
            context_id=THREAD,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=ACTOR,
                thread_id=THREAD,
                current_request_id="prior-request",
                current_message=QUESTION,
            ),
        )
        self.assertIsNone(result)

    async def test_exact_receipt_and_historical_fallback_are_distinct(self) -> None:
        row, _binding, _receipt, _attestation = await exact_row()
        exact = await select_prior_lifeswitch_provenance_v1(
            RowsConn([row]),
            context_id=THREAD,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=bound_snapshot(),
        )
        assert exact is not None
        self.assertEqual(exact.responses[0].provenance_status, "exact_receipt")
        historical_row = dict(row)
        remove_receipt(historical_row)
        historical = await select_prior_lifeswitch_provenance_v1(
            RowsConn([historical_row]),
            context_id=THREAD,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=bound_snapshot(),
        )
        assert historical is not None
        self.assertEqual(
            historical.responses[0].provenance_status,
            "historical_binding_only",
        )
        self.assertFalse(historical.responses[0].source_refs[0].window_exact)

    async def test_cross_linked_receipt_and_attestation_are_rejected(self) -> None:
        row, _binding, receipt, _attestation = await exact_row()
        alternate_ref = LifeSwitchProvenanceSourceRefV1.create(
            ordinal=0,
            projection="training_range",
            status=receipt.source_refs[0].status,
            record_count=receipt.source_refs[0].record_count,
            window_start_date=receipt.source_refs[0].window_start_date,
            window_end_date=receipt.source_refs[0].window_end_date,
            payload_sha256=receipt.source_refs[0].payload_sha256,
        )
        cases: list[dict[str, Any]] = []
        for update in (
            {"request_id_sha256": "1" * 64},
            {"conversation_snapshot_sha256": "2" * 64},
            {"created_at": NOW + timedelta(seconds=1)},
            {"source_refs": (alternate_ref,)},
        ):
            changed = dict(row)
            put_receipt(changed, receipt_with(receipt, **update))
            cases.append(changed)
        for key, value in (
            ("attestation_request_id_sha256", "3" * 64),
            ("attestation_snapshot_sha256", "4" * 64),
            ("attestation_created_at", NOW + timedelta(seconds=1)),
            ("receipt_answer_model_exposed", False),
        ):
            changed = dict(row)
            changed[key] = value
            cases.append(changed)
        malformed = dict(row)
        malformed["receipt_source_refs"] = "not-json"
        cases.append(malformed)
        for index, changed in enumerate(cases):
            with self.subTest(case=index):
                result = await select_prior_lifeswitch_provenance_v1(
                    RowsConn([changed]),
                    context_id=THREAD,
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=bound_snapshot(),
                )
                self.assertIsNone(result)

    async def test_cutoff_owner_thread_source_and_attestation_are_strict(self) -> None:
        row, _binding, _receipt, _attestation = await exact_row()
        cases = []
        for key, value in (
            ("owner_user_id", UUID("22222222-2222-4222-8222-222222222222")),
            ("thread_id", UUID("33333333-3333-4333-8333-333333333333")),
            ("chat_source", "backend/other:assistant:v1"),
            ("attestation_answer_id", CURRENT_LOG),
            ("chat_log_id", CURRENT_LOG),
            ("created_at", NOW + timedelta(minutes=1)),
        ):
            changed = dict(row)
            changed[key] = value
            cases.append(changed)
        for index, changed in enumerate(cases):
            with self.subTest(case=index):
                result = await select_prior_lifeswitch_provenance_v1(
                    RowsConn([changed]),
                    context_id=THREAD,
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=bound_snapshot(),
                )
                self.assertIsNone(result)

    async def test_prompt_provenance_remains_content_free(self) -> None:
        row, _binding, _receipt, _attestation = await exact_row()
        result = await select_prior_lifeswitch_provenance_v1(
            RowsConn([row]),
            context_id=THREAD,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=bound_snapshot(),
        )
        assert result is not None
        self.assertNotIn("Monday used", result.content)
        self.assertNotIn("protein_g", result.content)
        self.assertNotIn(str(ACTOR), result.content)
        self.assertIn("nutrition", result.content)


if __name__ == "__main__":
    unittest.main()
