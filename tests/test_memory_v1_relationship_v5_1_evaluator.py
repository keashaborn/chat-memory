from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_relationship_v5_1_local_synthetic_eval import (
    case_source_class,
    observation_direction,
    temporal_expectation_passes,
)


class RelationshipV51EvaluatorTest(unittest.TestCase):
    def test_direction_is_derived_from_entity_types(self) -> None:
        entities = {
            "e01": {"entity_ref": "e01", "entity_type": "self"},
            "e02": {"entity_ref": "e02", "entity_type": "person"},
        }
        self.assertEqual(
            observation_direction(
                {
                    "subject_entity_ref": "e01",
                    "object": {"kind": "entity", "entity_ref": "e02"},
                },
                entities,
            ),
            "self_to_named",
        )
        self.assertEqual(
            observation_direction(
                {
                    "subject_entity_ref": "e02",
                    "object": {"kind": "entity", "entity_ref": "e01"},
                },
                entities,
            ),
            "named_to_self",
        )

    def test_temporal_shape_gate_is_explicit(self) -> None:
        self.assertTrue(
            temporal_expectation_passes(
                {"temporal": {"semantic": "state_validity", "shape": "open_interval"}},
                "dynamic_open",
            )
        )
        self.assertTrue(
            temporal_expectation_passes(
                {"temporal": {"semantic": "state_validity", "shape": "bounded_interval"}},
                "closed_or_bounded",
            )
        )
        self.assertFalse(
            temporal_expectation_passes(
                {"temporal": {"semantic": "state_validity", "shape": "open_interval"}},
                "closed_or_bounded",
            )
        )

    def test_source_class_may_be_bound_by_case(self) -> None:
        self.assertEqual(
            case_source_class(
                {
                    "case_id": "adversarial-001",
                    "source_class": "technical_discussion",
                }
            ),
            "technical_discussion",
        )


if __name__ == "__main__":
    unittest.main()

