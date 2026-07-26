#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts import memory_v1_v5_2_evidence_context_stance_claim_stage as stance
from scripts.memory_v1_v5_2_projection_dispatch import render_claim_text


class EvidenceContextStanceClaimStageTest(unittest.TestCase):
    def test_exact_one_observation_boundary(self) -> None:
        self.assertEqual(list(stance.TARGETS), [stance.OBSERVATION_ID])
        target = stance.TARGETS[stance.OBSERVATION_ID]
        self.assertEqual(target["predicate"], "stance.reported")
        self.assertEqual(target["state_relation"], "not_applicable")

    def test_renderer_preserves_attribution_and_first_person_position(self) -> None:
        source = {
            "predicate": "stance.reported",
            "subject_entity_type": "self",
            "subject_canonical_name": "Self",
            "object_kind": "literal",
            "object_literal": stance.EXPECTED_LITERAL,
            "polarity": "affirmed",
            "modality": "reported_belief",
        }
        self.assertEqual(
            render_claim_text(source),
            stance.TARGETS[stance.OBSERVATION_ID]["canonical_text"],
        )

    def test_stage_budget_is_six_candidate_rows(self) -> None:
        self.assertEqual(stance.stage.EXPECTED_NEW_ROWS, 6)
        self.assertEqual(
            stance.stage.EXPECTED_TABLE_ROWS,
            {
                "observation_entailment_v5": 1,
                "relational_operation_request": 1,
                "projection_plan": 1,
                "projection_plan_item": 1,
                "projection_claim_payload": 1,
                "projection_plan_observation": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
