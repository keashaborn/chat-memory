from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_entity_resolution_budget import (
    EntityResolutionBudgetError,
    expected_table_deltas,
    verify_table_deltas,
)


def plan(operations: list[str], bindings: int, rows: int) -> dict:
    return {
        "items": [
            {"manifest_item": {"operation": operation}}
            for operation in operations
        ],
        "expected_total_bindings": bindings,
        "expected_new_rows": rows,
    }


class EntityResolutionBudgetTest(unittest.TestCase):
    def test_exact_pet_batch_budget(self) -> None:
        value = expected_table_deltas(
            plan(
                [
                    "manual_create_new_and_apply",
                    "auto_apply",
                    "manual_link_existing_and_apply",
                    "auto_apply",
                    "manual_create_new_and_apply",
                    "auto_apply",
                ],
                11,
                34,
            )
        )
        self.assertEqual(value["entity"], 2)
        self.assertEqual(value["entity_resolution_review"], 3)
        self.assertEqual(value["entity_resolution_apply"], 6)
        self.assertEqual(value["entity_alias_observation"], 3)
        self.assertEqual(value["observation_entity_binding"], 11)
        self.assertEqual(value["relational_operation_request"], 9)
        self.assertEqual(value["entity_resolution_reconciliation_v5_2"], 0)

    def test_reconciliation_budget(self) -> None:
        value = expected_table_deltas(
            plan(["reconcile_existing_and_apply"], 2, 10)
        )
        self.assertEqual(value["entity_resolution_reconciliation_v5_2"], 1)
        self.assertEqual(value["entity_resolution_plan"], 1)
        self.assertEqual(value["entity_resolution_candidate"], 1)
        self.assertEqual(value["entity"], 0)

    def test_rejects_false_row_budget(self) -> None:
        with self.assertRaises(EntityResolutionBudgetError):
            expected_table_deltas(
                plan(["manual_create_new_and_apply"], 3, 8)
            )

    def test_verifies_zero_delta_hashes(self) -> None:
        value = plan(["auto_apply"], 1, 3)
        before = {
            "entity_resolution_apply": (1, "a"),
            "observation_entity_binding": (1, "b"),
            "relational_operation_request": (1, "c"),
            "claim": (4, "d"),
        }
        after = {
            "entity_resolution_apply": (2, "changed"),
            "observation_entity_binding": (2, "changed"),
            "relational_operation_request": (2, "changed"),
            "claim": (4, "d"),
        }
        verify_table_deltas(value, before, after)


if __name__ == "__main__":
    unittest.main()
