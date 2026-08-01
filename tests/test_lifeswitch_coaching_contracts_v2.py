from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from pydantic import ValidationError

from rag_engine.lifeswitch_coaching_contracts_v2 import (
    AuthorizationBindingV2,
    AuthorizationBudgetV2,
    AuthorizationRechecksV2,
    AuthorizationSnapshotV2,
    CoachingCapabilitiesV2,
    CoachingContextBindingV2,
    EffectiveGrantV2,
    ProjectionAbsoluteWindowV1,
    ProjectionAuthorizationDecisionV2,
    ProjectionEnvelopeV1,
    ProjectionErrorV1,
    ProjectionFieldStateV1,
    ProjectionProvenanceV1,
    ProjectionSelectionResultV1,
    ProjectionSourceRefV1,
    SelectedProjectionDecisionV1,
    StructuredCoachingContextV2,
    UnavailableDomainV2,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
)


ACTOR = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
REQUEST = "33333333-3333-4333-8333-333333333333"
THREAD = "44444444-4444-4444-8444-444444444444"
SNAPSHOT = "55555555-5555-4555-8555-555555555555"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def replace(model, **changes):
    values = model.model_dump(mode="python")
    values.update(changes)
    return type(model).model_validate(values)


def authorization_binding(*, delegated: bool) -> AuthorizationBindingV2:
    return AuthorizationBindingV2(
        request_id=REQUEST,
        thread_id=THREAD,
        context_snapshot_id=SNAPSHOT,
        actor_user_id=ACTOR,
        subject_user_id=SUBJECT if delegated else ACTOR,
        subject_selection="explicit",
        perspective="delegated" if delegated else "self",
        account_timezone="America/Chicago",
        evaluated_at="2026-08-01T12:00:00Z",
        transaction_isolation="repeatable_read",
        transaction_access="read_only",
        transaction_snapshot_digest=DIGEST_A,
    )


def authorization(*, delegated: bool = False) -> AuthorizationSnapshotV2:
    return AuthorizationSnapshotV2(
        schema_id="lifeswitch.authorization_snapshot",
        schema_version=2,
        binding=authorization_binding(delegated=delegated),
        decision="allow",
        authorization_epoch="epoch-17",
        sensitivity_ceiling="S1" if delegated else "S3",
        relationship_basis="accepted_relationship" if delegated else "self",
        relationship_binding_digest=DIGEST_B if delegated else None,
        effective_grants=(
            (
                EffectiveGrantV2(
                    scope="nutrition:view",
                    permission_level="view",
                    grant_digest=DIGEST_A,
                ),
            )
            if delegated
            else ()
        ),
        projection_decisions=(
            ProjectionAuthorizationDecisionV2(
                projection_id="nutrition.daily.v1",
                outcome="authorized",
                executed=True,
                required_scopes=("nutrition:view",),
                sensitivity_ceiling="S1",
                field_policy_version="lifeswitch.sensitivity_field_policy@1",
                internal_reason_code=None,
            ),
        ),
        rechecks=AuthorizationRechecksV2(
            pre_retrieval="pass",
            pre_prompt="pass",
            delivery="not_run",
        ),
        budget=AuthorizationBudgetV2(
            projection_count_limit=6,
            context_byte_limit=32768,
            selected_projection_count=1,
            serialized_projection_bytes=120,
            truncation_reasons=(),
        ),
    )


def projection() -> ProjectionEnvelopeV1:
    return ProjectionEnvelopeV1(
        projection_id="nutrition.daily.v1",
        projection_schema_version=1,
        status="available",
        executed=True,
        sensitivity="S1",
        data={
            "local_date": "2026-07-31",
            "calories": 1910,
            "protein_g": 203.4,
            "day_state": "present",
        },
        field_states=(
            ProjectionFieldStateV1(field="calories", state="present"),
            ProjectionFieldStateV1(field="protein_g", state="present"),
        ),
        window=ProjectionAbsoluteWindowV1(
            start_local_date="2026-07-31",
            end_local_date="2026-07-31",
            timezone="America/Chicago",
            boundary="inclusive_local_dates",
            relative_expression=None,
        ),
        provenance=ProjectionProvenanceV1(
            source_refs=(
                ProjectionSourceRefV1(
                    source_id="nutrition_day",
                    gateway_id="lifeswitch_chat.read_nutrition_day_v2",
                    gateway_version=2,
                    field_policy_version=1,
                ),
            ),
            snapshot_id="pg-snapshot-42",
            retrieved_at_utc="2026-08-01T12:00:01Z",
        ),
        serialized_bytes=120,
        fallback_used=False,
    )


