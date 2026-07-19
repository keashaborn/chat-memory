from __future__ import annotations

import math
import unittest
import uuid

from lifeswitch_agentic.plan_domain import (
    PlanDocumentV1,
    PlanDomainError,
    RevisionState,
    RevisionTrigger,
    assert_revision_transition,
    diff_plan_documents,
    validate_activation,
    validate_plan_document,
    validate_revision_base,
)


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
COACH = uuid.UUID("22222222-2222-4222-8222-222222222222")
PLAN_V1 = uuid.UUID("33333333-3333-4333-8333-333333333333")
PLAN_V2 = uuid.UUID("44444444-4444-4444-8444-444444444444")


def plan(**overrides):
    value = {
        "phase": "cut",
        "phase_label": "Cut to 16% body fat",
        "primary_goal": "Reduce body-fat percentage while maintaining strength.",
        "start_date": "2026-07-01",
        "review_date": "2026-07-15",
        "review_cadence": "weekly",
        "body_state": {"body_fat_percent": 20},
        "nutrition_targets": {"calorie_target": {"lower": 1800, "upper": 2100}},
        "training_targets": {"strength_sessions_per_week": 3},
        "conditioning_targets": {},
        "activity_targets": {"steps_minimum": 7000},
        "recovery_targets": {},
        "monitoring_rules": {"review_every_days": 7},
        "coach_notes": "",
    }
    value.update(overrides)
    return PlanDocumentV1.from_mapping(value)


class PlanDocumentV1Test(unittest.TestCase):
    def test_legacy_profile_converts_without_metadata_or_owner(self) -> None:
        document = PlanDocumentV1.from_legacy_profile(
            {
                **plan().to_dict(),
                "owner_user_id": str(OWNER),
                "plan_profile_id": str(PLAN_V1),
                "created_at": "2026-07-01T00:00:00Z",
            }
        )
        payload = document.to_dict()
        self.assertNotIn("owner_user_id", payload)
        self.assertNotIn("plan_profile_id", payload)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["phase"], "cut")

    def test_hash_is_stable_across_nested_key_order(self) -> None:
        first = plan(nutrition_targets={"protein": 160, "calorie_target": {"upper": 2100, "lower": 1800}})
        second = plan(nutrition_targets={"calorie_target": {"lower": 1800, "upper": 2100}, "protein": 160})
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.sha256(), second.sha256())
        self.assertRegex(first.sha256(), r"^[0-9a-f]{64}$")

    def test_document_is_deeply_immutable_after_validation(self) -> None:
        source = {"calorie_target": {"lower": 1800, "upper": 2100}}
        document = plan(nutrition_targets=source)
        original_hash = document.sha256()
        source["calorie_target"]["lower"] = 1200
        self.assertEqual(document.sha256(), original_hash)
        with self.assertRaises(TypeError):
            document.nutrition_targets["protein"] = 160  # type: ignore[index]
        with self.assertRaises(TypeError):
            document.nutrition_targets["calorie_target"]["lower"] = 1200  # type: ignore[index]

    def test_unknown_authoritative_field_is_rejected(self) -> None:
        value = plan().to_dict()
        value["owner_user_id"] = str(OWNER)
        with self.assertRaises(PlanDomainError) as caught:
            PlanDocumentV1.from_mapping(value)
        self.assertEqual(caught.exception.code, "unknown_plan_field")

    def test_invalid_phase_fails_closed(self) -> None:
        with self.assertRaisesRegex(PlanDomainError, "phase must be") as caught:
            plan(phase="diet")
        self.assertEqual(caught.exception.code, "invalid_phase")

    def test_section_must_be_object(self) -> None:
        with self.assertRaisesRegex(PlanDomainError, "must be a JSON object"):
            plan(nutrition_targets=[1800, 2100])

    def test_non_finite_number_is_rejected(self) -> None:
        with self.assertRaises(PlanDomainError) as caught:
            plan(body_state={"weight": math.nan})
        self.assertEqual(caught.exception.code, "invalid_json_number")

    def test_invalid_date_is_rejected(self) -> None:
        with self.assertRaises(PlanDomainError) as caught:
            plan(start_date="07/01/2026")
        self.assertEqual(caught.exception.code, "invalid_date")

    def test_semantic_validation_returns_error_and_warning(self) -> None:
        document = plan(primary_goal="", body_state={}, monitoring_rules={})
        issues = validate_plan_document(document)
        self.assertEqual(
            {(issue.code, issue.severity) for issue in issues},
            {
                ("primary_goal_missing", "error"),
                ("cut_body_state_missing", "warning"),
                ("monitoring_rules_missing", "warning"),
            },
        )


