from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_v5_2_reviewed_observation_stage import (
    ReviewedObservationStageError,
    bounded_targets,
    operation_ids,
    verify_results,
)


OWNER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")


def target(number: int, source_kind: str = "atom_apply") -> dict[str, object]:
    return {
        "source_kind": source_kind,
        "route_event_id": uuid.UUID(f"00000000-0000-4000-8000-{number:012d}"),
        "atom_apply_id": (
            uuid.UUID(f"10000000-0000-4000-8000-{number:012d}")
            if source_kind == "atom_apply"
            else None
        ),
        "batch_id": uuid.UUID(f"20000000-0000-4000-8000-{number:012d}"),
        "stage_manifest_sha256": "1" * 64,
        "resolution_state_sha256": "2" * 64,
        "observation_state_sha256": "3" * 64,
        "observation_count": 1,
    }


class ReviewedObservationStageTest(unittest.TestCase):
    def test_owner_fair_selection(self) -> None:
        selected = bounded_targets(
            [
                (OWNER_A, [target(1), target(2), target(3)]),
                (OWNER_B, [target(4), target(5)]),
            ],
            4,
        )
        self.assertEqual(
            [(owner, row["route_event_id"]) for owner, row in selected],
            [
                (OWNER_A, target(1)["route_event_id"]),
                (OWNER_B, target(4)["route_event_id"]),
                (OWNER_A, target(2)["route_event_id"]),
                (OWNER_B, target(5)["route_event_id"]),
            ],
        )

    def test_ids_are_stable_and_source_sensitive(self) -> None:
        first = operation_ids(OWNER_A, target(1))
        self.assertEqual(first, operation_ids(OWNER_A, target(1)))
        self.assertNotEqual(first, operation_ids(OWNER_B, target(1)))
        self.assertNotEqual(first, operation_ids(OWNER_A, target(1, "reviewed_route")))

    def test_duplicate_route_is_rejected(self) -> None:
        with self.assertRaises(ReviewedObservationStageError):
            bounded_targets([(OWNER_A, [target(1), target(1)])], 2)

    def test_apply_and_replay_contract(self) -> None:
        value = target(1)
        applied = {
            "admission_id": uuid.uuid4(),
            "route_event_id": value["route_event_id"],
            "batch_id": value["batch_id"],
            "decision": "v5_2_atom_reviewed_stage",
            "outcome": "applied",
            "rows_written": 2,
        }
        replay = {
            **applied,
            "outcome": "replayed",
            "rows_written": 0,
        }
        verify_results([(OWNER_A, value)], [applied], [replay])

    def test_unknown_source_kind_is_rejected(self) -> None:
        value = target(1)
        value["source_kind"] = "unknown"
        with self.assertRaises(ReviewedObservationStageError):
            operation_ids(OWNER_A, value)


if __name__ == "__main__":
    unittest.main()
