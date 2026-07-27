from __future__ import annotations

import datetime as dt
import unittest
import uuid

from lifeswitch_agentic.plan_domain import PlanDomainError
from lifeswitch_agentic.recovery_adjustments import (
    RecoveryAdjustmentInput,
    RecoveryAdjustmentRecord,
    RecoveryPeriod,
)


class RecoveryAdjustmentDomainTest(unittest.TestCase):
    def test_requires_at_least_one_domain_period(self) -> None:
        with self.assertRaisesRegex(PlanDomainError, "select nutrition"):
            RecoveryAdjustmentInput.create(
                reason_code="surgery_recovery",
                note="",
                nutrition_starts_on=None,
                nutrition_ends_on=None,
                strength_starts_on=None,
                strength_ends_on=None,
            )

    def test_rejects_reversed_or_overlong_periods(self) -> None:
        with self.assertRaisesRegex(PlanDomainError, "cannot be before"):
            RecoveryAdjustmentInput.create(
                reason_code="injury",
                note="",
                nutrition_starts_on=dt.date(2026, 7, 28),
                nutrition_ends_on=dt.date(2026, 7, 27),
                strength_starts_on=None,
                strength_ends_on=None,
            )
        with self.assertRaisesRegex(PlanDomainError, "cannot exceed"):
            RecoveryAdjustmentInput.create(
                reason_code="injury",
                note="",
                nutrition_starts_on=None,
                nutrition_ends_on=None,
                strength_starts_on=dt.date(2026, 1, 1),
                strength_ends_on=dt.date(2027, 1, 2),
            )

    def test_stopping_is_exclusive_and_preserves_historical_days(self) -> None:
        record = RecoveryAdjustmentRecord(
            adjustment_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            reason_code="surgery_recovery",
            note="private",
            nutrition_period=RecoveryPeriod(
                starts_on=dt.date(2026, 7, 27),
                ends_on=dt.date(2026, 7, 27),
            ),
            strength_period=RecoveryPeriod(
                starts_on=dt.date(2026, 7, 27),
                ends_on=dt.date(2026, 8, 9),
            ),
            created_by_actor_user_id=uuid.uuid4(),
            created_at=dt.datetime(2026, 7, 27, tzinfo=dt.UTC),
            stopped_on=dt.date(2026, 7, 30),
            stopped_by_actor_user_id=uuid.uuid4(),
            stopped_at=dt.datetime(2026, 7, 30, tzinfo=dt.UTC),
        )
        payload = record.to_dict(include_private_note=True)
        self.assertEqual(
            payload["nutrition_period"],
            {"starts_on": "2026-07-27", "ends_on": "2026-07-27"},
        )
        self.assertEqual(
            payload["strength_period"],
            {"starts_on": "2026-07-27", "ends_on": "2026-07-29"},
        )

    def test_private_note_can_be_hidden_from_delegated_viewers(self) -> None:
        record = RecoveryAdjustmentRecord(
            adjustment_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            reason_code="illness",
            note="private details",
            nutrition_period=RecoveryPeriod(
                starts_on=dt.date(2026, 7, 27),
                ends_on=dt.date(2026, 7, 27),
            ),
            strength_period=None,
            created_by_actor_user_id=uuid.uuid4(),
            created_at=dt.datetime(2026, 7, 27, tzinfo=dt.UTC),
            stopped_on=None,
            stopped_by_actor_user_id=None,
            stopped_at=None,
        )
        self.assertEqual(record.to_dict(include_private_note=False)["note"], "")
