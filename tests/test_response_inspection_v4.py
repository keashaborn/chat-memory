from __future__ import annotations

import json
import unittest

from rag_engine.openai_chat_request_v4 import OpenAIChatCompletionsAdapterV3
from rag_engine.response_finalization_v3 import finalize_trusted_response_v3
from rag_engine.response_inspection_v4 import build_response_inspection_v4
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR, ANSWER, NOW, new_plan
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response


class ResponseInspectionV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_reports_only_content_free_provenance_counts(self) -> None:
        message = "Did you access my LifeSwitch nutrition day for Monday?"
        plan = await new_plan(message, with_prior=True)
        response = OpenAIChatCompletionsAdapterV3(
            FakeClient(provider_response(content="Those figures came from LifeSwitch."))
        ).complete(plan)
        finalized = finalize_trusted_response_v3(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        trace = build_response_inspection_v4(
            trusted_plan=plan,
            provider_response=response,
            finalized=finalized,
            transcript_persistence="persisted",
        )
        self.assertEqual(trace.before_openai.prior_lifeswitch_response_count, 1)
        self.assertEqual(trace.before_openai.prior_lifeswitch_source_ref_count, 1)
        self.assertEqual(trace.after_openai.lifeswitch_provenance_receipt, "bound")
        exported = json.dumps(trace.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn(message, exported)
        self.assertNotIn(str(ACTOR), exported)
        self.assertNotIn("protein_g", exported)


if __name__ == "__main__":
    unittest.main()