def selection() -> ProjectionSelectionResultV1:
    return ProjectionSelectionResultV1(
        schema_id="lifeswitch.projection_selection",
        schema_version=1,
        requested_count=1,
        selected_count=1,
        max_selected_count=6,
        decisions=(
            SelectedProjectionDecisionV1(
                projection_id="nutrition.daily.v1",
                status="selected",
                executed=True,
                execution_ordinal=1,
            ),
        ),
        truncated=False,
        truncation=None,
    )


def context(*, delegated: bool = False) -> StructuredCoachingContextV2:
    auth = authorization(delegated=delegated)
    return StructuredCoachingContextV2(
        schema_id="lifeswitch.structured_coaching_context",
        schema_version=2,
        binding=CoachingContextBindingV2(
            request_id=REQUEST,
            thread_id=THREAD,
            context_snapshot_id=SNAPSHOT,
            actor_user_id=ACTOR,
            subject_user_id=SUBJECT if delegated else ACTOR,
            account_timezone="America/Chicago",
            as_of_utc="2026-08-01T12:00:00Z",
            as_of_local_date="2026-08-01",
            authorization_epoch="epoch-17",
            transaction_isolation="repeatable_read",
            transaction_access="read_only",
        ),
        authorization=auth,
        selection=selection(),
        projections=(projection(),),
        unavailable_domains=(
            UnavailableDomainV2(domain="measurements", status="unavailable"),
        ),
        serialized_projection_bytes=120,
        max_projection_bytes=32768,
        context_truncation=None,
        capabilities=CoachingCapabilitiesV2(
            read=True,
            change_design=False,
            experiment_consent=False,
            write_consent=False,
            proactive_follow_up=False,
        ),
    )


