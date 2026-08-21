from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from uuid import UUID

from seebx.capabilities.coaching.shadow_observer import (
    LifeSwitchSelfShadowObserverV2,
    SELF_S1_SHADOW_AUTHORIZATION_EPOCH,
    SELF_S1_SHADOW_OBSERVER_CONTRACT,
)
from seebx.capabilities.plans.data_plan import create_lifeswitch_data_plan_v1
from seebx.capabilities.plans.domain_context import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
THREAD = UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = UUID("44444444-4444-4444-8444-444444444444")
NOW = dt.datetime(2026, 8, 1, 19, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 8, 1)


def _source():
    query = "What is my current plan?"
    request = TrustedLifeSwitchContextRequestV1.create(
        request_id="33333333-3333-4333-8333-333333333333",
        authenticated_actor_user_id=OWNER,
        owner_user_id=OWNER,
        thread_id=THREAD,
        conversation_snapshot_sha256="a" * 64,
        owner_timezone="America/Chicago",
        query=query,
        data_plan=create_lifeswitch_data_plan_v1(query, today=TODAY),
    )
    section = LifeSwitchContextSectionV1.create(
        projection="current_plan",
        status="AVAILABLE",
        window=None,
        record_count=1,
        source_relations=("lifeswitch_plan.plan_profile",),
        payload={"primary_goal": "Maintain"},
    )
    envelope = create_lifeswitch_context_envelope_v1(
        request=request,
        plan_source="canonical_plan",
        as_of_local_date=TODAY,
        sections=(section,),
        generated_at=NOW,
    )
    return request, envelope


class RecordingSink:
    def __init__(self) -> None:
        self.inspections = []

    async def record(self, inspection) -> None:
        self.inspections.append(inspection)


class FailingSink:
    async def record(self, inspection) -> None:
        raise RuntimeError("sink failed")


class LifeSwitchSelfShadowObserverV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_returns_and_records_only_safe_inspection(self):
        request, envelope = _source()
        sink = RecordingSink()
        observer = LifeSwitchSelfShadowObserverV2(sink)
        inspection = await observer.observe(
            request=request,
            envelope=envelope,
            context_snapshot_id=CONTEXT,
            transaction_snapshot_digest="b" * 64,
            evaluated_at=NOW,
        )
        self.assertEqual(sink.inspections, [inspection])
        serialized = inspection.model_dump_json()
        self.assertNotIn(str(OWNER), serialized)
        self.assertNotIn(str(THREAD), serialized)
        self.assertNotIn("Maintain", serialized)
        self.assertFalse(inspection.prompt_influence)
        self.assertFalse(inspection.model_exposure)
        self.assertFalse(inspection.persistence)

    async def test_observer_without_sink_has_no_side_effect(self):
        request, envelope = _source()
        inspection = await LifeSwitchSelfShadowObserverV2().observe(
            request=request,
            envelope=envelope,
            context_snapshot_id=CONTEXT,
            transaction_snapshot_digest="b" * 64,
            evaluated_at=NOW,
        )
        self.assertEqual(inspection.target_projection_id, "plan.current.v1")

    async def test_sink_failure_is_visible_to_provider_boundary(self):
        request, envelope = _source()
        with self.assertRaisesRegex(RuntimeError, "sink failed"):
            await LifeSwitchSelfShadowObserverV2(FailingSink()).observe(
                request=request,
                envelope=envelope,
                context_snapshot_id=CONTEXT,
                transaction_snapshot_digest="b" * 64,
                evaluated_at=NOW,
            )

    async def test_naive_evaluation_time_fails_closed(self):
        request, envelope = _source()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            await LifeSwitchSelfShadowObserverV2().observe(
                request=request,
                envelope=envelope,
                context_snapshot_id=CONTEXT,
                transaction_snapshot_digest="b" * 64,
                evaluated_at=dt.datetime(2026, 8, 1, 19),
            )

    def test_default_epoch_is_fixed_server_policy_version(self):
        self.assertEqual(
            SELF_S1_SHADOW_AUTHORIZATION_EPOCH,
            "lifeswitch-self-s1-shadow-policy-1",
        )

    def test_empty_or_oversized_epoch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "epoch"):
            LifeSwitchSelfShadowObserverV2(authorization_epoch="")
        with self.assertRaisesRegex(ValueError, "epoch"):
            LifeSwitchSelfShadowObserverV2(authorization_epoch="x" * 257)

    def test_observer_has_no_database_network_or_prompt_imports(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "seebx"
            / "capabilities"
            / "coaching"
            / "shadow_observer.py"
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

    def test_machine_readable_contract_matches_observer(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "specs"
            / "lifeswitch"
            / "self_s1_shadow_observer_v1.yaml"
        )
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["contract_version"], SELF_S1_SHADOW_OBSERVER_CONTRACT)
        self.assertFalse(artifact["activation"]["enabled_by_default"])
        self.assertEqual(artifact["failure_behavior"], "discard_shadow_only")


if __name__ == "__main__":
    unittest.main()
