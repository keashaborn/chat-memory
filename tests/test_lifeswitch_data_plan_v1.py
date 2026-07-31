from __future__ import annotations

import datetime as dt
import unittest

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1


TODAY = dt.date(2026, 7, 29)  # Wednesday


class LifeSwitchDataPlanV1Tests(unittest.TestCase):
    def test_unrelated_question_performs_no_lifeswitch_access(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Who won America's Next Top Model in 2015?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OFF")
        self.assertFalse(plan.data_access)
        self.assertEqual(plan.domains, ())
        self.assertEqual(plan.budget.max_rows, 0)
        self.assertEqual(plan.budget.max_prompt_tokens, 0)

    def test_general_nutrition_advice_does_not_read_personal_data(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What is a reasonable protein target for strength training?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OFF")

    def test_broad_status_question_selects_compact_combined_projection(self) -> None:
        plan = create_lifeswitch_data_plan_v1("How am I doing?", today=TODAY)
        self.assertEqual(plan.intent, "OVERALL_STATUS")
        self.assertEqual(
            plan.domains,
            ("plan", "nutrition", "training", "conditioning", "measurements"),
        )
        self.assertEqual(plan.budget.max_prompt_tokens, 800)

    def test_explicit_plan_adherence_selects_combined_projection(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Look at my plan and make sure I am meeting all my macros and exercise goals.",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OVERALL_STATUS")
        self.assertIn("explicit_plan_adherence_request", plan.reason_codes)

    def test_monday_protein_selects_one_local_day(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Was I low on protein Monday?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "NUTRITION_DAY")
        self.assertEqual(plan.window.start_date, dt.date(2026, 7, 27))
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 27))
        self.assertEqual(plan.budget.max_prompt_tokens, 250)

    def test_recent_macros_select_bounded_nutrition_range(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How have my macros been this week?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "NUTRITION_RANGE")
        self.assertEqual(plan.window.end_date, TODAY)
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 20)

    def test_training_day_selects_one_session_projection(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What did I do in my workout yesterday?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "TRAINING_SESSION")
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 28))

    def test_named_exercise_progression_is_bounded(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How is my squat progressing?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_PROGRESSION")
        self.assertEqual(plan.subject, "squat")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 83)
        self.assertEqual(plan.budget.max_rows, 200)

    def test_personal_progress_across_lifting_weights_selects_training_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "If you look at my progress with the different weights, "
            "I've been doing do you have any suggestions?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "TRAINING_SUMMARY")
        self.assertTrue(plan.data_access)
        self.assertEqual(plan.domains, ("training", "conditioning", "plan"))
        self.assertIn(
            "explicit_personal_lifting_progress_request",
            plan.reason_codes,
        )
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 27)

    def test_personal_lifting_loads_over_time_select_training_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How have my lifting loads changed over time?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "TRAINING_SUMMARY")
        self.assertIn(
            "explicit_personal_lifting_progress_request",
            plan.reason_codes,
        )

    def test_personal_weight_without_lifting_context_remains_measurement(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How has my body weight changed over time?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "MEASUREMENTS_SUMMARY")
        self.assertEqual(plan.domains, ("measurements",))

    def test_general_weight_selection_does_not_read_personal_data(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What weights should I use?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OFF")
        self.assertFalse(plan.data_access)

    def test_personal_measurements_use_measurements_only(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How is my waist measurement changing?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "MEASUREMENTS_SUMMARY")
        self.assertEqual(plan.domains, ("measurements",))


if __name__ == "__main__":
    unittest.main()
