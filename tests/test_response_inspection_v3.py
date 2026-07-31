from __future__ import annotations

import datetime as dt
import json
import unittest
import uuid

from rag_engine.lifeswitch_answer_binding_v1 import (
    FinalAnswerLifeSwitchBindingV1,
)
from rag_engine.response_lifeswitch_integration_v1 import (
    TrustedLifeSwitchResponsePlanV1,
)
from rag_engine.response_inspection_v3 import build_response_inspection_v3
from tests.test_response_lifeswitch_integration_v1 import selected_context
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


class ResponseInspectionV3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_is_content_free_and_reports_lifeswitch_exposure(self) -> None:
        message = "Was I low on protein Monday?"
        base_plan = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="request-123",
                conversation=messages(message),
            )
        )
        prepared = selected_context(base_plan, message)
        assembly = TrustedLifeSwitchResponsePlanV1.create(
            base_response_plan=base_plan,
            lifeswitch_context=prepared,
        ).assembled_prompt
        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=assembly,
            authenticated_actor_user_id=ACTOR,
            answer_id=uuid.uuid4(),
            created_at=dt.datetime.now(dt.timezone.utc),
        )

        trace = build_response_inspection_v3(
            prepared=prepared,
            assembled=assembly,
            binding=binding,
        )

        exported = json.dumps(trace.model_dump(mode="json"), sort_keys=True)
        self.assertEqual(trace.contract_version, "response_inspection_v3")
        self.assertTrue(trace.before_openai.included)
        self.assertEqual(trace.before_openai.projections, ("nutrition_day",))
        self.assertEqual(trace.after_openai.answer_binding, "bound")
        self.assertNotIn(message, exported)
        self.assertNotIn(str(ACTOR), exported)
        self.assertNotIn("protein_g", exported)


if __name__ == "__main__":
    unittest.main()
