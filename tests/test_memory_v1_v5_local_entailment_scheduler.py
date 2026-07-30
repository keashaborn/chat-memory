import argparse
import unittest
import uuid

from scripts.memory_v1_v5_local_entailment_scheduler import (
    LocalEntailmentError,
    bounded_targets,
    validate_arguments,
)
from scripts.memory_v1_v5_local_inference_canary import (
    PINNED_MODEL_ALIAS,
    PINNED_MODEL_FILE_SHA256,
    PINNED_RUNTIME_REVISION,
)


def _args(max_records: int) -> argparse.Namespace:
    return argparse.Namespace(
        timeout_seconds=120.0,
        max_output_tokens=128,
        max_records=max_records,
        model=PINNED_MODEL_ALIAS,
        model_file_sha256=PINNED_MODEL_FILE_SHA256,
        runtime_revision=PINNED_RUNTIME_REVISION,
        apply=False,
    )


def _row(observation_id: str) -> dict[str, str]:
    return {"observation_id": observation_id}


class LocalEntailmentSchedulerTests(unittest.TestCase):
    def test_record_limit_is_bounded(self):
        validate_arguments(_args(1))
        validate_arguments(_args(20))
        with self.assertRaises(LocalEntailmentError):
            validate_arguments(_args(0))
        with self.assertRaises(LocalEntailmentError):
            validate_arguments(_args(21))

    def test_selection_is_round_robin_across_owners(self):
        first = uuid.uuid4()
        second = uuid.uuid4()
        first_rows = [_row(str(uuid.uuid4())) for _ in range(4)]
        second_rows = [_row(str(uuid.uuid4())) for _ in range(2)]

        selected = bounded_targets(
            [(first, first_rows), (second, second_rows)],
            5,
        )

        self.assertEqual(
            [owner for owner, _ in selected],
            [first, second, first, second, first],
        )

    def test_duplicate_observation_for_an_owner_is_rejected(self):
        owner = uuid.uuid4()
        observation_id = str(uuid.uuid4())

        with self.assertRaisesRegex(
            LocalEntailmentError,
            "planner returned a duplicate",
        ):
            bounded_targets(
                [(owner, [_row(observation_id), _row(observation_id)])],
                10,
            )


if __name__ == "__main__":
    unittest.main()
