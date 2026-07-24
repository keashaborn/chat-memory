from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from memory_v1_projection_v5_contract_test import sha256  # noqa: E402
from memory_v1_v5_claim_projection_controlled_project import (  # noqa: E402
    ControlledProjectionError,
    OWNER,
    QUERY_BY_PREDICATE,
    load_admission,
    load_apply,
)


def apply_result(item_count: int) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": "memory_v1_claim_projection_apply_batch_result_v1",
        "mode": "apply",
        "owner_user_id": OWNER,
        "insert_rows": 12 * item_count,
        "mutated_rows": 13 * item_count,
        "outcomes": [{"claim_id": str(index)} for index in range(item_count)],
    }
    value["result_sha256"] = sha256(value)
    return value


class ControlledProjectionBoundaryTest(unittest.TestCase):
    def write_result(self, directory: str, value: dict[str, object]) -> Path:
        path = Path(directory) / "apply.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_accepts_one_through_four_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for item_count in range(1, 5):
                value = apply_result(item_count)
                loaded = load_apply(self.write_result(directory, value))
                self.assertEqual(len(loaded["outcomes"]), item_count)

    def test_rejects_zero_or_more_than_four_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for item_count in (0, 5):
                with self.assertRaisesRegex(
                    ControlledProjectionError, "bounded claim boundary"
                ):
                    load_apply(self.write_result(directory, apply_result(item_count)))

    def test_parent_relationship_has_an_approved_shadow_query(self) -> None:
        self.assertIn("relationship.parent_of", QUERY_BY_PREDICATE)

    def test_death_event_has_an_approved_shadow_query(self) -> None:
        self.assertEqual(
            QUERY_BY_PREDICATE["life_event.died"],
            "Have I had any deaths in the family?",
        )

    def test_reported_stance_has_an_approved_shadow_query(self) -> None:
        self.assertEqual(
            QUERY_BY_PREDICATE["stance.reported"],
            "What have I said about worrying about the future?",
        )

    def test_accepts_exact_deferred_admission_result(self) -> None:
        claim_id = "8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"
        outbox_id = "11111111-1111-4111-8111-111111111111"
        apply = apply_result(1)
        apply["outcomes"] = [{"claim_id": claim_id}]
        apply["result_sha256"] = sha256(
            {key: item for key, item in apply.items() if key != "result_sha256"}
        )
        admission: dict[str, object] = {
            "contract_version": (
                "memory_v1_v5_deferred_projection_admission_result_v1"
            ),
            "mode": "apply",
            "owner_user_id": OWNER,
            "source_apply_result_sha256": apply["result_sha256"],
            "rows_written": 1,
            "qdrant_writes": 0,
            "outcomes": [
                {
                    "claim_id": claim_id,
                    "outbox_id": outbox_id,
                    "status": "pending",
                    "predicate": "stance.reported",
                }
            ],
        }
        admission["result_sha256"] = sha256(admission)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_text(json.dumps(admission), encoding="utf-8")
            loaded = load_admission(path, apply)
        self.assertEqual(loaded[claim_id]["outbox_id"], outbox_id)


if __name__ == "__main__":
    unittest.main()
