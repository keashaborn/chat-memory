from __future__ import annotations

import unittest

from rag_engine.response_composition_root_v0_2 import (
    InactiveResponseCompositionRootV0_2,
)
from rag_engine.response_inspection_v1 import build_response_inspection_v1
from tests.test_response_composition_root_v0_2 import (
    ANSWER,
    CORRELATION,
    CombinedOpenAIClient,
    SnapshotConn,
    classifier_output,
    command,
)


class ResponseInspectionV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_describes_exact_execution_without_private_content(self) -> None:
        client = CombinedOpenAIClient(classifier_output(fm_explicit=True))
        root = InactiveResponseCompositionRootV0_2(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )
        user_text = "Explain Fractal Monism in plain language."
        execution = await root.execute_detailed(SnapshotConn(), command(user_text))

        inspection = build_response_inspection_v1(
            trusted_plan=execution.trusted_plan,
            provider_response=execution.provider_response,
            finalized=execution.finalized,
            transcript_persistence="persisted",
        )

        self.assertEqual(inspection.contract_version, "response_inspection_v1")
        self.assertEqual(inspection.before_openai.response_mode, "FM_EXPLICIT")
        self.assertEqual(inspection.before_openai.fm_level, "EXPLICIT")
        self.assertGreater(inspection.before_openai.fm_record_count, 0)
        self.assertEqual(inspection.openai.response_id, "chatcmpl-test-001")
        self.assertEqual(inspection.after_openai.answer_id, ANSWER)
        self.assertEqual(
            inspection.after_openai.transcript_persistence, "persisted"
        )

        wire = inspection.model_dump_json()
        for forbidden in (
            user_text,
            "A bounded test response.",
            "system_prompt",
            "assistant_text",
            "memory_used",
            "content_sha256",
            "request_sha256",
        ):
            self.assertNotIn(forbidden, wire)

    async def test_stateless_trace_reports_skipped_persistence(self) -> None:
        root = InactiveResponseCompositionRootV0_2(
            openai_client=CombinedOpenAIClient(),
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )
        execution = await root.execute_detailed(SnapshotConn(), command("Hello"))
        inspection = build_response_inspection_v1(
            trusted_plan=execution.trusted_plan,
            provider_response=execution.provider_response,
            finalized=execution.finalized,
            transcript_persistence="skipped",
        )

        self.assertFalse(inspection.before_openai.memory_included)
        self.assertEqual(inspection.before_openai.memory_record_count, 0)
        self.assertEqual(
            inspection.after_openai.transcript_persistence, "skipped"
        )
        self.assertEqual(inspection.after_openai.memory_binding, "none")


if __name__ == "__main__":
    unittest.main()
