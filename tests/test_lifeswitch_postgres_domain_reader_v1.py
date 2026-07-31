from __future__ import annotations

import datetime as dt
import unittest
import uuid
from typing import Any

from rag_engine.lifeswitch_postgres_domain_reader_v1 import (
    PostgresLifeSwitchDomainReaderV1,
)
from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_domain_context_v1 import (
    TrustedLifeSwitchContextRequestV1,
    render_lifeswitch_context_v1,
)
from rag_engine.lifeswitch_domain_provider_v1 import LifeSwitchDomainContextProviderV1


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
THREAD = uuid.UUID("22222222-2222-4222-8222-222222222222")
TODAY = dt.date(2026, 7, 29)


def plan_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": "lean_gain",
        "phase_label": "Build",
        "primary_goal": "Add muscle",
        "nutrition_targets": {
            "calorie_target": {"lower": 2800, "upper": 3000},
            "protein_g": 190,
        },
        "training_targets": {"workouts_per_week": 4},
        "conditioning_targets": {"sessions_per_week": 2},
    }


class FakeConnection:
    def __init__(
        self,
        *,
        active_plan: dict[str, Any] | None = None,
        legacy_plan: dict[str, Any] | None = None,
        nutrition_rows: list[dict[str, Any]] | None = None,
        training_rows: list[dict[str, Any]] | None = None,
        conditioning_rows: list[dict[str, Any]] | None = None,
        progression_rows: list[dict[str, Any]] | None = None,
        frequency_rows: list[dict[str, Any]] | None = None,
        lifting_summary_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.active_plan = active_plan
        self.legacy_plan = legacy_plan
        self.nutrition_rows = nutrition_rows or []
        self.training_rows = training_rows or []
        self.conditioning_rows = conditioning_rows or []
        self.progression_rows = progression_rows or []
        self.frequency_rows = frequency_rows or []
        self.lifting_summary_rows = lifting_summary_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append((query, args))
        if "lifeswitch_chat.read_plan_v1" in query:
            if self.active_plan is not None:
                return {
                    "plan_source": "agentic_active",
                    "document": self.active_plan["document"],
                }
            if self.legacy_plan is not None:
                return {
                    "plan_source": "legacy_fallback",
                    "document": self.legacy_plan,
                }
            return None
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args: Any):
        self.calls.append((query, args))
        if "read_nutrition_daily_v1" in query:
            return self.nutrition_rows
        if "read_training_day_v1" in query:
            return self.training_rows
        if "read_conditioning_sessions_v1" in query:
            return self.conditioning_rows
        if "read_exercise_progression_v1" in query:
            return self.progression_rows
        if "read_exercise_frequency_v1" in query:
            return self.frequency_rows
        if "read_lifting_progression_summary_v2" in query:
            return self.lifting_summary_rows
        raise AssertionError(f"unexpected fetch query: {query}")


class FakeObservations:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def summarize(self, _conn, **kwargs):
        self.calls.append(kwargs)
        permissions = kwargs["permissions"]
        return {
            "as_of_local_date": TODAY.isoformat(),
            "nutrition": (
                {
                    "status": "available",
                    "logged_days": 7,
                    "missing_log_days": 14,
                    "data_sufficiency": "limited",
                    "calories": {"average_on_logged_days": 2875.0},
                    "protein": {"average_on_logged_days": 192.0},
                }
                if permissions.nutrition
                else {"status": "unavailable"}
            ),
            "training": (
                {
                    "status": "available",
                    "all_logged_resistance_sessions": 4,
                    "strength_sessions_last_7_days": 4,
                    "data_sufficiency": "limited",
                }
                if permissions.training
                else {"status": "unavailable"}
            ),
            "conditioning": (
                {"status": "available", "session_count": 2}
                if permissions.training
                else {"status": "unavailable"}
            ),
            "measurements": (
                {
                    "status": "available",
                    "weight": {"observation_days": 3},
                }
                if permissions.measurements
                else {"status": "unavailable"}
            ),
            "activity": {"status": "unavailable"},
            "recovery": {"status": "unavailable"},
        }


