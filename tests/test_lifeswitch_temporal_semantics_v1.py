from __future__ import annotations

import datetime as dt
import unittest

from pydantic import ValidationError

from rag_engine.lifeswitch_temporal_semantics_v1 import (
    LifeSwitchTemporalContextV1,
    LifeSwitchTemporalWindowV1,
    parse_lifeswitch_temporal_windows_v1,
)


AS_OF_UTC = dt.datetime(2026, 8, 2, 3, 30, tzinfo=dt.timezone.utc)


def context(
    as_of_utc: dt.datetime = AS_OF_UTC,
    timezone_name: str = "America/Chicago",
) -> LifeSwitchTemporalContextV1:
    return LifeSwitchTemporalContextV1.create(
        timezone_name=timezone_name,
        timezone_source="account_setting",
        as_of_utc=as_of_utc,
    )


def resolve(query: str, **kwargs):
    return parse_lifeswitch_temporal_windows_v1(query, context=context(), **kwargs)


class LifeSwitchTemporalSemanticsV1Tests(unittest.TestCase):
    def test_utc_and_account_local_date_are_distinct_authorities(self) -> None:
        value = context()
        self.assertEqual(value.as_of_utc.date(), dt.date(2026, 8, 2))
        self.assertEqual(value.as_of_local_date, dt.date(2026, 8, 1))

    def test_timezone_must_be_authoritative_and_recognized(self) -> None:
        with self.assertRaises(ValueError):
            context(timezone_name="Not/A_Zone")
        value = context()
        payload = value.model_dump(mode="python")
        payload["as_of_local_date"] = dt.date(2026, 8, 2)
        with self.assertRaises(ValidationError):
            LifeSwitchTemporalContextV1.model_validate(payload)

    def test_today_and_yesterday_use_account_local_date(self) -> None:
        today = resolve("today").windows[0]
        yesterday = resolve("yesterday").windows[0]
        self.assertEqual((today.start_date, today.end_date), (dt.date(2026, 8, 1),) * 2)
        self.assertEqual((yesterday.start_date, yesterday.end_date), (dt.date(2026, 7, 31),) * 2)

    def test_last_n_days_are_inclusive(self) -> None:
        value = resolve("over the last 14 days").windows[0]
        self.assertEqual(value.start_date, dt.date(2026, 7, 19))
        self.assertEqual(value.end_date, dt.date(2026, 8, 1))
        self.assertEqual((value.end_date - value.start_date).days, 13)

    def test_current_and_previous_calendar_weeks(self) -> None:
        current = resolve("this week").windows[0]
        previous = resolve("last week").windows[0]
        self.assertEqual((current.start_date, current.end_date), (dt.date(2026, 7, 27), dt.date(2026, 8, 1)))
        self.assertFalse(current.is_complete_period)
        self.assertEqual((previous.start_date, previous.end_date), (dt.date(2026, 7, 20), dt.date(2026, 7, 26)))
        self.assertTrue(previous.is_complete_period)

    def test_current_last_and_trailing_month_differ(self) -> None:
        current = resolve("this month").windows[0]
        previous = resolve("last month").windows[0]
        trailing = resolve("over the last month").windows[0]
        self.assertEqual((current.start_date, current.end_date), (dt.date(2026, 8, 1), dt.date(2026, 8, 1)))
        self.assertEqual((previous.start_date, previous.end_date), (dt.date(2026, 7, 1), dt.date(2026, 7, 31)))
        self.assertEqual((trailing.start_date, trailing.end_date), (dt.date(2026, 7, 3), dt.date(2026, 8, 1)))

    def test_named_month_is_most_recent_non_future_month(self) -> None:
        july = resolve("in July").windows[0]
        september = resolve("in September").windows[0]
        self.assertEqual((july.start_date, july.end_date), (dt.date(2026, 7, 1), dt.date(2026, 7, 31)))
        self.assertEqual((september.start_date, september.end_date), (dt.date(2025, 9, 1), dt.date(2025, 9, 30)))

    def test_explicit_range_overrides_relative_language(self) -> None:
        value = resolve("July 18 through July 31, not this week")
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual((value.windows[0].start_date, value.windows[0].end_date), (dt.date(2026, 7, 18), dt.date(2026, 7, 31)))

    def test_reversed_and_invalid_dates_fail_closed(self) -> None:
        reversed_value = resolve("July 31 through July 18")
        invalid = resolve("February 30, 2026")
        self.assertEqual(reversed_value.status, "UNAVAILABLE")
        self.assertIn("TEMPORAL_EXPRESSION_CONFLICT", reversed_value.reason_codes)
        self.assertEqual(invalid.status, "UNAVAILABLE")

    def test_leap_year_previous_month(self) -> None:
        leap_context = context(dt.datetime(2024, 3, 15, 18, tzinfo=dt.timezone.utc))
        value = parse_lifeswitch_temporal_windows_v1("last month", context=leap_context).windows[0]
        self.assertEqual((value.start_date, value.end_date), (dt.date(2024, 2, 1), dt.date(2024, 2, 29)))

    def test_dst_boundaries_do_not_change_local_date_semantics(self) -> None:
        spring = context(dt.datetime(2026, 3, 8, 7, 30, tzinfo=dt.timezone.utc))
        fall = context(dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(spring.as_of_local_date, dt.date(2026, 3, 8))
        self.assertEqual(fall.as_of_local_date, dt.date(2026, 11, 1))

    def test_two_window_comparison_is_ordered_and_non_overlapping(self) -> None:
        value = resolve("compare last week with this week")
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(len(value.windows), 2)
        self.assertLess(value.windows[0].end_date, value.windows[1].start_date)

    def test_explicit_range_comparison_preserves_both_operands(self) -> None:
        value = resolve(
            "Compare my protein from July 1 through July 7 with July 8 through July 14"
        )
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(
            [(window.start_date, window.end_date) for window in value.windows],
            [
                (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
                (dt.date(2026, 7, 8), dt.date(2026, 7, 14)),
            ],
        )

    def test_iso_range_comparison_preserves_query_order(self) -> None:
        value = resolve(
            "Compare 2026-07-08 through 2026-07-14 with 2026-07-01 through 2026-07-07"
        )
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(
            [(window.start_date, window.end_date) for window in value.windows],
            [
                (dt.date(2026, 7, 8), dt.date(2026, 7, 14)),
                (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
            ],
        )

    def test_range_to_tokens_are_not_comparison_separators(self) -> None:
        value = resolve("Compare July 1 to July 7 with July 8 to July 14")
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(
            [(window.start_date, window.end_date) for window in value.windows],
            [
                (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
                (dt.date(2026, 7, 8), dt.date(2026, 7, 14)),
            ],
        )

    def test_explicit_comparison_overlap_or_missing_operand_fails_closed(self) -> None:
        for query in (
            "Compare July 1 through July 7 with July 7 through July 14",
            "Compare July 1 through July 7 with",
            "Compare July 1 to July 7 to July 8 to July 14",
        ):
            with self.subTest(query=query):
                value = resolve(query)
                self.assertEqual(value.status, "UNAVAILABLE")
                self.assertEqual(value.windows, ())

    def test_compare_prefix_without_connector_fails_closed(self) -> None:
        value = resolve("Compare my protein from July 1 through July 7")
        self.assertEqual(value.status, "UNAVAILABLE")
        self.assertEqual(value.windows, ())

    def test_single_explicit_range_is_unchanged(self) -> None:
        value = resolve("Show my protein from July 1 through July 7")
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(len(value.windows), 1)
        self.assertEqual(
            (value.windows[0].start_date, value.windows[0].end_date),
            (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
        )

    def test_comparison_can_use_validated_prior_window_anchor(self) -> None:
        prior = resolve("last week").windows[0]
        value = resolve("compare that with this week", prior_window=prior)
        self.assertEqual(value.status, "RESOLVED")
        self.assertEqual(len(value.windows), 2)
        self.assertEqual(value.windows[0].source_phrase_kind, "IMPLIED_PREVIOUS_PERIOD")
        self.assertEqual((value.windows[0].start_date, value.windows[0].end_date), (dt.date(2026, 7, 20), dt.date(2026, 7, 26)))

    def test_conflicting_unmarked_periods_fail_closed(self) -> None:
        value = resolve("show this week and last week")
        self.assertEqual(value.status, "UNAVAILABLE")
        self.assertIn("TEMPORAL_EXPRESSION_CONFLICT", value.reason_codes)

    def test_projection_default_is_explicitly_declared(self) -> None:
        missing = resolve("lately")
        declared = resolve("lately", projection_default_days=28)
        self.assertEqual(missing.status, "UNAVAILABLE")
        self.assertEqual((declared.windows[0].end_date - declared.windows[0].start_date).days, 27)
        self.assertEqual(declared.windows[0].semantic_kind, "PROJECTION_DEFAULT")

    def test_week_before_that_requires_validated_prior_window(self) -> None:
        missing = resolve("and the week before that")
        prior = resolve("last week").windows[0]
        inherited = resolve("and the week before that", prior_window=prior)
        self.assertEqual(missing.status, "UNAVAILABLE")
        self.assertEqual((inherited.windows[0].start_date, inherited.windows[0].end_date), (dt.date(2026, 7, 13), dt.date(2026, 7, 19)))

    def test_window_is_strict_frozen_and_hash_bound(self) -> None:
        value = resolve("last week").windows[0]
        payload = value.model_dump(mode="python")
        payload["start_date"] = dt.date(2026, 7, 19)
        with self.assertRaises(ValidationError):
            LifeSwitchTemporalWindowV1.model_validate(payload)
        with self.assertRaises(ValidationError):
            LifeSwitchTemporalWindowV1.model_validate({**value.model_dump(mode="python"), "unexpected": True})


if __name__ == "__main__":
    unittest.main()
