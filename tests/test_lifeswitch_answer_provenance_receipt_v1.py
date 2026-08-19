from __future__ import annotations

import datetime as dt
import hashlib
import unittest
import uuid

from pydantic import ValidationError

from seebx.capabilities.conversation.lifeswitch_answer_binding import FinalAnswerLifeSwitchBindingV1
from seebx.capabilities.plans.data_plan import create_lifeswitch_data_plan_v1
from seebx.capabilities.plans.domain_context import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
    render_lifeswitch_context_v1,
)
from seebx.capabilities.conversation.lifeswitch_provenance import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
    LifeSwitchProvenanceSourceRefV1,
)
from seebx.adapters.lifeswitch_prior_provenance_postgres import (
    PriorLifeSwitchPreparedContextV1,
)
from seebx.capabilities.conversation.prior_lifeswitch_provenance import (
    PriorLifeSwitchProvenanceEnvelopeV1,
    PriorLifeSwitchResponseV1,
)
from seebx.capabilities.conversation.lifeswitch_plan import TrustedLifeSwitchResponsePlanV2
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


NOW = dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 7, 29)
ANSWER = uuid.UUID("90000000-0000-4000-8000-000000000031")


def selected_context(base_plan, message: str):
    plan = create_lifeswitch_data_plan_v1(message, today=TODAY)
    trusted = TrustedLifeSwitchContextRequestV1.create(
        request_id=base_plan.policy_input.request_id,
        authenticated_actor_user_id=ACTOR,
        owner_user_id=ACTOR,
        thread_id=base_plan.thread_id,
        conversation_snapshot_sha256=base_plan.conversation_snapshot_sha256,
        owner_timezone="America/Chicago",
        query=message,
        data_plan=plan,
    )
    section = LifeSwitchContextSectionV1.create(
        projection="nutrition_day",
        status="AVAILABLE",
        window=plan.window,
        record_count=1,
        source_relations=("lifeswitch_nutrition.nutrition_day",),
        payload={"daily": [{"date": "2026-07-27", "protein_g": 142.0}]},
    )
    envelope = create_lifeswitch_context_envelope_v1(
        request=trusted,
        plan_source="agentic_active",
        as_of_local_date=TODAY,
        sections=(section,),
        generated_at=NOW,
    )
    from seebx.capabilities.conversation.lifeswitch_context import (
        LifeSwitchPreparedContextV1,
    )

    return LifeSwitchPreparedContextV1.create(
        status="SELECTED",
        timezone_source="account_timezone",
        database_accessed=True,
        data_plan=plan,
        envelope=envelope,
        rendered=render_lifeswitch_context_v1(envelope),
    )


def sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def off_prior() -> PriorLifeSwitchPreparedContextV1:
    return PriorLifeSwitchPreparedContextV1.create(status="OFF", database_accessed=False)


def prior_envelope(base, query: str) -> PriorLifeSwitchPreparedContextV1:
    ref = LifeSwitchProvenanceSourceRefV1.create(
        ordinal=0,
        projection="nutrition_range",
        status="AVAILABLE",
        record_count=14,
        window_start_date=dt.date(2026, 7, 18),
        window_end_date=dt.date(2026, 7, 31),
        payload_sha256="a" * 64,
    )
    response = PriorLifeSwitchResponseV1(
        relative_ordinal=0,
        answer_id=ANSWER,
        provenance_status="exact_receipt",
        source_refs=(ref,),
        answer_sha256="b" * 64,
        attestation_sha256="c" * 64,
    )
    envelope = PriorLifeSwitchProvenanceEnvelopeV1.create(
        authenticated_actor_user_id=ACTOR,
        thread_id=base.thread_id,
        conversation_snapshot_sha256=base.conversation_snapshot_sha256,
        current_request_id=base.policy_input.request_id,
        current_query=query,
        responses=(response,),
    )
    return PriorLifeSwitchPreparedContextV1.create(
        status="SELECTED",
        database_accessed=True,
        envelope=envelope,
    )


async def new_plan(message: str, *, with_prior: bool = False):
    base = await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(message),
        )
    )
    return TrustedLifeSwitchResponsePlanV2.create(
        base_response_plan=base,
        lifeswitch_context=selected_context(base, message),
        prior_lifeswitch_context=(prior_envelope(base, message) if with_prior else off_prior()),
    )


class LifeSwitchAnswerProvenanceReceiptV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_is_content_free_and_exactly_bound(self) -> None:
        plan = await new_plan("Where did you get my earlier nutrition numbers?")
        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=plan.assembled_prompt,
            authenticated_actor_user_id=ACTOR,
            answer_id=ANSWER,
            created_at=NOW,
        )
        assert binding is not None
        receipt = FinalAnswerLifeSwitchProvenanceReceiptV1.create(
            prepared_context=plan.lifeswitch_context,
            binding=binding,
            assistant_text_sha256="d" * 64,
            attestation_sha256="e" * 64,
        )
        exported = receipt.model_dump_json()
        self.assertEqual(receipt.source_refs[0].projection, "nutrition_day")
        self.assertNotIn("protein_g", exported)
        self.assertNotIn("142", exported)

    def test_source_ref_rejects_false_exact_window(self) -> None:
        with self.assertRaises(ValidationError):
            LifeSwitchProvenanceSourceRefV1(
                ordinal=0,
                projection="nutrition_range",
                status="AVAILABLE",
                record_count=1,
                window_start_date=None,
                window_end_date=None,
                window_exact=True,
                source_categories=("plan", "nutrition"),
                payload_sha256="a" * 64,
                source_ref_manifest_sha256="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
