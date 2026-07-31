from pathlib import Path
import unittest

from scripts import memory_v1_contextual_evidence_intake_dispatcher_v3 as bridge
from scripts import memory_v1_v5_chat_capture as capture


ROOT = Path(__file__).resolve().parents[1]


class ContextualChatCaptureBridgeV1Test(unittest.TestCase):
    def test_new_capture_contract_is_contextual_and_source_bound(self) -> None:
        self.assertEqual(
            capture.CAPTURE_VERSION,
            "memory_v1_v5_chat_capture_20260730_v2_contextual",
        )
        source = (ROOT / "scripts/memory_v1_v5_chat_capture.py").read_text()
        for key in (
            '"source_id"',
            '"source_content_sha256"',
            '"source_char_start"',
            '"source_char_end"',
            '"primary_lane"',
            '"epistemic_role"',
            '"span_origin"',
        ):
            self.assertIn(key, source)

    def test_dispatcher_uses_v3_split_and_exact_capture_planner(self) -> None:
        self.assertEqual(
            bridge.CONTRACT_VERSION,
            "memory_v1_contextual_evidence_intake_dispatcher_v3",
        )
        self.assertEqual(
            bridge.SELECTOR_VERSION,
            "20260729_v4_contextual_resplit",
        )
        self.assertEqual(
            bridge.APPLY_CAPABILITY,
            "memory_v1_contextual_evidence_intake_apply_v3",
        )
        source = (
            ROOT
            / "scripts/memory_v1_contextual_evidence_intake_dispatcher_v3.py"
        ).read_text()
        self.assertIn("plan_owner_contextual_chat_capture_v1", source)
        self.assertIn("preflight_owner_contextual_split_v3", source)
        self.assertIn("apply_owner_contextual_split_v3", source)
        self.assertIn("finalize_owner_contextual_split_v3", source)

    def test_systemd_retires_legacy_raw_dispatcher(self) -> None:
        unit = (
            ROOT / "ops/systemd/memory-v1-evidence-intake-dispatcher.service"
        ).read_text()
        self.assertIn(
            "memory_v1_contextual_evidence_intake_dispatcher_v3.py",
            unit,
        )
        self.assertIn(
            "MEMORY_V1_CONTEXTUAL_INTAKE_APPLY="
            "memory_v1_contextual_evidence_intake_apply_v3",
            unit,
        )
        self.assertIn("20260729_v4_contextual_resplit", unit)
        self.assertNotIn(
            "/scripts/memory_v1_evidence_intake_dispatcher.py",
            unit,
        )


if __name__ == "__main__":
    unittest.main()
