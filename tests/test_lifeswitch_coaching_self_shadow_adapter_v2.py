from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from seebx.capabilities.coaching.contracts import (
    AuthorizationBindingV2,
    AuthorizationBudgetV2,
    AuthorizationRechecksV2,
    AuthorizationSnapshotV2,
    ProjectionAuthorizationDecisionV2,
    ProjectionEnvelopeV1,
    canonical_sha256,
)
from seebx.capabilities.coaching.shadow_adapter import (
    LifeSwitchSelfShadowAdapterError,
    SELF_S1_SHADOW_ADAPTER_MAP_V1,
    SELF_S1_SHADOW_FIELD_POLICY,
    adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2,
)
from seebx.capabilities.plans.data_plan import (
    LifeSwitchDataWindowV1,
    create_lifeswitch_data_plan_v1,
)
from seebx.capabilities.plans.domain_context import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
THREAD = UUID("22222222-2222-4222-8222-222222222222")
REQUEST_ID = "33333333-3333-4333-8333-333333333333"
CONTEXT_SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
CONVERSATION_SHA = "a" * 64
TRANSACTION_SHA = "b" * 64
NOW = dt.datetime(2026, 8, 1, 17, 30, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 8, 1)


def _request(query: str) -> TrustedLifeSwitchContextRequestV1:
    return TrustedLifeSwitchContextRequestV1.create(
        request_id=REQUEST_ID,
        authenticated_actor_user_id=OWNER,
        owner_user_id=OWNER,
        thread_id=THREAD,
        conversation_snapshot_sha256=CONVERSATION_SHA,
        owner_timezone="America/Chicago",
        query=query,
        data_plan=create_lifeswitch_data_plan_v1(query, today=TODAY),
    )


def _section(
    *,
    projection: str,
    payload: dict,
    sources: tuple[str, ...],
    start: dt.date | None = None,
    end: dt.date | None = None,
    status: str = "AVAILABLE",
) -> LifeSwitchContextSectionV1:
    window = None
    if start is not None or end is not None:
        window = LifeSwitchDataWindowV1(
            start_date=start or end,
            end_date=end or start,
        )
    return LifeSwitchContextSectionV1.create(
        projection=projection,
        status=status,
        window=window,
        record_count=1 if status == "AVAILABLE" else 0,
        source_relations=sources,
        payload=payload,
    )


def _envelope(request, *sections, plan_source="not_requested"):
    return create_lifeswitch_context_envelope_v1(
        request=request,
        plan_source=plan_source,
        as_of_local_date=TODAY,
        sections=tuple(sections),
        generated_at=NOW,
    )


def _authorization(
    request: TrustedLifeSwitchContextRequestV1,
    target_projection_id: str,
    scopes: tuple[str, ...],
    *,
    actor: UUID = OWNER,
    subject: UUID = OWNER,
    perspective: str = "self",
    serialized_bytes: int = 0,
) -> AuthorizationSnapshotV2:
    binding = AuthorizationBindingV2(
        request_id=request.request_id,
        thread_id=str(request.thread_id),
        context_snapshot_id=CONTEXT_SNAPSHOT_ID,
        actor_user_id=str(actor),
        subject_user_id=str(subject),
        perspective=perspective,
        account_timezone=request.owner_timezone,
        evaluated_at=NOW.isoformat().replace("+00:00", "Z"),
        transaction_snapshot_digest=TRANSACTION_SHA,
    )
    decision = ProjectionAuthorizationDecisionV2(
        projection_id=target_projection_id,
        outcome="authorized",
        executed=True,
        required_scopes=scopes,
        sensitivity_ceiling="S1",
        field_policy_version=SELF_S1_SHADOW_FIELD_POLICY,
        internal_reason_code=None,
    )
    return AuthorizationSnapshotV2(
        binding=binding,
        decision="allow",
        authorization_epoch="self-shadow-epoch-1",
        sensitivity_ceiling="S1",
        relationship_basis="self" if perspective == "self" else "accepted_relationship",
        relationship_binding_digest=None if perspective == "self" else "c" * 64,
        effective_grants=(),
        projection_decisions=(decision,),
        rechecks=AuthorizationRechecksV2(
            pre_retrieval="pass",
            pre_prompt="pass",
            delivery="not_run",
        ),
        budget=AuthorizationBudgetV2(
            selected_projection_count=1,
            serialized_projection_bytes=serialized_bytes,
            truncation_reasons=(),
        ),
    )


