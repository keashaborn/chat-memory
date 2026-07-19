"""Bounded LifeSwitch planning, analysis, and problem-solving domain services."""

from .plan_domain import (
    ActivationDecision,
    PlanChange,
    PlanDocumentV1,
    PlanDomainError,
    PlanValidationIssue,
    PLAN_VALIDATION_VERSION,
    RevisionState,
    RevisionTrigger,
    assert_revision_transition,
    diff_plan_documents,
    plan_validation_result,
    validate_activation,
    validate_plan_document,
    validate_revision_base,
)

__all__ = [
    "ActivationDecision",
    "PlanChange",
    "PlanDocumentV1",
    "PlanDomainError",
    "PlanValidationIssue",
    "PLAN_VALIDATION_VERSION",
    "RevisionState",
    "RevisionTrigger",
    "assert_revision_transition",
    "diff_plan_documents",
    "plan_validation_result",
    "validate_activation",
    "validate_plan_document",
    "validate_revision_base",
]
