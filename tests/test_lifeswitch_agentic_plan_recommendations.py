from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Mapping

from lifeswitch_agentic.plan_domain import PlanDocumentV1
from lifeswitch_agentic.plan_recommendations import (
    ModelEvidence,
    ModelPlanReview,
    ModelQuestion,
    ModelSuggestion,
    PlanRecommendationService,
    ProviderPlanReview,
    RecommendationFocus,
)


def plan_document() -> dict[str, Any]:
    return {
        "phase": "cut",
        "phase_label": "Cut to 16% body fat",
        "primary_goal": "Reduce body-fat percentage while maintaining strength.",
        "start_date": "2026-07-01",
        "review_date": "2026-07-15",
        "review_cadence": "weekly",
        "body_state": {"body_fat_percent": 20},
        "nutrition_targets": {
            "calorie_target": {"lower": 1800, "upper": 2100},
            "protein_grams_minimum": 160,
        },
        "training_targets": {"strength_sessions_per_week": 3},
        "conditioning_targets": {},
        "activity_targets": {"steps_minimum": 7000},
        "recovery_targets": {},
        "monitoring_rules": {"review_every_days": 7},
        "coach_notes": "",
    }


class FakeProvider:
    def __init__(self, output: ModelPlanReview) -> None:
        self.output = output
        self.last_editable_paths: tuple[str, ...] = ()
        self.last_observation_context: Mapping[str, Any] = {}
        self.last_ambiguity_routes: tuple[Mapping[str, str], ...] = ()

    async def review_plan(
        self,
        *,
        document: Mapping[str, Any],
        validation: Mapping[str, Any],
        observation_context: Mapping[str, Any],
        ambiguity_routes: tuple[Mapping[str, str], ...],
        focus: RecommendationFocus,
        user_request: str,
        editable_paths: tuple[str, ...],
    ) -> ProviderPlanReview:
        self.last_editable_paths = editable_paths
        self.last_observation_context = observation_context
        self.last_ambiguity_routes = ambiguity_routes
        return ProviderPlanReview(
            output=self.output,
            provider="fake",
            model="fake-plan-model",
            response_id="resp_test",
        )


def suggestion(
    field_path: str,
    proposed_value: Any,
    *,
    evidence_path: str = "/primary_goal",
) -> ModelSuggestion:
    return ModelSuggestion(
        field_path=field_path,
        proposed_value=proposed_value,
        rationale="This would make the target boundary explicit.",
        evidence=[
            ModelEvidence(
                field_path=evidence_path,
                explanation="The stated goal is the relevant Plan evidence.",
            )
        ],
        confidence="medium",
        data_sufficiency="limited",
    )


class PlanRecommendationServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_filters_untrusted_model_output_and_never_writes(self) -> None:
        provider = FakeProvider(
            ModelPlanReview(
                summary="The draft has a clear goal but needs a few boundaries.",
                questions=[
                    ModelQuestion(
                        field_path="/conditioning_targets",
                        question="What conditioning schedule is realistic?",
                        why_needed="The section is currently empty.",
                    ),
                    ModelQuestion(
                        field_path="/not-a-plan-field",
                        question="Invalid question",
                        why_needed="This must be filtered.",
                    ),
                ],
                suggestions=[
                    suggestion(
                        "/nutrition_targets/calorie_target/lower",
                        1900,
                        evidence_path="/context/nutrition/decision_rule/triggered",
                    ),
                    suggestion("/primary_goal", 123),
                    suggestion("/unknown/path", "not allowed"),
                    suggestion(
                        "/review_cadence",
                        "every 14 days",
                        evidence_path="/unknown/evidence",
                    ),
                ],
            )
        )
        service = PlanRecommendationService(provider)
        document = PlanDocumentV1.from_mapping(plan_document())
        context = {
            "nutrition": {"decision_rule": {"triggered": True}},
            "writes_performed": False,
        }

        result = await service.review_draft(
            document=document,
            focus="whole_plan",
            user_request="Review the target boundaries.",
            observation_context=context,
        )

        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(len(result["suggestions"]), 1)
        accepted = result["suggestions"][0]
        self.assertEqual(
            accepted["field_path"],
            "/nutrition_targets/calorie_target/lower",
        )
        self.assertEqual(accepted["current_value"], 1800)
        self.assertEqual(accepted["proposed_value"], 1900)
        self.assertTrue(accepted["evidence"][0]["observed_value"])
        self.assertFalse(result["provenance"]["writes_performed"])
        self.assertEqual(result["provenance"]["draft_sha256"], document.sha256())
        self.assertNotIn("/schema_version", provider.last_editable_paths)

    async def test_unchanged_and_invalid_date_suggestions_are_removed(self) -> None:
        provider = FakeProvider(
            ModelPlanReview(
                summary="No safe edit is available.",
                questions=[],
                suggestions=[
                    suggestion("/phase", "cut"),
                    suggestion("/review_date", "not-a-date"),
                ],
            )
        )
        result = await PlanRecommendationService(provider).review_draft(
            document=PlanDocumentV1.from_mapping(plan_document()),
            focus="schedule",
            user_request="",
        )
        self.assertEqual(result["suggestions"], [])

    async def test_canonical_context_is_valid_evidence_and_explicit_request_limits_focus(self) -> None:
        provider = FakeProvider(
            ModelPlanReview(
                summary="The calorie range can be evaluated from logged intake.",
                questions=[
                    ModelQuestion(
                        field_path="/training_targets",
                        question="How many strength sessions are planned?",
                        why_needed="This is unrelated and must be filtered.",
                    )
                ],
                suggestions=[
                    suggestion(
                        "/nutrition_targets/calorie_target/lower",
                        1850,
                        evidence_path="/context/nutrition/decision_rule/triggered",
                    ),
                    suggestion("/training_targets/strength_sessions_per_week", 4),
                ],
            )
        )
        context = {
            "nutrition": {
                "calories": {
                    "adherence": {"percent_of_observed_days": 82.0},
                },
                "decision_rule": {"triggered": True},
            },
            "writes_performed": False,
        }
        result = await PlanRecommendationService(provider).review_draft(
            document=PlanDocumentV1.from_mapping(plan_document()),
            focus="whole_plan",
            user_request="Are my calorie goals okay?",
            observation_context=context,
        )
        self.assertEqual(result["questions"], [])
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertTrue(result["suggestions"][0]["evidence"][0]["observed_value"])
        self.assertEqual(result["provenance"]["effective_focus"], "nutrition_targets")
        self.assertTrue(all(path.startswith("/nutrition_targets/") for path in provider.last_editable_paths))
        self.assertEqual(provider.last_observation_context, context)

    async def test_rejects_random_protein_reduction_when_current_target_is_reliably_exceeded(self) -> None:
        raw_document = plan_document()
        raw_document["nutrition_targets"] = {"protein_g": 180}
        provider = FakeProvider(
            ModelPlanReview(
                summary="The current protein target is being met reliably.",
                questions=[],
                suggestions=[
                    suggestion(
                        "/nutrition_targets/protein_g",
                        170,
                        evidence_path="/context/nutrition/protein/adherence/percent_of_observed_days",
                    )
                ],
            )
        )
        context = {
            "nutrition": {
                "data_sufficiency": "sufficient",
                "protein": {
                    "average_on_logged_days": 213.6,
                    "adherence": {
                        "status": "evaluable",
                        "percent_of_observed_days": 90.5,
                    },
                },
            },
            "writes_performed": False,
        }
        result = await PlanRecommendationService(provider).review_draft(
            document=PlanDocumentV1.from_mapping(raw_document),
            focus="nutrition_targets",
            user_request="Are my protein goals okay?",
            observation_context=context,
        )
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["policy"]["rejected_suggestion_count"], 1)
        self.assertIn(
            "protein_reduction_contradicts_successful_adherence",
            result["policy"]["rejected_reason_codes"],
        )
        route_codes = {item["code"] for item in provider.last_ambiguity_routes}
        self.assertIn("external_knowledge_may_be_needed", route_codes)


class PlanRecommendationSourceBoundaryTest(unittest.TestCase):
    def test_provider_has_no_data_tools_or_memory_dependencies(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "lifeswitch_agentic"
            / "plan_recommendations.py"
        ).read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "import asyncpg",
            "plan_repository",
            "rag_engine",
            "qdrant",
            "memory_v1",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("store=False", source)
        self.assertIn('"writes_performed": False', source)


if __name__ == "__main__":
    unittest.main()