class RevisionStateTest(unittest.TestCase):
    def test_draft_can_propose_and_proposal_can_activate(self) -> None:
        assert_revision_transition(RevisionState.DRAFT, RevisionState.PROPOSED)
        assert_revision_transition(RevisionState.PROPOSED, RevisionState.ACTIVATED)

    def test_terminal_state_cannot_reopen(self) -> None:
        with self.assertRaises(PlanDomainError) as caught:
            assert_revision_transition(RevisionState.REJECTED, RevisionState.DRAFT)
        self.assertEqual(caught.exception.code, "invalid_revision_transition")

    def test_requested_changes_require_new_linked_draft(self) -> None:
        assert_revision_transition(RevisionState.PROPOSED, RevisionState.NEEDS_CHANGES)
        with self.assertRaises(PlanDomainError):
            assert_revision_transition(RevisionState.NEEDS_CHANGES, RevisionState.DRAFT)

    def test_initial_plan_requires_null_base_and_active_pointer(self) -> None:
        validate_revision_base(
            trigger=RevisionTrigger.INITIAL_PLAN,
            base_plan_version_id=None,
            current_active_plan_version_id=None,
        )
        with self.assertRaises(PlanDomainError) as caught:
            validate_revision_base(
                trigger=RevisionTrigger.INITIAL_PLAN,
                base_plan_version_id=None,
                current_active_plan_version_id=PLAN_V1,
            )
        self.assertEqual(caught.exception.code, "state_conflict")

    def test_stale_revision_base_fails_conflict(self) -> None:
        with self.assertRaises(PlanDomainError) as caught:
            validate_revision_base(
                trigger=RevisionTrigger.OWNER_REQUEST,
                base_plan_version_id=PLAN_V1,
                current_active_plan_version_id=PLAN_V2,
            )
        self.assertEqual(caught.exception.code, "state_conflict")

    def test_only_owner_can_approve_activation(self) -> None:
        with self.assertRaises(PlanDomainError) as caught:
            validate_activation(
                proposal_state=RevisionState.PROPOSED,
                trigger=RevisionTrigger.OWNER_REQUEST,
                base_plan_version_id=PLAN_V1,
                current_active_plan_version_id=PLAN_V1,
                owner_user_id=OWNER,
                approving_actor_user_id=COACH,
                validation_status="valid",
                next_version_number=2,
            )
        self.assertEqual(caught.exception.code, "owner_approval_required")

    def test_valid_activation_returns_version_decision(self) -> None:
        decision = validate_activation(
            proposal_state=RevisionState.PROPOSED,
            trigger=RevisionTrigger.OWNER_REQUEST,
            base_plan_version_id=PLAN_V1,
            current_active_plan_version_id=PLAN_V1,
            owner_user_id=OWNER,
            approving_actor_user_id=OWNER,
            validation_status="valid_with_warnings",
            next_version_number=2,
        )
        self.assertEqual(decision.next_version_number, 2)
        self.assertEqual(decision.prior_active_plan_version_id, PLAN_V1)


class PlanDiffTest(unittest.TestCase):
    def test_nested_diff_is_deterministic_and_preserves_presence(self) -> None:
        old = plan(nutrition_targets={"calorie_target": {"lower": 1800, "upper": 2100}})
        new = plan(nutrition_targets={"calorie_target": {"lower": 1750, "upper": 2100}, "protein": 160})
        changes = diff_plan_documents(old, new)
        self.assertEqual(
            [change.field_path for change in changes],
            [
                "/nutrition_targets/calorie_target/lower",
                "/nutrition_targets/protein",
            ],
        )
        added = changes[1]
        self.assertFalse(added.old_present)
        self.assertTrue(added.new_present)
        self.assertEqual(added.new_value, 160)

    def test_initial_plan_diff_uses_empty_base(self) -> None:
        changes = diff_plan_documents(None, plan())
        paths = {change.field_path for change in changes}
        self.assertIn("/primary_goal", paths)
        self.assertIn("/nutrition_targets", paths)


if __name__ == "__main__":
    unittest.main()
