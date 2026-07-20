from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SuggestionPolicyDecision:
    allowed: bool
    reason_code: str | None = None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_ambiguity_routes(
    document: Mapping[str, Any],
    observation_context: Mapping[str, Any],
    user_request: str,
) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    nutrition = _mapping(observation_context.get("nutrition"))
    calorie_adherence = _mapping(_mapping(nutrition.get("calories")).get("adherence"))
    if calorie_adherence.get("reason") == "point_target_has_no_acceptable_range":
        routes.append(
            {
                "code": "calorie_bounds_missing",
                "route": "ask_or_set_bounds_in_draft",
                "detail": "A point calorie target cannot be scored as adherence.",
            }
        )
    if nutrition.get("data_sufficiency") in {"insufficient", "limited"}:
        routes.append(
            {
                "code": "nutrition_observations_limited",
                "route": "collect_more_data",
                "detail": "Do not infer intervention effectiveness from limited logged observations.",
            }
        )
    training = _mapping(observation_context.get("training"))
    strength = _mapping(training.get("strength_adherence"))
    if strength.get("rehab_exclusion_supported") is False:
        routes.append(
            {
                "code": "rehab_not_classified",
                "route": "verify_data_structure",
                "detail": "Do not interpret all resistance sessions as strength sessions.",
            }
        )
    request = user_request.casefold()
    if any(
        term in request
        for term in ("appropriate", "optimal", "safe", "research", "evidence", "okay", " ok")
    ):
        routes.append(
            {
                "code": "external_knowledge_may_be_needed",
                "route": "evaluate_observed_response_then_research",
                "detail": "Use outcome data first; external claims require the restricted research gateway.",
            }
        )
    return routes


_PROTEIN_TARGET_PATHS = {
    "/nutrition_targets/protein_g",
    "/nutrition_targets/protein",
    "/nutrition_targets/protein_minimum_g",
    "/nutrition_targets/protein_grams_minimum",
    "/nutrition_targets/protein_target/minimum_g",
}
_CALORIE_NOMINAL_PATHS = {
    "/nutrition_targets/calories",
    "/nutrition_targets/target_kcal",
    "/nutrition_targets/calorie_target/nominal_kcal",
}


def _has_decision_basis(evidence_paths: tuple[str, ...]) -> bool:
    return any(
        marker in path
        for path in evidence_paths
        for marker in ("/decision_rule", "/recommendation_signal", "/research/")
    )


def evaluate_suggestion(
    *,
    field_path: str,
    current_value: Any,
    proposed_value: Any,
    evidence_paths: tuple[str, ...],
    observation_context: Mapping[str, Any],
) -> SuggestionPolicyDecision:
    current_number = _number(current_value)
    proposed_number = _number(proposed_value)
    if current_number is None or proposed_number is None or current_number == proposed_number:
        return SuggestionPolicyDecision(True)

    nutrition = _mapping(observation_context.get("nutrition"))
    if field_path in _PROTEIN_TARGET_PATHS and proposed_number < current_number:
        protein = _mapping(nutrition.get("protein"))
        adherence = _mapping(protein.get("adherence"))
        average = _number(protein.get("average_on_logged_days"))
        percent = _number(adherence.get("percent_of_observed_days"))
        if (
            adherence.get("status") == "evaluable"
            and average is not None
            and percent is not None
            and average >= current_number
            and percent >= 85
        ):
            return SuggestionPolicyDecision(
                False, "protein_reduction_contradicts_successful_adherence"
            )

    if field_path in _CALORIE_NOMINAL_PATHS and proposed_number < current_number:
        weekly = _mapping(nutrition.get("weekly_evaluation"))
        if weekly.get("status") == "evaluable" and weekly.get(
            "calorie_average_within_range"
        ) is True:
            return SuggestionPolicyDecision(
                False, "calorie_reduction_contradicts_configured_weekly_rule"
            )

    if field_path.startswith("/nutrition_targets/") and not _has_decision_basis(
        evidence_paths
    ):
        return SuggestionPolicyDecision(
            False, "numeric_nutrition_change_has_no_decision_or_research_basis"
        )
    return SuggestionPolicyDecision(True)
