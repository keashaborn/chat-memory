from __future__ import annotations

import datetime as dt
import unittest

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
    render_lifeswitch_context_v1,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from rag_engine.openai_chat_request_v3 import OpenAIChatRequestV3
from rag_engine.response_lifeswitch_integration_v1 import (
    TrustedLifeSwitchResponsePlanV1,
)
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


TODAY = dt.date(2026, 7, 29)


def selected_context(base_plan, message: str) -> LifeSwitchPreparedContextV1:
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
        generated_at=dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc),
    )
    return LifeSwitchPreparedContextV1.create(
        status="SELECTED",
        timezone_source="account_timezone",
        database_accessed=True,
        data_plan=plan,
        envelope=envelope,
        rendered=render_lifeswitch_context_v1(envelope),
    )


class ResponseLifeSwitchIntegrationV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_plan_preserves_base_and_adds_lifeswitch(self) -> None:
        message = "Was I low on protein Monday?"
        base = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="request-123",
                conversation=messages(message),
            )
        )

        result = TrustedLifeSwitchResponsePlanV1.create(
            base_response_plan=base,
            lifeswitch_context=selected_context(base, message),
        )

        self.assertEqual(result.base_response_plan_sha256, base.plan_sha256)
        self.assertEqual(
            tuple(block.block_id for block in result.assembled_prompt.context_blocks),
            ("lifeswitch_domain_context_v1",),
        )
        self.assertEqual(
            result.assembled_prompt.source_request.base_assembly.manifest.assembly_sha256,
            base.assembled_prompt.manifest.assembly_sha256,
        )

    async def test_openai_request_uses_lifeswitch_as_named_reference_data(self) -> None:
        message = "Was I low on protein Monday?"
        base = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="request-123",
                conversation=messages(message),
            )
        )
        plan = TrustedLifeSwitchResponsePlanV1.create(
            base_response_plan=base,
            lifeswitch_context=selected_context(base, message),
        )

        request = OpenAIChatRequestV3.create(source_plan=plan)

        named = tuple(item.name for item in request.messages if item.name is not None)
        self.assertEqual(named, ("lifeswitch_domain_context_v1",))
        self.assertFalse(request.store)
        self.assertEqual(request.messages[-1].content, message)
        self.assertNotIn(message, request.messages[-2].content)


if __name__ == "__main__":
    unittest.main()
