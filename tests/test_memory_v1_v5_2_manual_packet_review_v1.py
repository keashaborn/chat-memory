from __future__ import annotations

import datetime as dt
import unittest

from scripts.memory_v1_v5_2_manual_packet_review_v1 import (
    ManualPacketReviewError,
    analyze_packet,
)


OBSERVED = dt.datetime(2026, 7, 30, 21, 39, 53, 840736, tzinfo=dt.timezone.utc)
RECORDED = dt.datetime(2026, 7, 31, 11, 24, 23, 478195, tzinfo=dt.timezone.utc)


def packet() -> dict:
    temporal = {
        "instant_range": {
            "lower": "2026-07-31T11:24:23.478195Z",
            "upper": None,
            "bounds": "[)",
        }
    }
    return {
        "entity_mentions": [
            {
                "entity_ref": "e00",
                "entity_type": "person",
                "name_text": "Jerry",
                "mention_kind": "named",
                "relationship_role": "family:father",
            },
            {
                "entity_ref": "e01",
                "entity_type": "place",
                "name_text": "assisted living",
                "mention_kind": "named",
                "relationship_role": "residence:reported",
            },
        ],
        "observations": [
            {
                "observation_ref": "o01",
                "predicate": "residence.lives_at",
                "subject_entity_ref": "e00",
                "object": {"kind": "entity", "entity_ref": "e01"},
                "temporal": temporal,
            },
            {
                "observation_ref": "o02",
                "predicate": "health.user_reported_observation",
                "subject_entity_ref": "e00",
                "object": {"kind": "literal", "value": "severe dementia"},
                "modality": "reported_observation",
                "sensitivity": "high",
                "surface_policy": "explicit_recall_only",
            },
            {
                "observation_ref": "o03",
                "predicate": "health.user_reported_observation",
                "subject_entity_ref": "e00",
                "object": {
                    "kind": "literal",
                    "value": "short-term memory lasts about three seconds",
                    "approximate": False,
                },
                "modality": "reported_observation",
                "sensitivity": "high",
                "surface_policy": "explicit_recall_only",
            },
        ],
    }


class ManualPacketReviewV1Tests(unittest.TestCase):
    def test_review_requires_corrected_split(self) -> None:
        result = analyze_packet(
            packet(),
            evidence_observed_at=OBSERVED,
            evidence_recorded_at=RECORDED,
            prior_user_text="Do you know my dad's name in a certain current situation?",
            prior_duration_literal={
                "value": "Memory only lasts about 30 seconds or so."
            },
        )
        self.assertEqual(result["disposition"], "corrected_split_required")
        self.assertEqual(result["corrected_duration_approximate"], True)
        self.assertEqual(result["corrected_temporal_anchor"], OBSERVED.isoformat().replace("+00:00", "Z"))
        self.assertEqual(
            [item["decision"] for item in result["entity_decisions"]],
            ["approve_named_role_resolution", "hold_generic_place_identity"],
        )

    def test_review_rejects_unresolved_pronoun_context(self) -> None:
        with self.assertRaisesRegex(ManualPacketReviewError, "coreference"):
            analyze_packet(
                packet(),
                evidence_observed_at=OBSERVED,
                evidence_recorded_at=RECORDED,
                prior_user_text="Tell me something else.",
                prior_duration_literal={
                    "value": "Memory only lasts about 30 seconds or so."
                },
            )

    def test_review_rejects_missing_anchor_defect(self) -> None:
        value = packet()
        value["observations"][0]["temporal"]["instant_range"]["lower"] = (
            "2026-07-30T21:39:53.840736Z"
        )
        with self.assertRaisesRegex(ManualPacketReviewError, "anchor defect"):
            analyze_packet(
                value,
                evidence_observed_at=OBSERVED,
                evidence_recorded_at=RECORDED,
                prior_user_text="Do you know my dad's name?",
                prior_duration_literal={
                    "value": "Memory only lasts about 30 seconds or so."
                },
            )


if __name__ == "__main__":
    unittest.main()
