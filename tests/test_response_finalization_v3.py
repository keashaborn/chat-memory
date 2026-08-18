from __future__ import annotations

import unittest

from seebx.adapters.lifeswitch_openai_chat import OpenAIChatCompletionsAdapterV3
from rag_engine.response_finalization_v3 import finalize_trusted_response_v3
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ANSWER, NOW, new_plan
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response


class ResponseFinalizationV3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_current_lifeswitch_exposure_creates_paired_receipt(self) -> None:
        plan = await new_plan("What were my macros Monday?")
        response = OpenAIChatCompletionsAdapterV3(
            FakeClient(provider_response(content="Monday used your LifeSwitch log."))
        ).complete(plan)
        finalized = finalize_trusted_response_v3(
            trusted_plan=plan,
            provider_response=response,
            answer_id=ANSWER,
            created_at=NOW,
        )
        self.assertIsNotNone(finalized.lifeswitch_binding)
        self.assertIsNotNone(finalized.lifeswitch_provenance_receipt)
        self.assertEqual(
            finalized.lifeswitch_provenance_receipt.lifeswitch_binding_manifest_sha256,
            finalized.lifeswitch_binding.binding_manifest_sha256,
        )
        self.assertEqual(
            finalized.lifeswitch_provenance_receipt.attestation_sha256,
            finalized.attestation.attestation_sha256,
        )


if __name__ == "__main__":
    unittest.main()
