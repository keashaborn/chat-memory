from __future__ import annotations

import datetime as dt
import unittest

from pydantic import ValidationError

from rag_engine.lifeswitch_data_plan_v2 import (
    MAX_COMBINED_ROWS,
    MAX_COMBINED_TOKENS,
    LifeSwitchDataPlanV2,
    create_lifeswitch_data_plan_v2,
)
from rag_engine.lifeswitch_query_signals_v2 import create_lifeswitch_query_signals_v2
from rag_engine.lifeswitch_temporal_semantics_v1 import LifeSwitchTemporalContextV1


def context() -> LifeSwitchTemporalContextV1:
    return LifeSwitchTemporalContextV1.create(
        timezone_name="America/Chicago",
        timezone_source="account_setting",
        as_of_utc=dt.datetime(2026, 8, 2, 3, 30, tzinfo=dt.timezone.utc),
    )


def plan(query: str, *, runtime_max_selections: int = 4):
    signals = create_lifeswitch_query_signals_v2(
        query,
        temporal_context=context(),
    )
    return create_lifeswitch_data_plan_v2(
        signals,
        runtime_max_selections=runtime_max_selections,
    )


class LifeSwitchDataPlanV2Tests(unittest.TestCase):
    def test_unrelated_query_is_off_with_zero_budget(self) -> None:
        value = plan("Who won a television competition in 2015?")
        self.assertEqual(value.status, "OFF")
        self.assertFalse(value.data_access)
        self.assertEqual(value.selections, ())
        self.assertEqual(value.combined_budget.max_rows, 0)
        self.assertEqual(value.combined_budget.max_prompt_tokens, 0)

    def test_current_targets_use_allowlisted_current_plan(self) -> None:
        value = plan("What are my current targets?")
        self.assertEqual([item.projection_id for item in value.selections], ["plan.current.v1"])

    def test_nutrition_day_and_range_use_existing_allowlist(self) -> None:
        day = plan("What did I eat yesterday?")
        range_value = plan("How did I eat over the last 14 days?")
        self.assertEqual(day.selections[0].projection_id, "nutrition.daily.v1")
        self.assertEqual(range_value.selections[0].projection_id, "nutrition.range.v1")

    def test_training_ranking_and_named_progression_are_distinct(self) -> None:
        ranking = plan("What exercises did I do most last month?")
        progression = plan("Show my progress on Decline Bench Press over the last month.")
        self.assertEqual(ranking.selections[0].projection_id, "training.exercise_frequency.v1")
        self.assertEqual(progression.selections[0].projection_id, "training.exercise_progression.v1")
        self.assertEqual(progression.selections[0].named_subject, "Decline Bench Press")

    def test_cross_domain_daily_request_is_deterministically_ordered(self) -> None:
        value = plan("For each day over the last 14 days show my protein, training, and conditioning.")
        self.assertEqual(
            [item.projection_id for item in value.selections],
            [
                "nutrition.range.v1",
                "training.sessions_by_day.v1",
                "conditioning.sessions_by_day.v1",
            ],
        )
        self.assertEqual([item.execution_ordinal for item in value.selections], [1, 2, 3])

    def test_contract_and_runtime_selection_limits_are_enforced(self) -> None:
        value = plan(
            "How am I doing with my plan, nutrition, training, conditioning, and measurements?",
            runtime_max_selections=4,
        )
        self.assertLessEqual(value.selected_projection_count, 4)
        self.assertTrue(value.rejected_selections)
        self.assertTrue(
            {item.reason_code for item in value.rejected_selections}
            <= {"PROJECTION_COUNT_LIMIT", "PROJECTION_BUDGET_LIMIT"}
        )

    def test_combined_budget_is_exact_and_bounded(self) -> None:
        value = plan("For each day over the last 14 days compare my protein and training with my plan.")
        self.assertEqual(value.combined_budget.max_rows, sum(item.row_budget for item in value.selections))
        self.assertEqual(value.combined_budget.max_prompt_tokens, sum(item.token_budget for item in value.selections))
        self.assertLessEqual(value.combined_budget.max_rows, MAX_COMBINED_ROWS)
        self.assertLessEqual(value.combined_budget.max_prompt_tokens, MAX_COMBINED_TOKENS)

    def test_historical_plan_authority_fails_closed(self) -> None:
        value = plan("Compare my current plan with my previous plan.")
        self.assertEqual(value.status, "UNAVAILABLE")
        self.assertFalse(value.data_access)
        self.assertEqual(value.rejected_selections[0].reason_code, "HISTORICAL_PLAN_AUTHORITY_UNAVAILABLE")
        self.assertEqual(value.rejected_selections[0].row_budget, 0)

    def test_date_bound_measurement_request_fails_closed(self) -> None:
        value = plan("Show my body measurements in July.")
        self.assertEqual(value.status, "UNAVAILABLE")
        self.assertFalse(value.data_access)
        self.assertEqual(value.rejected_selections[0].reason_code, "MEASUREMENT_DATE_PROJECTION_UNAVAILABLE")

    def test_current_measurement_summary_remains_available(self) -> None:
        value = plan("What is my current weight?")
        self.assertEqual(value.status, "ACTIVE")
        self.assertEqual(value.selections[0].projection_id, "measurements.core_summary.v1")

    def test_rejected_selection_never_carries_access_or_budget(self) -> None:
        value = plan(
            "How am I doing with my plan, nutrition, training, conditioning, and measurements?",
            runtime_max_selections=2,
        )
        for rejection in value.rejected_selections:
            self.assertFalse(rejection.data_access)
            self.assertEqual(rejection.row_budget, 0)
            self.assertEqual(rejection.token_budget, 0)

    def test_plan_is_strict_frozen_and_hash_bound(self) -> None:
        value = plan("How did I eat over the last 14 days?")
        payload = value.model_dump(mode="python")
        payload["data_access"] = False
        with self.assertRaises(ValidationError):
            LifeSwitchDataPlanV2.model_validate(payload)
        with self.assertRaises(ValidationError):
            LifeSwitchDataPlanV2.model_validate({**value.model_dump(mode="python"), "unexpected": True})


if __name__ == "__main__":
    unittest.main()