class LifeSwitchCoachingContractsV2Tests(unittest.TestCase):
    def test_valid_self_context_is_strict_and_frozen(self) -> None:
        value = context()
        self.assertEqual(value.authorization.binding.perspective, "self")
        with self.assertRaises(ValidationError):
            StructuredCoachingContextV2.model_validate(
                {**value.model_dump(mode="python"), "unexpected": True}
            )
        with self.assertRaises(ValidationError):
            value.serialized_projection_bytes = 0

    def test_valid_delegated_s1_context(self) -> None:
        value = context(delegated=True)
        self.assertEqual(value.authorization.sensitivity_ceiling, "S1")
        self.assertEqual(value.authorization.effective_grants[0].scope, "nutrition:view")

    def test_delegated_context_rejects_s2_projection(self) -> None:
        value = context(delegated=True)
        elevated = replace(projection(), sensitivity="S2")
        with self.assertRaisesRegex(ValidationError, "sensitivity ceiling"):
            replace(value, projections=(elevated,))

    def test_delegated_context_requires_relationship_digest(self) -> None:
        auth = authorization(delegated=True)
        with self.assertRaisesRegex(ValidationError, "bound relationship"):
            replace(auth, relationship_binding_digest=None)

    def test_self_context_rejects_delegated_grants(self) -> None:
        auth = authorization()
        grant = EffectiveGrantV2(
            scope="nutrition:view",
            permission_level="view",
            grant_digest=DIGEST_A,
        )
        with self.assertRaisesRegex(ValidationError, "self access"):
            replace(auth, effective_grants=(grant,))

    def test_context_rejects_binding_mismatch(self) -> None:
        value = context()
        changed = replace(value.binding, request_id=THREAD)
        with self.assertRaisesRegex(ValidationError, "request binding mismatch"):
            replace(value, binding=changed)

    def test_context_rejects_failed_pre_prompt_recheck(self) -> None:
        value = context()
        checks = replace(value.authorization.rechecks, pre_prompt="fail")
        auth = replace(value.authorization, rechecks=checks)
        with self.assertRaisesRegex(ValidationError, "pre-prompt"):
            replace(value, authorization=auth)

    def test_selection_rejects_noncontiguous_ordinals(self) -> None:
        first = SelectedProjectionDecisionV1(
            projection_id="nutrition.daily.v1",
            execution_ordinal=2,
        )
        with self.assertRaisesRegex(ValidationError, "contiguous"):
            ProjectionSelectionResultV1(
                requested_count=1,
                selected_count=1,
                decisions=(first,),
                truncated=False,
            )

    def test_selection_rejects_more_than_six_selected(self) -> None:
        with self.assertRaises(ValidationError):
            SelectedProjectionDecisionV1(
                projection_id="conditioning.sessions_by_day.v1",
                execution_ordinal=7,
            )

    def test_context_rejects_over_budget_bytes(self) -> None:
        value = context()
        with self.assertRaises(ValidationError):
            replace(value, serialized_projection_bytes=32769)

    def test_projection_rejects_raw_identifier(self) -> None:
        with self.assertRaisesRegex(ValidationError, "raw identifier"):
            replace(projection(), data={"subject_user_id": SUBJECT})

    def test_projection_rejects_private_content_field(self) -> None:
        with self.assertRaisesRegex(ValidationError, "forbidden field"):
            replace(projection(), data={"exercise_notes": "private text"})

    def test_subject_authorization_projection_is_content_free(self) -> None:
        base = projection()
        with self.assertRaisesRegex(ValidationError, "content-free"):
            replace(
                base,
                projection_id="people.subject_authorization.v1",
                sensitivity="S0",
                data={"name": "Kelly"},
            )

    def test_projection_rejects_non_json_number(self) -> None:
        with self.assertRaises(ValueError):
            replace(projection(), data={"calories": math.nan})

    def test_decision_blocked_projection_requires_known_block(self) -> None:
        base = projection()
        with self.assertRaisesRegex(ValidationError, "block identifier"):
            replace(base, status="decision_blocked", data=None, decision_blocks=())
        blocked = replace(
            base,
            status="decision_blocked",
            data=None,
            decision_blocks=("D06_FORECAST_METHOD",),
        )
        self.assertEqual(blocked.decision_blocks, ("D06_FORECAST_METHOD",))

    def test_field_state_rejects_unknown_lifecycle(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectionFieldStateV1(field="calories", state="unknown")

    def test_window_rejects_relative_or_reversed_dates(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectionAbsoluteWindowV1(
                start_local_date="2026-08-01",
                end_local_date="2026-07-01",
                timezone="America/Chicago",
            )
        with self.assertRaises(ValidationError):
            ProjectionAbsoluteWindowV1(
                start_local_date="2026-07-01",
                end_local_date="2026-08-01",
                timezone="America/Chicago",
                relative_expression="last month",
            )

    def test_projection_error_is_fail_closed_without_fallback(self) -> None:
        error = ProjectionErrorV1(
            projection_id="nutrition.daily.v1",
            error_code="malformed_projection_payload",
        )
        self.assertTrue(error.fail_closed)
        self.assertFalse(error.fallback_used)
        with self.assertRaises(ValidationError):
            replace(error, fallback_used=True)

    def test_contract_versions_are_literal(self) -> None:
        with self.assertRaises(ValidationError):
            replace(context(), schema_version=3)
        with self.assertRaises(ValidationError):
            replace(selection(), schema_version=2)

    def test_canonical_hash_is_order_independent(self) -> None:
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            canonical_json_bytes({"a": 1, "b": 2}),
        )
        self.assertEqual(
            canonical_sha256({"b": 2, "a": 1}),
            canonical_sha256({"a": 1, "b": 2}),
        )

    def test_manifest_hashes_match_frozen_artifacts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "specs/lifeswitch/coaching_contract_manifest_v2.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_id"], "lifeswitch.coaching_contracts.v2")
        self.assertEqual(manifest["starting_commit"], "7ef10be1d662bc6ce2330c7db2349c285a68fc50")
        for artifact in manifest["artifacts"]:
            path = root / artifact["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(file_sha256(path), artifact["sha256"])


if __name__ == "__main__":
    unittest.main()
