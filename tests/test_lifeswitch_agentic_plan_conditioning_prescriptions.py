from __future__ import annotations

import datetime as dt
import json
import unittest
import uuid
from decimal import Decimal
from typing import Any

from lifeswitch_agentic.plan_conditioning_prescriptions import (
    PlanConditioningPrescriptionService,
)
from lifeswitch_agentic.plan_domain import PlanDocumentV1, PlanDomainError
from lifeswitch_agentic.plan_recommendations import _agent_may_edit


def document(linked_conditioning: Any) -> PlanDocumentV1:
    return PlanDocumentV1.from_mapping(
        {
            "phase": "cut",
            "phase_label": "Cut",
            "primary_goal": "Reduce body fat while maintaining strength.",
            "conditioning_targets": {
                "linked_conditioning": linked_conditioning,
            },
        }
    )


class FakeConnection:
    def __init__(self, owner: uuid.UUID, prescription_id: uuid.UUID) -> None:
        self.owner = owner
        self.prescription_id = prescription_id
        self.updated_at = dt.datetime(2026, 7, 20, 16, 0, tzinfo=dt.timezone.utc)

    def row(self) -> dict[str, Any]:
        return {
            "my_conditioning_prescription_id": self.prescription_id,
            "name": "Incline Treadmill Walk",
            "category": "cardio",
            "modality": "treadmill",
            "purpose": "Aerobic base",
            "target_duration_min": 20,
            "target_frequency_per_week": Decimal("2.0"),
            "target_intensity": "moderate",
            "preferred_timing": "after lifting",
            "recovery_constraints": "Stop for foot pain",
            "notes": "Use a comfortable incline",
            "dose_type": "time",
            "dose_config": json.dumps({"minutes": 20, "incline_percent": 4}),
            "updated_at": self.updated_at,
            "library_slug": "incline-treadmill-walk",
            "library_name": "Incline Treadmill Walk",
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if args != (self.prescription_id, self.owner):
            return None
        return self.row()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if args != (self.owner,):
            return []
        return [self.row()]


class PlanConditioningPrescriptionServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_agent_cannot_edit_references_or_snapshots(self) -> None:
        self.assertFalse(
            _agent_may_edit("/conditioning_targets/linked_conditioning")
        )
        self.assertFalse(
            _agent_may_edit(
                "/conditioning_targets/linked_conditioning/0/"
                "my_conditioning_prescription_id"
            )
        )
        self.assertTrue(_agent_may_edit("/conditioning_targets/intensity"))

    async def test_lists_owner_scoped_canonical_options(self) -> None:
        owner = uuid.uuid4()
        prescription_id = uuid.uuid4()
        service = PlanConditioningPrescriptionService()
        options = await service.list_options(
            FakeConnection(owner, prescription_id),  # type: ignore[arg-type]
            owner_user_id=owner,
        )
        self.assertEqual(
            options[0]["my_conditioning_prescription_id"], str(prescription_id)
        )
        self.assertEqual(options[0]["target_frequency_per_week"], 2)
        self.assertEqual(options[0]["dose_config"]["incline_percent"], 4)

    async def test_materializes_immutable_prescription_snapshot(self) -> None:
        owner = uuid.uuid4()
        prescription_id = uuid.uuid4()
        service = PlanConditioningPrescriptionService()
        result = await service.materialize_document(
            FakeConnection(owner, prescription_id),  # type: ignore[arg-type]
            owner_user_id=owner,
            document=document(
                [
                    {
                        "my_conditioning_prescription_id": str(prescription_id),
                        "sessions_per_week": 2.5,
                        "schedule_notes": "Tuesday, Friday, alternating Sunday",
                        "prescription_snapshot": {"name": "untrusted client value"},
                    }
                ]
            ),
        )
        linked = result.to_dict()["conditioning_targets"]["linked_conditioning"]
        self.assertEqual(linked[0]["sessions_per_week"], 2.5)
        self.assertEqual(
            linked[0]["prescription_snapshot"]["name"],
            "Incline Treadmill Walk",
        )
        self.assertEqual(
            linked[0]["prescription_snapshot"]["dose_config"]["minutes"], 20
        )

    async def test_rejects_duplicate_reference(self) -> None:
        owner = uuid.uuid4()
        prescription_id = uuid.uuid4()
        service = PlanConditioningPrescriptionService()
        with self.assertRaises(PlanDomainError) as caught:
            await service.materialize_document(
                FakeConnection(owner, prescription_id),  # type: ignore[arg-type]
                owner_user_id=owner,
                document=document(
                    [
                        {
                            "my_conditioning_prescription_id": str(prescription_id),
                            "sessions_per_week": 1,
                        },
                        {
                            "my_conditioning_prescription_id": str(prescription_id),
                            "sessions_per_week": 1,
                        },
                    ]
                ),
            )
        self.assertEqual(caught.exception.code, "invalid_section")

    async def test_rejects_reference_outside_owner_scope(self) -> None:
        owner = uuid.uuid4()
        prescription_id = uuid.uuid4()
        service = PlanConditioningPrescriptionService()
        with self.assertRaises(PlanDomainError) as caught:
            await service.materialize_document(
                FakeConnection(uuid.uuid4(), prescription_id),  # type: ignore[arg-type]
                owner_user_id=owner,
                document=document(
                    [
                        {
                            "my_conditioning_prescription_id": str(prescription_id),
                            "sessions_per_week": 1,
                        }
                    ]
                ),
            )
        self.assertEqual(caught.exception.code, "entity_out_of_scope")


if __name__ == "__main__":
    unittest.main()
