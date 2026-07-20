from __future__ import annotations

import datetime as dt
import unittest
import uuid
from typing import Any

from lifeswitch_agentic.plan_domain import PlanDocumentV1, PlanDomainError
from lifeswitch_agentic.plan_recommendations import _agent_may_edit
from lifeswitch_agentic.plan_workout_templates import PlanWorkoutTemplateService


def document(linked_workouts: Any) -> PlanDocumentV1:
    return PlanDocumentV1.from_mapping(
        {
            "phase": "cut",
            "phase_label": "Cut",
            "primary_goal": "Reduce body fat while maintaining strength.",
            "training_targets": {"linked_workouts": linked_workouts},
        }
    )


class FakeConnection:
    def __init__(self, owner: uuid.UUID, template_id: uuid.UUID) -> None:
        self.owner = owner
        self.template_id = template_id
        self.updated_at = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.timezone.utc)
        self.exercise_id = uuid.uuid4()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if args != (self.template_id, self.owner):
            return None
        return {
            "workout_template_id": self.template_id,
            "name": "Back/Bicept Pull A",
            "notes": "Primary pull workout",
            "updated_at": self.updated_at,
        }

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "group by" in query.casefold():
            return [
                {
                    "workout_template_id": self.template_id,
                    "name": "Back/Bicept Pull A",
                    "notes": "Primary pull workout",
                    "updated_at": self.updated_at,
                    "exercise_count": 2,
                    "strength_exercise_count": 1,
                    "rehab_exercise_count": 1,
                }
            ]
        if "wte.workout_template_id," in query and "wte.workout_template_exercise_id" not in query:
            return [
                {
                    "workout_template_id": self.template_id,
                    "display_name_snapshot": "Close-Grip Lat Pulldown",
                    "exercise_id": "close_grip_lat_pulldown",
                    "sort_order": 0,
                    "planned_sets": 3,
                    "default_weight": 100,
                    "default_reps": 10,
                    "exercise_role": "strength",
                }
            ]
        if "workout_template_exercise_segment" in query:
            return [
                {
                    "workout_template_exercise_id": self.exercise_id,
                    "segment_index": 0,
                    "label": "Working set",
                    "default_weight": 100,
                    "default_reps": 10,
                }
            ]
        return [
            {
                "workout_template_exercise_id": self.exercise_id,
                "exercise_id": "close_grip_lat_pulldown",
                "display_name_snapshot": "Close-Grip Lat Pulldown",
                "sort_order": 0,
                "set_type": "straight",
                "planned_sets": 3,
                "default_weight": 100,
                "default_reps": 10,
                "flags": "",
                "exercise_role": "strength",
            }
        ]


class PlanWorkoutTemplateServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_agent_cannot_edit_template_references_or_snapshots(self) -> None:
        self.assertFalse(_agent_may_edit("/training_targets/linked_workouts"))
        self.assertFalse(
            _agent_may_edit(
                "/training_targets/linked_workouts/0/workout_template_id"
            )
        )
        self.assertTrue(_agent_may_edit("/training_targets/progression_rule"))

    async def test_lists_owner_scoped_role_summary(self) -> None:
        owner = uuid.uuid4()
        template_id = uuid.uuid4()
        service = PlanWorkoutTemplateService()
        options = await service.list_options(
            FakeConnection(owner, template_id),  # type: ignore[arg-type]
            owner_user_id=owner,
        )
        self.assertEqual(options[0]["workout_template_id"], str(template_id))
        self.assertEqual(options[0]["role"], "mixed")
        self.assertEqual(options[0]["rehab_exercise_count"], 1)

    async def test_materializes_immutable_prescription_snapshot(self) -> None:
        owner = uuid.uuid4()
        template_id = uuid.uuid4()
        service = PlanWorkoutTemplateService()
        result = await service.materialize_document(
            FakeConnection(owner, template_id),  # type: ignore[arg-type]
            owner_user_id=owner,
            document=document(
                [
                    {
                        "workout_template_id": str(template_id),
                        "sessions_per_week": 2,
                        "schedule_notes": "Monday and Thursday",
                        "prescription_snapshot": {"name": "untrusted client value"},
                    }
                ]
            ),
        )
        linked = result.to_dict()["training_targets"]["linked_workouts"]
        self.assertEqual(linked[0]["sessions_per_week"], 2)
        self.assertEqual(linked[0]["prescription_snapshot"]["name"], "Back/Bicept Pull A")
        self.assertEqual(
            linked[0]["prescription_snapshot"]["exercises"][0]["role"],
            "strength",
        )
        self.assertEqual(
            linked[0]["prescription_snapshot"]["exercises"][0]["segments"][0]["label"],
            "Working set",
        )

    async def test_rejects_duplicate_template_reference(self) -> None:
        owner = uuid.uuid4()
        template_id = uuid.uuid4()
        service = PlanWorkoutTemplateService()
        with self.assertRaises(PlanDomainError) as caught:
            await service.materialize_document(
                FakeConnection(owner, template_id),  # type: ignore[arg-type]
                owner_user_id=owner,
                document=document(
                    [
                        {"workout_template_id": str(template_id), "sessions_per_week": 1},
                        {"workout_template_id": str(template_id), "sessions_per_week": 1},
                    ]
                ),
            )
        self.assertEqual(caught.exception.code, "invalid_section")

    async def test_rejects_template_outside_owner_scope(self) -> None:
        owner = uuid.uuid4()
        template_id = uuid.uuid4()
        service = PlanWorkoutTemplateService()
        with self.assertRaises(PlanDomainError) as caught:
            await service.materialize_document(
                FakeConnection(uuid.uuid4(), template_id),  # type: ignore[arg-type]
                owner_user_id=owner,
                document=document(
                    [{"workout_template_id": str(template_id), "sessions_per_week": 1}]
                ),
            )
        self.assertEqual(caught.exception.code, "entity_out_of_scope")


if __name__ == "__main__":
    unittest.main()
