from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from seebx.capabilities.coaching.contracts import canonical_sha256
from seebx.capabilities.coaching.shadow_runner import (
    LifeSwitchSelfShadowRunV1,
    LifeSwitchSelfShadowRunnerError,
    SELF_S1_SHADOW_RUNNER_CONTRACT,
    SelfShadowExecutionAuthorityV1,
    run_lifeswitch_self_s1_shadow_v2,
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
CONTEXT_ID = "44444444-4444-4444-8444-444444444444"
CONVERSATION_SHA = "a" * 64
TRANSACTION_SHA = "b" * 64
NOW = dt.datetime(2026, 8, 1, 18, 30, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 8, 1)


def _request(query: str = "What is my current plan?") -> TrustedLifeSwitchContextRequestV1:
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
    projection: str = "current_plan",
    payload: dict | None = None,
    sources: tuple[str, ...] = ("lifeswitch_plan.plan_profile",),
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> LifeSwitchContextSectionV1:
    window = None
    if start is not None or end is not None:
        window = LifeSwitchDataWindowV1(
            start_date=start or end,
            end_date=end or start,
        )
    return LifeSwitchContextSectionV1.create(
        projection=projection,
        status="AVAILABLE",
        window=window,
        record_count=1,
        source_relations=sources,
        payload=payload if payload is not None else {"primary_goal": "Maintain"},
    )


def _envelope(request, *sections, plan_source="canonical_plan"):
    return create_lifeswitch_context_envelope_v1(
        request=request,
        plan_source=plan_source,
        as_of_local_date=TODAY,
        sections=tuple(sections),
        generated_at=NOW,
    )


def _authority(**overrides) -> SelfShadowExecutionAuthorityV1:
    values = {
        "context_snapshot_id": CONTEXT_ID,
        "transaction_snapshot_digest": TRANSACTION_SHA,
        "authorization_epoch": "self-owner-epoch-1",
        "evaluated_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    values.update(overrides)
    return SelfShadowExecutionAuthorityV1(**values)


def _run(query: str = "What is my current plan?") -> LifeSwitchSelfShadowRunV1:
    request = _request(query)
    section = _section()
    return run_lifeswitch_self_s1_shadow_v2(
        request=request,
        envelope=_envelope(request, section),
        authority=_authority(),
    )


class LifeSwitchSelfShadowRunnerV2Tests(unittest.TestCase):
    def test_plan_run_builds_exact_context_without_exposure(self):
        result = _run()
        self.assertEqual(result.contract_version, SELF_S1_SHADOW_RUNNER_CONTRACT)
        self.assertEqual(result.inspection.target_projection_id, "plan.current.v1")
        self.assertEqual(result.inspection.outcome, "selected")
        self.assertFalse(result.inspection.prompt_influence)
        self.assertFalse(result.inspection.model_exposure)
        self.assertFalse(result.inspection.persistence)
        self.assertEqual(result.inspection.additional_database_reads, 0)
        self.assertEqual(result.structured_context.selection.selected_count, 1)
        self.assertEqual(
            result.structured_context.projections,
            (result.adaptation.result,),
        )

    def test_authorization_is_self_owned_and_exactly_byte_bound(self):
        result = _run()
        authorization = result.structured_context.authorization
        self.assertEqual(authorization.binding.perspective, "self")
        self.assertEqual(
            authorization.binding.actor_user_id,
            authorization.binding.subject_user_id,
        )
        self.assertEqual(authorization.relationship_basis, "self")
        self.assertEqual(authorization.effective_grants, ())
        self.assertEqual(
            authorization.budget.serialized_projection_bytes,
            result.adaptation.serialized_projection_bytes,
        )
        self.assertGreater(authorization.budget.serialized_projection_bytes, 0)
        self.assertEqual(authorization.rechecks.delivery, "not_run")

    def test_context_and_run_hashes_are_exact_and_deterministic(self):
        first = _run()
        second = _run()
        self.assertEqual(first.run_sha256, second.run_sha256)
        self.assertEqual(
            first.structured_context_sha256,
            canonical_sha256(first.structured_context),
        )
        self.assertEqual(
            first.authorization_snapshot_sha256,
            canonical_sha256(first.structured_context.authorization),
        )

    def test_tampered_run_hash_is_rejected(self):
        result = _run()
        payload = result.model_dump(mode="json")
        payload["run_sha256"] = "f" * 64
        with self.assertRaises(ValidationError):
            LifeSwitchSelfShadowRunV1.model_validate(payload)

    def test_inspection_is_content_free(self):
        result = _run()
        serialized = result.inspection.model_dump_json()
        self.assertNotIn(str(OWNER), serialized)
        self.assertNotIn(str(THREAD), serialized)
        self.assertNotIn("Maintain", serialized)
        self.assertNotIn("America/Chicago", serialized)
        self.assertNotIn("primary_goal", serialized)

    def test_repr_hides_structured_user_data(self):
        rendered = repr(_run())
        self.assertNotIn("Maintain", rendered)
        self.assertNotIn(str(OWNER), rendered)

    def test_nutrition_run_uses_one_day_range_and_exact_scope(self):
        request = _request("What are my macros today?")
        section = _section(
            projection="nutrition_day",
            payload={
                "plan_targets": {"calories": 2000, "protein_g": 180},
                "daily": [
                    {
                        "date": "2026-08-01",
                        "calories": 1950.0,
                        "protein_g": 190.0,
                        "carbs_g": 175.0,
                        "fat_g": 55.0,
                    }
                ],
            },
            sources=("lifeswitch_nutrition.nutrition_day",),
            start=TODAY,
            end=TODAY,
        )
        result = run_lifeswitch_self_s1_shadow_v2(
            request=request,
            envelope=_envelope(request, section, plan_source="not_requested"),
            authority=_authority(),
        )
        decision = result.structured_context.authorization.projection_decisions[0]
        self.assertEqual(result.inspection.target_projection_id, "nutrition.range.v1")
        self.assertEqual(decision.required_scopes, ("nutrition:view",))
        self.assertEqual(result.adaptation.result.window.start_local_date, "2026-08-01")

    def test_measurement_collision_remains_decision_blocked_and_data_free(self):
        request = _request("What is my recent weight?")
        section = _section(
            projection="measurements_summary",
            payload={"weight": {"latest": 205.0}},
            sources=("public.lifeswitch_measurement_entries",),
            start=dt.date(2025, 8, 2),
            end=TODAY,
        )
        result = run_lifeswitch_self_s1_shadow_v2(
            request=request,
            envelope=_envelope(request, section, plan_source="not_requested"),
            authority=_authority(),
        )
        self.assertEqual(result.inspection.target_status, "decision_blocked")
        self.assertEqual(
            result.inspection.decision_blocks,
            ("D03_MEASUREMENT_COLLISIONS",),
        )
        self.assertIsNone(result.adaptation.result.data)

    def test_off_context_is_not_run(self):
        request = _request("Who won a television show in 2015?")
        envelope = _envelope(request, plan_source="not_requested")
        self.assertEqual(envelope.status, "OFF")
        with self.assertRaisesRegex(LifeSwitchSelfShadowRunnerError, "selected"):
            run_lifeswitch_self_s1_shadow_v2(
                request=request,
                envelope=envelope,
                authority=_authority(),
            )

    def test_multiple_sections_fail_closed(self):
        request = _request("Compare my plan with my macros today")
        first = _section()
        second = _section(
            projection="nutrition_day",
            payload={"daily": []},
            sources=("lifeswitch_nutrition.nutrition_day",),
            start=TODAY,
            end=TODAY,
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowRunnerError, "exactly one"):
            run_lifeswitch_self_s1_shadow_v2(
                request=request,
                envelope=_envelope(request, first, second),
                authority=_authority(),
            )

    def test_unsupported_projection_fails_closed(self):
        request = _request("How is my training going?")
        section = _section(
            projection="training_summary",
            payload={"training": {}},
            sources=("lifeswitch_training.training_session_current_v",),
        )
        with self.assertRaisesRegex(LifeSwitchSelfShadowRunnerError, "not approved"):
            run_lifeswitch_self_s1_shadow_v2(
                request=request,
                envelope=_envelope(request, section, plan_source="not_requested"),
                authority=_authority(),
            )

    def test_noncanonical_context_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            _authority(context_snapshot_id="not-a-uuid")

    def test_invalid_transaction_digest_is_rejected(self):
        with self.assertRaises(ValidationError):
            _authority(transaction_snapshot_digest="not-a-digest")

    def test_naive_evaluation_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            _authority(evaluated_at="2026-08-01T18:30:00")

    def test_runner_is_not_imported_by_live_response_modules(self):
        root = Path(__file__).resolve().parents[1]
        needle = "seebx.capabilities.coaching.shadow_runner"
        live_paths = (
            root / "seebx" / "capabilities/conversation/router.py",
            root / "seebx" / "capabilities/conversation/lifeswitch_composition.py",
            root / "rag_engine" / "lifeswitch_response_context_provider_v1.py",
            root / "seebx" / "capabilities" / "conversation" / "lifeswitch_inspection.py",
        )
        for path in live_paths:
            if not path.exists():
                continue
            self.assertNotIn(needle, path.read_text(encoding="utf-8"), path.name)

    def test_runner_has_no_io_or_provider_imports(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "seebx"
            / "capabilities"
            / "coaching"
            / "shadow_runner.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "import asyncpg",
            "import requests",
            "import httpx",
            "import socket",
            "import subprocess",
            "import openai",
            "qdrant_client",
            "render_lifeswitch_context_v1",
        ):
            self.assertNotIn(forbidden, source)

    def test_machine_readable_contract_matches_runner(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "specs"
            / "lifeswitch"
            / "self_s1_shadow_runner_v1.yaml"
        )
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["contract_version"], SELF_S1_SHADOW_RUNNER_CONTRACT)
        self.assertEqual(artifact["projection_count"], 1)
        self.assertEqual(artifact["perspective"], "self")
        self.assertFalse(artifact["effects"]["prompt_influence"])
        self.assertFalse(artifact["effects"]["model_exposure"])
        self.assertFalse(artifact["effects"]["persistence"])


if __name__ == "__main__":
    unittest.main()
