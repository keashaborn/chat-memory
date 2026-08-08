from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from rag_engine.lifeswitch_query_signals_v2 import (
    LifeSwitchContinuationHintV1,
    LifeSwitchQuerySignalsV2,
    create_lifeswitch_query_signals_v2,
)
from rag_engine.lifeswitch_temporal_semantics_v1 import (
    LifeSwitchTemporalContextV1,
    parse_lifeswitch_temporal_windows_v1,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
FIXTURE = Path(__file__).parent / "fixtures" / "lifeswitch_query_temporal_v2_cases.json"


def context() -> LifeSwitchTemporalContextV1:
    return LifeSwitchTemporalContextV1.create(
        timezone_name="America/Chicago",
        timezone_source="account_setting",
        as_of_utc=dt.datetime(2026, 8, 2, 3, 30, tzinfo=dt.timezone.utc),
    )


def hint(*, domains=("nutrition",), window=None) -> LifeSwitchContinuationHintV1:
    return LifeSwitchContinuationHintV1(
        prior_domains=domains,
        prior_projection_family="nutrition.range.v1" if domains == ("nutrition",) else "training.sessions_by_day.v1",
        prior_window=window,
        prior_output_grain="summary",
        owner_binding_sha256=HASH_A,
        thread_binding_sha256=HASH_B,
        pre_request_snapshot_sha256=HASH_C,
        selection_manifest_sha256=HASH_D,
    )


def signals(query: str, **kwargs) -> LifeSwitchQuerySignalsV2:
    return create_lifeswitch_query_signals_v2(
        query,
        temporal_context=context(),
        **kwargs,
    )


class LifeSwitchQuerySignalsV2Tests(unittest.TestCase):
    def test_synthetic_fixture_matrix(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_version"], "lifeswitch_query_temporal_v2_cases_v1")
        for case in fixture["cases"]:
            with self.subTest(case=case["id"]):
                value = signals(case["query"])
                self.assertEqual(value.status, case["status"])
                self.assertEqual(value.requested_domains, tuple(case["domains"]))
                if value.status != "UNAVAILABLE":
                    self.assertEqual(value.output_grain, case["grain"])
                if "plan_relationship" in case:
                    self.assertEqual(value.plan_relationship, case["plan_relationship"])
                if "subject" in case:
                    self.assertEqual(value.named_subject, case["subject"])
                if "start_date" in case:
                    self.assertEqual(value.temporal_request.windows[0].start_date.isoformat(), case["start_date"])
                    self.assertEqual(value.temporal_request.windows[0].end_date.isoformat(), case["end_date"])
                if "windows" in case:
                    self.assertEqual(
                        [
                            [window.start_date.isoformat(), window.end_date.isoformat()]
                            for window in value.temporal_request.windows
                        ],
                        case["windows"],
                    )

    def test_ordinary_verbs_activate_correct_domains(self) -> None:
        self.assertEqual(signals("What did I eat yesterday?").requested_domains, ("nutrition",))
        self.assertEqual(signals("Did I train yesterday?").requested_domains, ("training",))
        self.assertEqual(signals("How much do I weigh?").requested_domains, ("measurements",))

    def test_general_advice_and_unrelated_questions_are_off(self) -> None:
        for query in (
            "What is a reasonable protein target for strength training?",
            "Who won a television competition in 2015?",
        ):
            with self.subTest(query=query):
                value = signals(query)
                self.assertEqual(value.status, "OFF")
                self.assertEqual(value.requested_domains, ())

    def test_daily_cross_domain_grain_is_preserved(self) -> None:
        value = signals("For each day in the last 14 days show my protein and whether I trained.")
        self.assertEqual(value.requested_domains, ("nutrition", "training"))
        self.assertEqual(value.output_grain, "daily_rows")

    def test_named_exercise_is_bounded_and_training_only(self) -> None:
        value = signals("Show my progress on Decline Bench Press over the last month.")
        self.assertEqual(value.named_subject, "Decline Bench Press")
        self.assertEqual(value.requested_domains, ("training",))
        self.assertEqual(value.output_grain, "trend")

    def test_valid_continuation_inherits_only_metadata(self) -> None:
        prior = parse_lifeswitch_temporal_windows_v1("last month", context=context()).windows[0]
        value = signals(
            "What about last week?",
            continuation_hint=hint(window=prior),
            expected_owner_binding_sha256=HASH_A,
            expected_thread_binding_sha256=HASH_B,
            expected_pre_request_snapshot_sha256=HASH_C,
        )
        self.assertEqual(value.status, "ACTIVE")
        self.assertEqual(value.requested_domains, ("nutrition",))
        self.assertEqual(value.continuation_kind, "prior_domain")
        self.assertEqual(value.temporal_request.windows[0].start_date, dt.date(2026, 7, 20))

    def test_explicit_current_domain_overrides_prior_domain(self) -> None:
        prior = parse_lifeswitch_temporal_windows_v1("last week", context=context()).windows[0]
        value = signals(
            "What about protein?",
            continuation_hint=hint(domains=("training",), window=prior),
            expected_owner_binding_sha256=HASH_A,
            expected_thread_binding_sha256=HASH_B,
            expected_pre_request_snapshot_sha256=HASH_C,
        )
        self.assertEqual(value.requested_domains, ("nutrition",))
        self.assertEqual(value.continuation_kind, "prior_window")

    def test_current_snapshot_can_recover_named_subject(self) -> None:
        prior = parse_lifeswitch_temporal_windows_v1("last month", context=context()).windows[0]
        value = signals(
            "What about decline bench?",
            continuation_hint=hint(domains=("training",), window=prior),
            expected_owner_binding_sha256=HASH_A,
            expected_thread_binding_sha256=HASH_B,
            expected_pre_request_snapshot_sha256=HASH_C,
        )
        self.assertEqual(value.named_subject, "decline bench")
        self.assertEqual(value.continuation_kind, "prior_subject")

    def test_compare_that_uses_prior_window_only_after_exact_binding(self) -> None:
        prior = parse_lifeswitch_temporal_windows_v1("last week", context=context()).windows[0]
        value = signals(
            "Compare that with this week.",
            continuation_hint=hint(window=prior),
            expected_owner_binding_sha256=HASH_A,
            expected_thread_binding_sha256=HASH_B,
            expected_pre_request_snapshot_sha256=HASH_C,
        )
        self.assertEqual(value.status, "ACTIVE")
        self.assertEqual(value.output_grain, "comparison")
        self.assertEqual(len(value.temporal_request.windows), 2)
        denied = signals("Compare that with this week.")
        self.assertEqual(denied.status, "UNAVAILABLE")

    def test_explicit_range_comparison_requires_two_windows(self) -> None:
        value = signals(
            "Compare my protein from July 1 through July 7 with July 8 through July 14"
        )
        self.assertEqual(value.status, "ACTIVE")
        self.assertEqual(value.output_grain, "comparison")
        self.assertEqual(
            [
                (window.start_date, window.end_date)
                for window in value.temporal_request.windows
            ],
            [
                (dt.date(2026, 7, 1), dt.date(2026, 7, 7)),
                (dt.date(2026, 7, 8), dt.date(2026, 7, 14)),
            ],
        )

    def test_invalid_explicit_comparison_is_unavailable_without_windows(self) -> None:
        for query in (
            "Compare my protein from July 1 through July 7 with July 7 through July 14",
            "Compare my protein from July 1 through July 7 with",
        ):
            with self.subTest(query=query):
                value = signals(query)
                self.assertEqual(value.status, "UNAVAILABLE")
                self.assertEqual(value.output_grain, "comparison")
                self.assertEqual(value.temporal_request.windows, ())

    def test_missing_stale_cross_owner_or_cross_thread_hint_fails_closed(self) -> None:
        prior = parse_lifeswitch_temporal_windows_v1("last week", context=context()).windows[0]
        for supplied, owner, thread, snapshot in (
            (None, HASH_A, HASH_B, HASH_C),
            (hint(window=prior), HASH_D, HASH_B, HASH_C),
            (hint(window=prior), HASH_A, HASH_D, HASH_C),
            (hint(window=prior), HASH_A, HASH_B, HASH_D),
        ):
            with self.subTest(supplied=supplied, owner=owner, thread=thread, snapshot=snapshot):
                value = signals(
                    "What about last week?",
                    continuation_hint=supplied,
                    expected_owner_binding_sha256=owner,
                    expected_thread_binding_sha256=thread,
                    expected_pre_request_snapshot_sha256=snapshot,
                )
                self.assertEqual(value.status, "UNAVAILABLE")
                self.assertEqual(value.requested_domains, ())
                self.assertIn("CONTINUATION_CONTEXT_UNAVAILABLE", value.reason_codes)

    def test_signals_carry_no_owner_thread_permission_or_storage_fields(self) -> None:
        keys = set(signals("What are my macros?").model_dump(mode="json"))
        for forbidden in ("owner_user_id", "thread_id", "role", "table", "sql", "permission", "projection_id"):
            self.assertNotIn(forbidden, keys)

    def test_signals_are_strict_frozen_and_hash_bound(self) -> None:
        value = signals("What are my macros?")
        payload = value.model_dump(mode="python")
        payload["output_grain"] = "ranking"
        with self.assertRaises(ValidationError):
            LifeSwitchQuerySignalsV2.model_validate(payload)
        with self.assertRaises(ValidationError):
            LifeSwitchQuerySignalsV2.model_validate({**value.model_dump(mode="python"), "unexpected": True})


if __name__ == "__main__":
    unittest.main()
