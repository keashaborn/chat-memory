from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "memory_v1_v5_2_manual_packet_review_v2.py"
SPEC = importlib.util.spec_from_file_location("manual_review_v2", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManualPacketReviewV2Test(unittest.TestCase):
    def packet(self) -> dict:
        temporal = {
            "instant_range": {"lower": "2026-07-30T21:39:53.840736Z"},
            "certainty": "bounded",
        }
        return {
            "entity_mentions": [{
                "entity_ref": "e00", "entity_type": "person", "name_text": "Jerry",
                "relationship_role": "family:father",
            }],
            "observations": [
                {
                    "observation_ref": "o02", "predicate": "health.user_reported_observation",
                    "subject_entity_ref": "e00", "sensitivity": "high",
                    "surface_policy": "explicit_recall_only", "modality": "reported_observation",
                    "polarity": "affirmed", "temporal": temporal,
                    "object": {"value": "severe dementia"}, "reason_codes": [],
                },
                {
                    "observation_ref": "o03", "predicate": "health.user_reported_observation",
                    "subject_entity_ref": "e00", "sensitivity": "high",
                    "surface_policy": "explicit_recall_only", "modality": "reported_observation",
                    "polarity": "affirmed", "temporal": temporal,
                    "object": {
                        "value": "short-term memory lasts about three seconds",
                        "approximate": False,
                    },
                    "reason_codes": ["approximate_reported_duration"],
                },
            ],
            "deferrals": [
                {"reason_code": "sensitive_manual_review"},
                {
                    "reason_code": "unregistered_predicate",
                    "memory_shape": "supportive_context", "sensitivity": "medium",
                    "review_required": True,
                },
                {"reason_code": "insufficient_evidence"},
            ],
        }

    def test_approves_restricted_split_and_keeps_care_setting_deferred(self) -> None:
        result = MODULE.analyze_packet(
            self.packet(),
            evidence_observed_at=dt.datetime(
                2026, 7, 30, 21, 39, 53, 840736, tzinfo=dt.timezone.utc
            ),
            prior_user_text="Do you know my dad's name in a certain current situation?",
            prior_duration_literal={"value": "Memory only lasts about 30 seconds or so."},
        )
        self.assertEqual(result["disposition"], "approve_restricted_partial_stage")
        self.assertEqual(result["approved_observation_refs"], ["o02", "o03"])
        self.assertTrue(result["care_setting_deferred"])

    def test_rejects_unbound_father_coreference(self) -> None:
        with self.assertRaises(MODULE.ManualPacketReviewV2Error):
            MODULE.analyze_packet(
                self.packet(),
                evidence_observed_at=dt.datetime(
                    2026, 7, 30, 21, 39, 53, 840736, tzinfo=dt.timezone.utc
                ),
                prior_user_text="Tell me about Jerry.",
                prior_duration_literal={"value": "Memory only lasts about 30 seconds or so."},
            )

    def test_rejects_residence_place_hallucination(self) -> None:
        packet = self.packet()
        packet["entity_mentions"].append({
            "entity_ref": "e01", "entity_type": "place", "name_text": "assisted living"
        })
        with self.assertRaises(MODULE.ManualPacketReviewV2Error):
            MODULE.analyze_packet(
                packet,
                evidence_observed_at=dt.datetime(
                    2026, 7, 30, 21, 39, 53, 840736, tzinfo=dt.timezone.utc
                ),
                prior_user_text="Do you know my dad's name?",
                prior_duration_literal={"value": "Memory only lasts about 30 seconds or so."},
            )


if __name__ == "__main__":
    unittest.main()
