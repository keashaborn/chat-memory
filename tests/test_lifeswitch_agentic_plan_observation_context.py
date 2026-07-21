from __future__ import annotations

import datetime as dt
import unittest
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from lifeswitch_agentic.plan_observation_context import (
    CanonicalPlanObservationContextRepository,
    ObservationPermissions,
)


class FakeConnection:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        for marker, rows in self.rows.items():
            if marker in query:
                return rows
        raise AssertionError(f"unexpected query: {query}")


def document(calories: Any = {"lower": 1800, "upper": 2100}) -> dict[str, Any]:
    return {
        "nutrition_targets": {
            "calorie_target": calories,
            "protein_g": 160,
        },
        "training_targets": {"workouts_per_week": 4},
    }


class CanonicalPlanObservationContextRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_computes_bounded_summaries_and_never_returns_raw_observations(self) -> None:
        today = dt.datetime.now(ZoneInfo("America/Chicago")).date()
        nutrition = [
            {
                "day": today - dt.timedelta(days=offset),
                "entry_count": 3,
                "kcal": 1800 if offset % 2 == 0 else 2200,
                "protein_g": 170,
            }
            for offset in range(14)
        ]
        measurements = [
            {
                "local_date": today - dt.timedelta(days=offset),
                "weight_value": 200 - offset / 10,
                "weight_unit": "lb",
                "waist_value": 38 - offset / 100,
                "body_fat_percent": 20 - offset / 100,
                "measurement_unit": "in",
            }
            for offset in (0, 3, 6, 7, 10, 13, 35)
        ]
        training = [
            {
                "day": today - dt.timedelta(days=offset),
                "active_set_count": 12,
                "exercise_count": 4,
                "strength_set_count": 12,
                "strength_exercise_count": 4,
                "rehab_set_count": 0,
                "rehab_exercise_count": 0,
                "unknown_role_set_count": 0,
                "unknown_role_exercise_count": 0,
            }
            for offset in (1, 3, 8, 10, 15, 17)
        ]
        conditioning = [
            {"day": today - dt.timedelta(days=1), "duration_min": 30},
            {"day": today - dt.timedelta(days=8), "duration_min": 20},
        ]
        conn = FakeConnection(
            {
                "lifeswitch_plan_context:nutrition_daily_totals": nutrition,
                "lifeswitch_plan_context:measurements": measurements,
                "lifeswitch_plan_context:resistance_sessions": training,
                "lifeswitch_plan_context:conditioning_sessions": conditioning,
            }
        )
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="America/Chicago",
            document=document(),
            permissions=ObservationPermissions(True, True, True),
        )

        self.assertEqual(result["nutrition"]["data_sufficiency"], "sufficient")
        self.assertEqual(result["nutrition"]["calories"]["adherence"]["days_within_range"], 7)
        self.assertEqual(result["nutrition"]["protein"]["adherence"]["days_meeting_target"], 14)
        self.assertEqual(
            result["measurements"]["weight"]["weekly_average_trend_lb"]["data_sufficiency"],
            "sufficient",
        )
        self.assertTrue(result["training"]["strength_adherence"]["rehab_exclusion_supported"])
        self.assertEqual(result["conditioning"]["session_count"], 2)
        self.assertEqual(result["activity"]["steps"]["status"], "not_collected")
        self.assertFalse(result["writes_performed"])
        self.assertNotIn("raw_observations", result)

        owner_arguments = [call[1][0] for call in conn.calls]
        self.assertEqual(len(owner_arguments), 4)
        self.assertEqual(len(set(str(value) for value in owner_arguments)), 1)

    async def test_point_calorie_target_is_not_scored_as_hit_or_miss(self) -> None:
        today = dt.datetime.now(ZoneInfo("UTC")).date()
        conn = FakeConnection(
            {
                "lifeswitch_plan_context:nutrition_daily_totals": [
                    {"day": today, "entry_count": 2, "kcal": 2007, "protein_g": 160}
                ],
                "lifeswitch_plan_context:measurements": [],
                "lifeswitch_plan_context:resistance_sessions": [],
                "lifeswitch_plan_context:conditioning_sessions": [],
            }
        )
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="UTC",
            document=document(2000),
            permissions=ObservationPermissions(True, True, True),
        )
        adherence = result["nutrition"]["calories"]["adherence"]
        self.assertEqual(adherence["status"], "not_evaluable")
        self.assertEqual(adherence["reason"], "point_target_has_no_acceptable_range")

    async def test_structured_daily_and_weekly_rules_are_scored_separately(self) -> None:
        today = dt.datetime.now(ZoneInfo("UTC")).date()
        nutrition = [
            {
                "day": today - dt.timedelta(days=offset),
                "entry_count": 2,
                "kcal": 1800 if offset < 3 else 2000,
                "protein_g": 190 if offset != 5 else 170,
            }
            for offset in range(7)
        ]
        conn = FakeConnection(
            {
                "lifeswitch_plan_context:nutrition_daily_totals": nutrition,
                "lifeswitch_plan_context:measurements": [],
                "lifeswitch_plan_context:resistance_sessions": [],
                "lifeswitch_plan_context:conditioning_sessions": [],
            }
        )
        structured = document()
        structured["nutrition_targets"] = {
            "calories": "2000",
            "protein_g": "180",
            "calorie_target": {
                "nominal_kcal": 2000,
                "daily_range_kcal": {"lower": 1800, "upper": 2100},
                "rolling_average_kcal": {
                    "window_days": 7,
                    "lower": 1800,
                    "upper": 2000,
                },
            },
            "protein_target": {
                "minimum_g": 180,
                "weekly_adherence": {
                    "mode": "days_hit",
                    "window_days": 7,
                    "required_hit_days": 6,
                },
            },
            "adherence_rule": {
                "daily_requires_both_calorie_and_protein": True,
                "weekly_requires_both_calorie_and_protein": True,
            },
        }
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="UTC",
            document=structured,
            permissions=ObservationPermissions(True, True, True),
        )
        nutrition_result = result["nutrition"]
        self.assertEqual(
            nutrition_result["combined_daily_adherence"]["days_meeting_both"], 6
        )
        weekly = nutrition_result["weekly_evaluation"]
        self.assertEqual(weekly["status"], "evaluable")
        self.assertTrue(weekly["calorie_average_within_range"])
        self.assertEqual(weekly["protein_days_meeting_minimum"], 6)
        self.assertTrue(weekly["protein_days_hit_rule_met"])
        self.assertTrue(weekly["combined_rule_met"])

    async def test_delegated_context_respects_each_view_permission(self) -> None:
        conn = FakeConnection({})
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="UTC",
            document=document(),
            permissions=ObservationPermissions(False, False, False),
        )
        self.assertEqual(conn.calls, [])
        self.assertEqual(result["nutrition"]["required_permission"], "nutrition:view")
        self.assertEqual(result["training"]["required_permission"], "training:view")
        self.assertEqual(result["measurements"]["required_permission"], "measurements:view")

    async def test_rehab_only_session_is_excluded_from_strength_adherence(self) -> None:
        today = dt.datetime.now(ZoneInfo("UTC")).date()
        conn = FakeConnection(
            {
                "lifeswitch_plan_context:nutrition_daily_totals": [],
                "lifeswitch_plan_context:measurements": [],
                "lifeswitch_plan_context:resistance_sessions": [
                    {
                        "day": today,
                        "active_set_count": 3,
                        "exercise_count": 1,
                        "strength_set_count": 0,
                        "strength_exercise_count": 0,
                        "rehab_set_count": 3,
                        "rehab_exercise_count": 1,
                        "unknown_role_set_count": 0,
                        "unknown_role_exercise_count": 0,
                    },
                    {
                        "day": today - dt.timedelta(days=1),
                        "active_set_count": 12,
                        "exercise_count": 4,
                        "strength_set_count": 12,
                        "strength_exercise_count": 4,
                        "rehab_set_count": 0,
                        "rehab_exercise_count": 0,
                        "unknown_role_set_count": 0,
                        "unknown_role_exercise_count": 0,
                    },
                ],
                "lifeswitch_plan_context:conditioning_sessions": [],
            }
        )
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="UTC",
            document=document(),
            permissions=ObservationPermissions(True, True, True),
        )
        training = result["training"]
        self.assertEqual(training["all_logged_resistance_sessions"], 2)
        self.assertEqual(training["strength_sessions"], 1)
        self.assertEqual(training["rehab_sessions"], 1)
        self.assertEqual(training["rehab_only_sessions"], 1)
        self.assertEqual(training["strength_sessions_last_7_days"], 1)
        self.assertFalse(training["strength_adherence"]["target_met"])
        self.assertTrue(training["strength_adherence"]["rehab_exclusion_supported"])

    async def test_training_reads_current_observations_and_never_promotes_unknown_roles(self) -> None:
        today = dt.datetime.now(ZoneInfo("UTC")).date()
        conn = FakeConnection(
            {
                "lifeswitch_plan_context:nutrition_daily_totals": [],
                "lifeswitch_plan_context:measurements": [],
                "lifeswitch_plan_context:resistance_sessions": [
                    {
                        "day": today,
                        "active_set_count": 4,
                        "exercise_count": 1,
                        "strength_set_count": 0,
                        "strength_exercise_count": 0,
                        "rehab_set_count": 0,
                        "rehab_exercise_count": 0,
                        "unknown_role_set_count": 4,
                        "unknown_role_exercise_count": 1,
                    }
                ],
                "lifeswitch_plan_context:conditioning_sessions": [],
            }
        )
        result = await CanonicalPlanObservationContextRepository().summarize(
            conn,  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            owner_timezone="UTC",
            document=document(),
            permissions=ObservationPermissions(True, True, True),
        )

        training = result["training"]
        self.assertEqual(training["strength_sessions"], 0)
        self.assertEqual(training["unknown_role_active_sets"], 4)
        self.assertEqual(training["unknown_role_exercises"], 1)
        resistance_query = next(
            query for query, _args in conn.calls
            if "lifeswitch_plan_context:resistance_sessions" in query
        )
        self.assertIn("training_session_current_v", resistance_query)
        self.assertIn("l.capture_role = 'strength'", resistance_query)
        self.assertNotIn("my_exercise", resistance_query)
        self.assertNotIn("'strength')", resistance_query)

        conditioning_query = next(
            query for query, _args in conn.calls
            if "lifeswitch_plan_context:conditioning_sessions" in query
        )
        self.assertIn("conditioning_session_current_v", conditioning_query)


if __name__ == "__main__":
    unittest.main()