def _bridge() -> str:
    return canonical_sha256(
        {
            "context_snapshot_id": CONTEXT_SNAPSHOT_ID,
            "conversation_snapshot_sha256": CONVERSATION_SHA,
        }
    )


def _adapt(request, envelope, target, scopes):
    return adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
        request=request,
        envelope=envelope,
        authorization=_authorization(request, target, scopes),
        context_bridge_sha256=_bridge(),
    )


class SelfShadowAdapterTests(unittest.TestCase):
    def test_map_artifact_matches_code(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "specs"
            / "lifeswitch"
            / "self_s1_shadow_adapter_map_v1.yaml"
        )
        artifact = json.loads(path.read_text(encoding="utf-8"))
        normalized = {
            key: {
                field: list(value) if isinstance(value, tuple) else value
                for field, value in definition.items()
            }
            for key, definition in SELF_S1_SHADOW_ADAPTER_MAP_V1.items()
        }
        self.assertEqual(artifact["mappings"], normalized)
        self.assertFalse(artifact["database_reads"])
        self.assertFalse(artifact["prompt_influence"])
        self.assertFalse(artifact["runtime_registration"])

    def test_current_plan_filters_to_approved_fields(self):
        request = _request("What is my current plan?")
        section = _section(
            projection="current_plan",
            payload={
                "phase": "cut",
                "primary_goal": "Reduce body fat while preserving strength",
                "nutrition_targets": {"calories": 2000, "protein_g": 180},
                "training_targets": {"strength_sessions_per_week": 4},
            },
            sources=(
                "lifeswitch_agentic.plan_owner_state",
                "lifeswitch_agentic.plan_versions",
            ),
        )
        result = _adapt(
            request,
            _envelope(request, section, plan_source="agentic_active"),
            "plan.current.v1",
            ("plan:view",),
        )
        self.assertEqual(result.target_projection_id, "plan.current.v1")
        self.assertIsInstance(result.result, ProjectionEnvelopeV1)
        self.assertEqual(result.result.data["routine_goal_direction"], section.payload["primary_goal"])
        self.assertNotIn("full_plan_document", result.result.data)
        self.assertTrue(result.capabilities.read)
        self.assertFalse(result.capabilities.change_design)
        self.assertFalse(result.capabilities.experiment_consent)
        self.assertFalse(result.capabilities.write_consent)
        self.assertFalse(result.capabilities.proactive_follow_up)

    def test_nutrition_range_preserves_days_and_missing_coverage(self):
        request = _request("How have my macros been over the last 7 days?")
        section = _section(
            projection="nutrition_range",
            payload={
                "logged_days": 2,
                "calendar_days": 3,
                "averages_on_logged_days": {
                    "calories": 1975.0,
                    "protein_g": 190.0,
                    "carbs_g": 170.0,
                    "fat_g": 55.0,
                },
                "plan_targets": {"calories": 2000, "protein_g": 180},
                "daily_columns": ["date", "calories", "protein_g", "carbs_g", "fat_g"],
                "daily_rows": [
                    ["2026-07-30", 1950.0, 185.0, 165.0, 54.0],
                    ["2026-08-01", 2000.0, 195.0, 175.0, 56.0],
                ],
            },
            sources=(
                "lifeswitch_agentic.plan_owner_state",
                "lifeswitch_agentic.plan_versions",
                "lifeswitch_nutrition.nutrition_day",
                "lifeswitch_nutrition.nutrition_entry",
            ),
            start=dt.date(2026, 7, 30),
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section, plan_source="agentic_active"),
            "nutrition.range.v1",
            ("nutrition:view",),
        )
        self.assertEqual(result.result.status, "partial")
        self.assertEqual(result.result.data["coverage_counts"]["missing_days"], 1)
        self.assertEqual(result.result.data["daily_totals"][0]["carbohydrate_g"], 165.0)
        self.assertNotIn("plan_targets", result.result.data)

    def test_nutrition_day_becomes_one_day_range(self):
        request = _request("What are my macros today?")
        section = _section(
            projection="nutrition_day",
            payload={
                "plan_targets": {"calories": 2000},
                "daily": [
                    {
                        "date": "2026-08-01",
                        "calories": 2010.0,
                        "protein_g": 188.0,
                        "carbs_g": 180.0,
                        "fat_g": 58.0,
                    }
                ],
            },
            sources=("lifeswitch_nutrition.nutrition_day",),
            start=TODAY,
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "nutrition.range.v1",
            ("nutrition:view",),
        )
        self.assertEqual(result.result.status, "available")
        self.assertEqual(result.result.data["coverage_counts"]["calendar_days"], 1)

    def test_frequency_includes_only_nonconflicted_strength_rows(self):
        request = _request("What exercises have I done most over the last four weeks?")
        section = _section(
            projection="exercise_frequency",
            payload={
                "columns": [
                    "exercise_name",
                    "effective_role",
                    "set_count",
                    "session_count",
                    "first_day",
                    "last_day",
                    "role_conflict",
                ],
                "rows": [
                    ["Decline Bench Press", "strength", 15, 5, "2026-07-02", "2026-07-22", False],
                    ["Calf Push", "rehab", 56, 13, "2026-07-01", "2026-07-31", False],
                    ["Unknown Press", "strength", 3, 1, "2026-07-20", "2026-07-20", True],
                ],
            },
            sources=(
                "lifeswitch_training.training_session_current_v",
                "lifeswitch_training.training_set_log",
                "lifeswitch_training.training_set_effective_role_v1",
            ),
            start=dt.date(2026, 7, 5),
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "training.exercise_frequency.v1",
            ("training:view",),
        )
        self.assertEqual(
            result.result.data["exercises"],
            [{"exercise_name": "Decline Bench Press", "completed_exposure_count": 5}],
        )

    def test_progression_preserves_observations_without_inventing_trend(self):
        request = _request("Show my recent progress on Decline Bench Press")
        section = _section(
            projection="exercise_progression",
            payload={
                "exercise_query": "Decline Bench Press",
                "observations": [
                    {
                        "date": "2026-07-02",
                        "exercise": "Decline Bench Press",
                        "sets": 3,
                        "reps": 48,
                        "max_load": 216.0,
                        "volume": 10368.0,
                        "load_unit": "lb",
                    },
                    {
                        "date": "2026-07-22",
                        "exercise": "Decline Bench Press",
                        "sets": 3,
                        "reps": 52,
                        "max_load": 216.0,
                        "volume": 11232.0,
                        "load_unit": "lb",
                    },
                ],
            },
            sources=(
                "lifeswitch_training.training_session_current_v",
                "lifeswitch_training.training_set_log",
                "lifeswitch_training.training_set_effective_role_v1",
            ),
            start=dt.date(2026, 5, 9),
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "training.exercise_progression.v1",
            ("training:view",),
        )
        self.assertEqual(len(result.result.data["metric_values"]), 2)
        self.assertNotIn("trend", result.result.data)
        self.assertNotIn("estimated_1rm", result.result.data)

    def test_conditioning_day_drops_health_and_recovery_fields(self):
        request = _request("Show my training session today")
        section = _section(
            projection="training_session",
            payload={
                "date": "2026-08-01",
                "resistance_sessions": [],
                "conditioning_sessions": [
                    {
                        "name": "Bike",
                        "category": "conditioning",
                        "modality": "stationary bike",
                        "duration_min": 30.0,
                        "intensity": "moderate",
                        "distance": 8.0,
                        "distance_unit": "mi",
                        "heart_rate_avg": 120.0,
                        "recovery_impact": "low",
                    }
                ],
            },
            sources=("lifeswitch_training.conditioning_session_current_v",),
            start=TODAY,
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "conditioning.sessions_by_day.v1",
            ("training:view",),
        )
        serialized = json.dumps(result.result.data, sort_keys=True)
        self.assertNotIn("heart", serialized)
        self.assertNotIn("recovery", serialized)
        self.assertEqual(result.result.data["sessions"][0]["duration"], 30.0)

    def test_training_range_preserves_exact_days_and_bounded_session_counts(self):
        request = _request(
            "For each day from July 30 through August 1, show whether I "
            "completed strength training or conditioning."
        )
        section = _section(
            projection="training_range",
            payload={
                "columns": [
                    "date",
                    "strength_session_count",
                    "strength_set_count",
                    "strength_exercise_count",
                    "rehab_session_count",
                    "rehab_set_count",
                    "rehab_exercise_count",
                    "conditioning_session_count",
                    "conditioning_minutes",
                    "unknown_role_session_count",
                    "unknown_role_set_count",
                    "unknown_role_exercise_count",
                ],
                "rows": [
                    ["2026-07-30", 1, 12, 4, 0, 0, 0, 0, 0.0, 0, 0, 0],
                    ["2026-07-31", 0, 0, 0, 0, 0, 0, 1, 25.0, 0, 0, 0],
                    ["2026-08-01", 0, 0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0],
                ],
            },
            sources=(
                "lifeswitch_training.training_session_current_v",
                "lifeswitch_training.training_set_log",
                "lifeswitch_training.training_set_effective_role_v1",
                "lifeswitch_training.conditioning_session_current_v",
            ),
            start=dt.date(2026, 7, 30),
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "training.sessions_by_day.v1",
            ("training:view",),
        )
        self.assertEqual(result.result.status, "available")
        self.assertEqual(len(result.result.data["sessions"]), 2)
        self.assertEqual(
            result.result.data["requested_window"],
            {
                "start_local_date": "2026-07-30",
                "end_local_date": "2026-08-01",
            },
        )
        self.assertEqual(
            [item["routine_session_type"] for item in result.result.data["sessions"]],
            ["strength", "conditioning"],
        )
        serialized = json.dumps(result.result.data, default=str, sort_keys=True)
        self.assertNotIn("heart", serialized)
        self.assertNotIn("notes", serialized)

    def test_measurements_are_data_free_and_decision_blocked(self):
        request = _request("What is my recent weight?")
        section = _section(
            projection="measurements_summary",
            payload={"weight": {"latest": 205.0, "private_notes": "must not surface"}},
            sources=("public.lifeswitch_measurement_entries",),
            start=dt.date(2025, 8, 2),
            end=TODAY,
        )
        result = _adapt(
            request,
            _envelope(request, section),
            "measurements.core_summary.v1",
            ("measurements:view",),
        )
        self.assertEqual(result.result.status, "decision_blocked")
        self.assertIsNone(result.result.data)
        self.assertEqual(result.result.decision_blocks, ("D03_MEASUREMENT_COLLISIONS",))

    def test_unknown_plan_field_fails_closed(self):
        request = _request("What is my current plan?")
        section = _section(
            projection="current_plan",
            payload={"primary_goal": "Maintain", "coach_notes": "private"},
            sources=("lifeswitch_agentic.plan_versions",),
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "unapproved fields"):
            _adapt(
                request,
                _envelope(request, section, plan_source="agentic_active"),
                "plan.current.v1",
                ("plan:view",),
            )

    def test_legacy_plan_source_fails_closed(self):
        request = _request("What is my current plan?")
        section = _section(
            projection="current_plan",
            payload={"primary_goal": "Maintain"},
            sources=("lifeswitch_plan.plan_profile",),
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "legacy plan fallback"):
            _adapt(
                request,
                _envelope(request, section, plan_source="legacy_fallback"),
                "plan.current.v1",
                ("plan:view",),
            )

    def test_unapproved_source_relation_fails_closed(self):
        request = _request("What are my macros today?")
        section = _section(
            projection="nutrition_day",
            payload={"daily": []},
            sources=("public.secret_table",),
            start=TODAY,
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "source relation"):
            _adapt(
                request,
                _envelope(request, section),
                "nutrition.range.v1",
                ("nutrition:view",),
            )

    def test_context_bridge_mismatch_fails_closed(self):
        request = _request("What are my macros today?")
        section = _section(
            projection="nutrition_day",
            payload={"daily": []},
            sources=("lifeswitch_nutrition.nutrition_day",),
            start=TODAY,
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "context snapshot bridge"):
            adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
                request=request,
                envelope=_envelope(request, section),
                authorization=_authorization(
                    request, "nutrition.range.v1", ("nutrition:view",)
                ),
                context_bridge_sha256="f" * 64,
            )

    def test_scope_mismatch_fails_closed(self):
        request = _request("What is my current plan?")
        section = _section(
            projection="current_plan",
            payload={"primary_goal": "Maintain"},
            sources=("lifeswitch_agentic.plan_versions",),
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "scopes differ"):
            adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
                request=request,
                envelope=_envelope(request, section, plan_source="agentic_active"),
                authorization=_authorization(
                    request, "plan.current.v1", ("nutrition:view",)
                ),
                context_bridge_sha256=_bridge(),
            )

    def test_multiple_v1_sections_fail_closed(self):
        request = _request("How am I doing with my plan and macros?")
        first = _section(
            projection="current_plan",
            payload={"primary_goal": "Maintain"},
            sources=("lifeswitch_agentic.plan_versions",),
        )
        second = _section(
            projection="nutrition_range",
            payload={
                "logged_days": 0,
                "calendar_days": 1,
                "averages_on_logged_days": {
                    "calories": None,
                    "protein_g": None,
                    "carbs_g": None,
                    "fat_g": None,
                },
                "daily_columns": ["date", "calories", "protein_g", "carbs_g", "fat_g"],
                "daily_rows": [],
            },
            sources=("lifeswitch_nutrition.nutrition_day",),
            start=TODAY,
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "exactly one"):
            _adapt(
                request,
                _envelope(request, first, second, plan_source="agentic_active"),
                "plan.current.v1",
                ("plan:view",),
            )

    def test_unsupported_v1_projection_fails_closed(self):
        request = _request("How has my training been lately?")
        section = _section(
            projection="training_summary",
            payload={"training": {}},
            sources=("lifeswitch_training.training_session_current_v",),
            start=dt.date(2026, 7, 19),
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "not approved"):
            _adapt(
                request,
                _envelope(request, section),
                "training.sessions_by_day.v1",
                ("training:view",),
            )

    def test_progression_requires_one_resolved_exercise(self):
        request = _request("Show my recent progress on press")
        section = _section(
            projection="exercise_progression",
            payload={
                "exercise_query": "press",
                "observations": [
                    {
                        "date": "2026-07-10",
                        "exercise": "Chest Press",
                        "sets": 3,
                        "reps": 30,
                        "max_load": 100.0,
                        "volume": 3000.0,
                        "load_unit": "lb",
                    },
                    {
                        "date": "2026-07-12",
                        "exercise": "Decline Bench Press",
                        "sets": 3,
                        "reps": 30,
                        "max_load": 200.0,
                        "volume": 6000.0,
                        "load_unit": "lb",
                    },
                ],
            },
            sources=("lifeswitch_training.training_set_log",),
            start=dt.date(2026, 5, 9),
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowAdapterError, "exactly one exercise"):
            _adapt(
                request,
                _envelope(request, section),
                "training.exercise_progression.v1",
                ("training:view",),
            )

    def test_adapter_is_not_imported_by_live_response_modules(self):
        root = Path(__file__).resolve().parents[1]
        needle = "seebx.capabilities.coaching.shadow_adapter"
        live_paths = (
            root / "seebx" / "capabilities/conversation/router.py",
            root / "seebx" / "capabilities/conversation/composition.py",
            root / "seebx" / "capabilities/conversation/lifeswitch_composition.py",
            root / "rag_engine" / "response_lifeswitch_integration_v2.py",
        )
        for path in live_paths:
            if not path.exists():
                continue
            self.assertNotIn(needle, path.read_text(encoding="utf-8"), path.name)

    def test_authorization_contract_rejects_nonself_identity(self):
        request = _request("What is my current plan?")
        other = UUID("55555555-5555-4555-8555-555555555555")
        with self.assertRaises(ValidationError):
            _authorization(
                request,
                "plan.current.v1",
                ("plan:view",),
                actor=OWNER,
                subject=other,
                perspective="self",
            )


if __name__ == "__main__":
    unittest.main()