class PostgresLifeSwitchDomainReaderV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_active_agentic_plan_wins_without_legacy_read(self) -> None:
        conn = FakeConnection(active_plan={"document": plan_document()})
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_plan(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
        )
        self.assertEqual(result.plan_source, "agentic_active")
        self.assertEqual(result.payload["phase"], "lean_gain")
        self.assertEqual(len(conn.calls), 1)
        self.assertIn("read_plan_v1", conn.calls[0][0])
        self.assertEqual(conn.calls[0][1], (CONTEXT,))

    async def test_legacy_plan_is_explicit_fallback_only(self) -> None:
        legacy = {
            "phase": "maintenance",
            "phase_label": "Maintain",
            "primary_goal": "Maintain performance",
            "start_date": TODAY,
            "review_date": None,
            "review_cadence": "weekly",
            "body_state": {},
            "nutrition_targets": {"protein_g": 180},
            "training_targets": {"workouts_per_week": 3},
            "conditioning_targets": {},
            "activity_targets": {},
            "recovery_targets": {},
            "monitoring_rules": {},
            "coach_notes": "",
        }
        conn = FakeConnection(active_plan=None, legacy_plan=legacy)
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_plan(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
        )
        self.assertEqual(result.plan_source, "legacy_fallback")
        self.assertEqual(result.source_relations, ("lifeswitch_plan.plan_profile",))
        self.assertEqual(len(conn.calls), 1)

    async def test_nutrition_day_returns_all_four_macros_and_plan_targets(self) -> None:
        conn = FakeConnection(
            active_plan={"document": plan_document()},
            nutrition_rows=[
                {
                    "day": TODAY,
                    "entry_count": 4,
                    "kcal": 2888.4,
                    "protein_g": 194.2,
                    "carbs_g": 335.7,
                    "fat_g": 82.3,
                }
            ],
        )
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_nutrition_day(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            day=TODAY,
        )
        totals = result.payload["daily"][0]
        self.assertEqual(totals["calories"], 2888.4)
        self.assertEqual(totals["protein_g"], 194.2)
        self.assertEqual(totals["carbs_g"], 335.7)
        self.assertEqual(totals["fat_g"], 82.3)
        self.assertEqual(result.payload["plan_targets"]["protein_g"], 190)

    async def test_missing_nutrition_day_is_empty_not_fabricated(self) -> None:
        conn = FakeConnection(active_plan=None, legacy_plan=None, nutrition_rows=[])
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_nutrition_day(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            day=TODAY,
        )
        self.assertEqual(result.status, "EMPTY")
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.payload, {})

    async def test_twenty_one_day_nutrition_range_preserves_daily_values_within_budget(self) -> None:
        rows = [
            {
                "day": TODAY - dt.timedelta(days=offset),
                "entry_count": 4,
                "kcal": 2800 + offset,
                "protein_g": 180 + offset,
                "carbs_g": 320 + offset,
                "fat_g": 80 + offset,
            }
            for offset in range(20, -1, -1)
        ]
        reader = PostgresLifeSwitchDomainReaderV1(
            FakeConnection(
                active_plan={"document": plan_document()},
                nutrition_rows=rows,
            ),
            context_id=CONTEXT,
        )
        query = (
            "Can you look at my nutrition for last couple weeks and let me know "
            "how I'm doing with hitting my macros"
        )
        plan = create_lifeswitch_data_plan_v1(query, today=TODAY)
        request = TrustedLifeSwitchContextRequestV1.create(
            request_id="nutrition-range-regression",
            authenticated_actor_user_id=OWNER,
            owner_user_id=OWNER,
            thread_id=THREAD,
            conversation_snapshot_sha256="a" * 64,
            owner_timezone="America/Chicago",
            query=query,
            data_plan=plan,
        )

        envelope = await LifeSwitchDomainContextProviderV1(reader).select(request)
        rendered = render_lifeswitch_context_v1(envelope)
        payload = envelope.sections[0].payload

        self.assertEqual(plan.intent, "NUTRITION_RANGE")
        self.assertEqual(plan.budget.max_prompt_tokens, 450)
        self.assertEqual(
            payload["daily_columns"],
            ["date", "calories", "protein_g", "carbs_g", "fat_g"],
        )
        self.assertEqual(len(payload["daily_rows"]), 21)
        self.assertEqual(payload["daily_rows"][0][0], "2026-07-09")
        self.assertEqual(payload["daily_rows"][-1][0], TODAY.isoformat())
        self.assertNotIn("daily", payload)
        self.assertLessEqual(rendered.estimated_tokens, 450)

    async def test_training_day_returns_resistance_and_conditioning(self) -> None:
        conn = FakeConnection(
            training_rows=[
                {
                    "day": TODAY,
                    "name": "Upper",
                    "set_count": 12,
                    "exercise_count": 4,
                    "total_volume": 8450.0,
                }
            ],
            conditioning_rows=[
                {
                    "day": TODAY,
                    "name": "Incline walk",
                    "category": "cardio",
                    "modality": "treadmill",
                    "duration_min": 25,
                    "intensity": "moderate",
                    "distance_value": 1.5,
                    "distance_unit": "mi",
                    "heart_rate_avg": 128,
                    "recovery_impact": "low",
                }
            ],
        )
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_training_session(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            day=TODAY,
        )
        self.assertEqual(result.status, "AVAILABLE")
        self.assertEqual(result.record_count, 2)
        self.assertEqual(result.payload["resistance_sessions"][0]["sets"], 12)
        self.assertEqual(result.payload["conditioning_sessions"][0]["duration_min"], 25.0)
        self.assertIn(
            "lifeswitch_training.conditioning_session_current_v",
            result.source_relations,
        )

    async def test_overall_status_uses_plan_and_all_authorized_observation_lanes(self) -> None:
        conn = FakeConnection(
            active_plan={"document": plan_document()},
            nutrition_rows=[
                {
                    "day": TODAY,
                    "entry_count": 4,
                    "kcal": 2888,
                    "protein_g": 194,
                    "carbs_g": 336,
                    "fat_g": 82,
                }
            ],
        )
        observations = FakeObservations()
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
            observation_repository=observations,
        ).read_overall_status(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
        )
        permissions = observations.calls[0]["permissions"]
        self.assertTrue(permissions.nutrition)
        self.assertTrue(permissions.training)
        self.assertTrue(permissions.measurements)
        self.assertEqual(
            result.payload["nutrition"]["macro_averages_on_logged_days"]["carbs_g"],
            336.0,
        )
        self.assertIn("public.lifeswitch_measurement_entries", result.source_relations)

    async def test_progression_query_keeps_owner_window_and_subject_parameterized(self) -> None:
        conn = FakeConnection(
            progression_rows=[
                {
                    "day": TODAY,
                    "exercise_name": "Back Squat",
                    "set_count": 4,
                    "total_reps": 20,
                    "max_load": 315,
                    "total_volume": 5400,
                    "load_unit": "lb",
                }
            ]
        )
        start = TODAY - dt.timedelta(days=83)
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_exercise_progression(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            start_date=start,
            end_date=TODAY,
            subject="squat",
        )
        self.assertEqual(result.payload["observations"][0]["max_load"], 315.0)
        query, arguments = conn.calls[0]
        self.assertIn("read_exercise_progression_v1", query)
        self.assertNotIn("owner_user_id", query)
        self.assertEqual(arguments, (CONTEXT, start, TODAY, "squat"))

    async def test_exercise_frequency_is_compact_and_owner_context_bound(self) -> None:
        start = TODAY - dt.timedelta(days=83)
        conn = FakeConnection(
            frequency_rows=[
                {
                    "exercise_id": "squat",
                    "exercise_name": "Back Squat",
                    "effective_role": "strength",
                    "set_count": 24,
                    "session_count": 6,
                    "first_day": start,
                    "last_day": TODAY,
                    "resolution_sources": ["capture_role"],
                    "role_conflict": False,
                }
            ]
        )
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_exercise_frequency(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            start_date=start,
            end_date=TODAY,
        )
        self.assertEqual(result.payload["columns"][0], "exercise_name")
        self.assertEqual(result.payload["rows"][0][2:4], [24, 6])
        query, arguments = conn.calls[0]
        self.assertIn("read_exercise_frequency_v1", query)
        self.assertEqual(arguments, (CONTEXT, start, TODAY))
        self.assertIn(
            "lifeswitch_training.training_set_effective_role_v1",
            result.source_relations,
        )

    async def test_lifting_summary_pairs_first_and_latest_metrics(self) -> None:
        start = TODAY - dt.timedelta(days=83)
        conn = FakeConnection(
            active_plan={"document": plan_document()},
            lifting_summary_rows=[
                {
                    "exercise_id": "squat",
                    "exercise_name": "Back Squat",
                    "exposure_count": 6,
                    "set_count": 24,
                    "first_day": start,
                    "last_day": TODAY,
                    "first_set_count": 4,
                    "latest_set_count": 3,
                    "first_max_load": 275,
                    "latest_max_load": 315,
                    "first_total_reps": 20,
                    "latest_total_reps": 18,
                    "first_total_volume": 5000,
                    "latest_total_volume": 5400,
                    "first_average_load": 250,
                    "latest_average_load": 300,
                    "first_load_unit": "lb",
                    "latest_load_unit": "lb",
                    "resolution_sources": ["capture_role"],
                }
            ],
        )
        result = await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_lifting_progression_summary(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            start_date=start,
            end_date=TODAY,
        )
        self.assertEqual(result.plan_source, "agentic_active")
        self.assertEqual(result.payload["plan_targets"]["workouts_per_week"], 4)
        columns = result.payload["columns"]
        row = dict(zip(columns, result.payload["rows"][0], strict=True))
        self.assertEqual(row["first_max_load"], 275.0)
        self.assertEqual(row["latest_max_load"], 315.0)
        self.assertEqual(row["first_set_count"], 4)
        self.assertEqual(row["latest_set_count"], 3)
        self.assertEqual(row["first_average_load"], 250.0)
        self.assertEqual(
            result.payload["comparison_policy"]["different_sets"],
            "normalize_per_set",
        )
        self.assertEqual(
            result.payload["comparison_policy"]["raw_totals_when_sets_differ"],
            "work_only",
        )
        self.assertEqual(len(conn.calls), 2)
        self.assertIn("read_plan_v1", conn.calls[0][0])
        self.assertIn("read_lifting_progression_summary_v2", conn.calls[1][0])

    async def test_frequency_max_rows_remain_within_prompt_budget(self) -> None:
        start = TODAY - dt.timedelta(days=83)
        rows = [
            {
                "exercise_name": f"Exercise {index} " + ("x" * 64),
                "effective_role": "strength",
                "set_count": 99,
                "session_count": 24,
                "first_day": start,
                "last_day": TODAY,
                "role_conflict": False,
            }
            for index in range(12)
        ]
        query = "What exercises do I do the most?"
        plan = create_lifeswitch_data_plan_v1(query, today=TODAY)
        request = TrustedLifeSwitchContextRequestV1.create(
            request_id="frequency-budget-regression",
            authenticated_actor_user_id=OWNER,
            owner_user_id=OWNER,
            thread_id=THREAD,
            conversation_snapshot_sha256="b" * 64,
            owner_timezone="America/Chicago",
            query=query,
            data_plan=plan,
        )
        envelope = await LifeSwitchDomainContextProviderV1(
            PostgresLifeSwitchDomainReaderV1(
                FakeConnection(frequency_rows=rows),
                context_id=CONTEXT,
            )
        ).select(request)
        rendered = render_lifeswitch_context_v1(envelope)
        self.assertEqual(envelope.sections[0].record_count, 12)
        self.assertLessEqual(rendered.estimated_tokens, 550)

    async def test_lifting_summary_max_rows_remain_within_prompt_budget(self) -> None:
        start = TODAY - dt.timedelta(days=83)
        rows = [
            {
                "exercise_name": f"Exercise {index} " + ("x" * 64),
                "exposure_count": 24,
                "set_count": 99,
                "first_day": start,
                "last_day": TODAY,
                "first_set_count": 4,
                "latest_set_count": 4,
                "first_max_load": 100,
                "latest_max_load": 125,
                "first_total_reps": 30,
                "latest_total_reps": 30,
                "first_total_volume": 3000,
                "latest_total_volume": 3750,
                "first_average_load": 100,
                "latest_average_load": 125,
                "first_load_unit": "lb",
                "latest_load_unit": "lb",
            }
            for index in range(12)
        ]
        query = "Have I been progressing with my weights and if so, which ones?"
        plan = create_lifeswitch_data_plan_v1(query, today=TODAY)
        request = TrustedLifeSwitchContextRequestV1.create(
            request_id="lifting-budget-regression",
            authenticated_actor_user_id=OWNER,
            owner_user_id=OWNER,
            thread_id=THREAD,
            conversation_snapshot_sha256="c" * 64,
            owner_timezone="America/Chicago",
            query=query,
            data_plan=plan,
        )
        envelope = await LifeSwitchDomainContextProviderV1(
            PostgresLifeSwitchDomainReaderV1(
                FakeConnection(
                    active_plan={"document": plan_document()},
                    lifting_summary_rows=rows,
                ),
                context_id=CONTEXT,
            )
        ).select(request)
        rendered = render_lifeswitch_context_v1(envelope)
        self.assertEqual(envelope.sections[0].record_count, 12)
        self.assertLessEqual(rendered.estimated_tokens, 750)

    async def test_reader_uses_only_gateway_sql(self) -> None:
        conn = FakeConnection(active_plan={"document": plan_document()})
        await PostgresLifeSwitchDomainReaderV1(
            conn,
            context_id=CONTEXT,
        ).read_nutrition_range(
            owner_user_id=OWNER,
            owner_timezone="America/Chicago",
            start_date=TODAY - dt.timedelta(days=6),
            end_date=TODAY,
        )
        sql = "\n".join(query.lower() for query, _ in conn.calls)
        self.assertIn("lifeswitch_chat.read_plan_v1", sql)
        self.assertIn("lifeswitch_chat.read_nutrition_daily_v1", sql)
        self.assertNotIn("from lifeswitch_nutrition", sql)
        self.assertNotIn("from lifeswitch_agentic", sql)


if __name__ == "__main__":
    unittest.main()
