from __future__ import annotations

import datetime as dt
import unittest

from seebx.capabilities.plans.data_plan import create_lifeswitch_data_plan_v1


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
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 6)

    def test_absolute_nutrition_range_is_preserved(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "For each day from July 18 through July 31, show my calories and "
            "protein and compare both with my plan.",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "NUTRITION_RANGE")
        self.assertEqual(plan.window.start_date, dt.date(2026, 7, 18))
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 31))
        self.assertEqual(plan.confidence, "high")

    def test_combined_daily_range_selects_cross_domain_projection(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "For each day from July 18 through July 31, show my protein "
            "and whether I completed strength training.",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "DAILY_STATUS_RANGE")
        self.assertEqual(
            plan.domains,
            ("nutrition", "training", "conditioning", "plan"),
        )
        self.assertEqual(plan.window.start_date, dt.date(2026, 7, 18))
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 31))

    def test_relative_cross_domain_range_uses_requested_fourteen_days(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Which days during the last 14 days missed my protein minimum, "
            "and on which days did I strength train?",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "DAILY_STATUS_RANGE")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 13)

    def test_relative_daily_training_range_uses_requested_fourteen_days(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "For each day in the last 14 days, show whether I strength trained "
            "or did conditioning.",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "TRAINING_RANGE")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 13)

    def test_training_day_selects_one_session_projection(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What did I do in my workout yesterday?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "TRAINING_SESSION")
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 28))

    def test_absolute_daily_training_range_selects_exact_window(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "For each day from July 18 through July 31, show whether I "
            "completed strength training or conditioning.",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "TRAINING_RANGE")
        self.assertEqual(plan.domains, ("training", "conditioning"))
        self.assertEqual(plan.window.start_date, dt.date(2026, 7, 18))
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 31))

    def test_named_exercise_progression_is_bounded(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How is my squat progressing?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_PROGRESSION")
        self.assertEqual(plan.subject, "squat")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 83)
        self.assertEqual(plan.budget.max_rows, 200)

    def test_recent_progress_on_named_exercise_extracts_exercise(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Show my recent progress on Decline Bench Press",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_PROGRESSION")
        self.assertEqual(plan.subject, "Decline Bench Press")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 83)

    def test_recent_modifier_is_never_used_as_exercise_subject(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Show my recent progress",
            today=TODAY,
        )
        self.assertNotEqual(plan.subject, "recent")

    def test_parenthesized_exercise_name_is_preserved(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Show my recent progress on Chest Press (Plate-Loaded)",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_PROGRESSION")
        self.assertEqual(plan.subject, "Chest Press (Plate-Loaded)")

    def test_personal_progress_across_lifting_weights_selects_progression_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "If you look at my progress with the different weights, "
            "I've been doing do you have any suggestions?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "LIFTING_PROGRESSION_SUMMARY")
        self.assertTrue(plan.data_access)
        self.assertEqual(plan.domains, ("training", "plan"))
        self.assertIn(
            "explicit_personal_lifting_progress_request",
            plan.reason_codes,
        )
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 83)

    def test_personal_lifting_loads_over_time_select_progression_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How have my lifting loads changed over time?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "LIFTING_PROGRESSION_SUMMARY")
        self.assertIn(
            "explicit_personal_lifting_progress_request",
            plan.reason_codes,
        )

    def test_exercise_frequency_prompt_selects_bounded_history(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What exercises do I do the most?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_FREQUENCY")
        self.assertEqual(plan.domains, ("training",))
        self.assertEqual(plan.budget.max_rows, 12)
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 83)

    def test_exercise_frequency_honors_four_week_window(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What exercises have I done most over the last four weeks?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_FREQUENCY")
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 27)

    def test_exercise_frequency_honors_absolute_window(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What exercises did I do most from July 4 through July 31?",
            today=dt.date(2026, 8, 1),
        )
        self.assertEqual(plan.intent, "EXERCISE_FREQUENCY")
        self.assertEqual(plan.window.start_date, dt.date(2026, 7, 4))
        self.assertEqual(plan.window.end_date, dt.date(2026, 7, 31))

    def test_exercise_data_access_prompt_selects_bounded_history(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Do you have access to any exercise data that I've done?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "EXERCISE_FREQUENCY")

    def test_broad_weightlifting_goal_prompt_selects_progression_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "How have I been doing with my weightlifting goals over the last couple weeks?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "LIFTING_PROGRESSION_SUMMARY")
        self.assertEqual(plan.domains, ("training", "plan"))
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 13)

    def test_broad_weight_progress_prompt_selects_progression_summary(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Have I been progressing with my weights and if so, which ones?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "LIFTING_PROGRESSION_SUMMARY")

    def test_implicit_personal_lift_progress_honors_month_window(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Which lifts have progressed over the last month?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "LIFTING_PROGRESSION_SUMMARY")
        self.assertEqual(
            plan.reason_codes,
            ("implicit_personal_lifting_progress_request",),
        )
        self.assertEqual((plan.window.end_date - plan.window.start_date).days, 29)

    def test_generic_lift_progress_question_does_not_read_personal_data(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "Which lifts have progressed most in Olympic competition?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OFF")
        self.assertFalse(plan.data_access)

    def test_general_exercise_advice_does_not_read_personal_history(self) -> None:
        plan = create_lifeswitch_data_plan_v1(
            "What exercises build shoulders?",
            today=TODAY,
        )
        self.assertEqual(plan.intent, "OFF")
        self.assertFalse(plan.data_access)

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
