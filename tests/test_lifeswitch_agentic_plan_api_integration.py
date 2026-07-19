from __future__ import annotations

import asyncio
import json
import os
import unittest
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlencode, urlsplit

import asyncpg
from fastapi import FastAPI, HTTPException, Request

from lifeswitch_agentic.plan_api import ActorContext, create_plan_router
from lifeswitch_agentic.plan_recommendations import (
    ModelEvidence,
    ModelPlanReview,
    ModelSuggestion,
    PlanRecommendationService,
    ProviderPlanReview,
)


class FakePlanRecommendationProvider:
    async def review_plan(self, **_: Any) -> ProviderPlanReview:
        return ProviderPlanReview(
            output=ModelPlanReview(
                summary="The goal can be stated more measurably.",
                questions=[],
                suggestions=[
                    ModelSuggestion(
                        field_path="/primary_goal",
                        proposed_value=(
                            "Reduce body-fat percentage to 16% while maintaining strength."
                        ),
                        rationale="This makes the intended outcome measurable.",
                        evidence=[
                            ModelEvidence(
                                field_path="/phase_label",
                                explanation="The phase label already identifies 16% body fat.",
                            )
                        ],
                        confidence="high",
                        data_sufficiency="sufficient",
                    )
                ],
            ),
            provider="fake",
            model="fake-plan-model",
            response_id="resp_api_test",
        )


def plan_document(*, calorie_lower: int = 1800) -> dict[str, Any]:
    return {
        "phase": "cut",
        "phase_label": "Cut to 16% body fat",
        "primary_goal": "Reduce body-fat percentage while maintaining strength.",
        "start_date": "2026-07-01",
        "review_date": "2026-07-15",
        "review_cadence": "weekly",
        "body_state": {"body_fat_percent": 20},
        "nutrition_targets": {
            "calorie_target": {"lower": calorie_lower, "upper": 2100},
            "protein_grams_minimum": 160,
        },
        "training_targets": {"strength_sessions_per_week": 3},
        "conditioning_targets": {},
        "activity_targets": {"steps_minimum": 7000},
        "recovery_targets": {},
        "monitoring_rules": {"review_every_days": 7},
        "coach_notes": "",
    }


async def asgi_request(
    app: FastAPI,
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
) -> tuple[int, dict[str, str], Any]:
    parsed = urlsplit(target)
    body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    request_headers = {"host": "testserver", **dict(headers or {})}
    if json_body is not None:
        request_headers["content-type"] = "application/json"
        request_headers["content-length"] = str(len(body))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    request_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    decoded = json.loads(response_body) if response_body else None
    return int(start["status"]), response_headers, decoded


class PlanApiIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dsn = os.getenv("LIFESWITCH_AGENTIC_TEST_DSN", "").strip()
        if not self.dsn:
            self.skipTest("LIFESWITCH_AGENTIC_TEST_DSN is not configured")

        self.owner = uuid.uuid4()
        self.other_owner = uuid.uuid4()
        self.coach = uuid.uuid4()
        self.viewer = uuid.uuid4()

        @asynccontextmanager
        async def connection_provider() -> AsyncIterator[asyncpg.Connection]:
            conn = await asyncpg.connect(self.dsn)
            try:
                yield conn
            finally:
                await conn.close()

        async def actor_dependency(request: Request) -> ActorContext:
            principal = request.headers.get("x-test-principal", "owner")
            contexts = {
                "owner": ActorContext(
                    actor_user_id=self.owner,
                    owner_user_id=self.owner,
                    owner_timezone="America/Chicago",
                ),
                "other-owner": ActorContext(
                    actor_user_id=self.other_owner,
                    owner_user_id=self.other_owner,
                    owner_timezone="America/New_York",
                ),
                "coach": ActorContext(
                    actor_user_id=self.coach,
                    owner_user_id=self.owner,
                    owner_timezone="America/Chicago",
                    permission_scopes=frozenset({"plan:view", "plan:edit"}),
                ),
                "viewer": ActorContext(
                    actor_user_id=self.viewer,
                    owner_user_id=self.owner,
                    owner_timezone="America/Chicago",
                    permission_scopes=frozenset({"plan:view"}),
                ),
            }
            if principal not in contexts:
                raise HTTPException(status_code=401, detail="unknown test principal")
            return contexts[principal]

        self.app = FastAPI()
        self.app.include_router(
            create_plan_router(
                connection_provider=connection_provider,
                actor_dependency=actor_dependency,
                recommendation_service=PlanRecommendationService(
                    FakePlanRecommendationProvider()
                ),
            ),
            prefix="/lifeswitch/plan",
        )
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute("create schema if not exists lifeswitch_plan")
            await conn.execute(
                """
                create table if not exists lifeswitch_plan.plan_profile (
                  plan_profile_id uuid primary key,
                  owner_user_id uuid not null unique,
                  phase text not null,
                  phase_label text not null,
                  primary_goal text not null,
                  start_date date,
                  review_date date,
                  review_cadence text not null,
                  body_state jsonb not null,
                  nutrition_targets jsonb not null,
                  training_targets jsonb not null,
                  conditioning_targets jsonb not null,
                  activity_targets jsonb not null,
                  recovery_targets jsonb not null,
                  monitoring_rules jsonb not null,
                  coach_notes text not null,
                  is_active boolean not null,
                  updated_at timestamptz not null
                )
                """
            )
        finally:
            await conn.close()

    async def request(
        self,
        method: str,
        target: str,
        *,
        principal: str = "owner",
        idempotency_key: str | None = None,
        json_body: Any = None,
    ) -> tuple[int, dict[str, str], Any]:
        headers = {"x-test-principal": principal, "x-request-id": str(uuid.uuid4())}
        if idempotency_key is not None:
            headers["idempotency-key"] = idempotency_key
        return await asgi_request(
            self.app,
            method,
            target,
            headers=headers,
            json_body=json_body,
        )

    async def create_draft(
        self,
        *,
        principal: str = "owner",
        key: str | None = None,
        document: dict[str, Any] | None = None,
        base_plan_version_id: str | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        body: dict[str, Any] = {"document": document or plan_document()}
        if base_plan_version_id is not None:
            body["base_plan_version_id"] = base_plan_version_id
        return await self.request(
            "POST",
            "/lifeswitch/plan/revisions",
            principal=principal,
            idempotency_key=key or f"create-{uuid.uuid4()}",
            json_body=body,
        )

    async def test_route_contract_does_not_accept_identity_or_timezone_fields(self) -> None:
        schema = self.app.openapi()
        paths = schema["paths"]
        expected = {
            "/lifeswitch/plan/workspace",
            "/lifeswitch/plan/active",
            "/lifeswitch/plan/versions",
            "/lifeswitch/plan/versions/{plan_version_id}",
            "/lifeswitch/plan/revisions/{revision_id}",
            "/lifeswitch/plan/revisions/{revision_id}/recommendations",
            "/lifeswitch/plan/revisions",
            "/lifeswitch/plan/revisions/adopt-current-profile",
            "/lifeswitch/plan/revisions/adopt-current-profile/refresh",
            "/lifeswitch/plan/revisions/from-current-profile",
            "/lifeswitch/plan/revisions/{revision_id}/draft",
            "/lifeswitch/plan/revisions/{revision_id}/refresh-current-profile",
            "/lifeswitch/plan/revisions/{revision_id}/propose",
            "/lifeswitch/plan/revisions/{revision_id}/approve-and-activate",
        }
        self.assertTrue(expected.issubset(paths))
        for model_name in (
            "CreateDraftRequest",
            "SaveDraftRequest",
            "PlanRecommendationRequest",
        ):
            properties = schema["components"]["schemas"][model_name]["properties"]
            for forbidden in (
                "owner_user_id",
                "actor_user_id",
                "owner_timezone",
                "permission_scopes",
            ):
                self.assertNotIn(forbidden, properties)

    async def test_sage_review_is_edit_scoped_and_does_not_mutate_draft(self) -> None:
        status, _, draft = await self.create_draft()
        self.assertEqual(status, 201)
        revision_id = draft["revision_id"]

        status, _, denied = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/recommendations",
            principal="viewer",
            json_body={"focus": "goal", "user_request": "Review the goal."},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["detail"]["code"], "permission_denied")

        status, _, review = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/recommendations",
            json_body={"focus": "goal", "user_request": "Review the goal."},
        )
        self.assertEqual(status, 200)
        self.assertFalse(review["active_plan_changed"])
        self.assertFalse(review["recommendation"]["provenance"]["writes_performed"])
        self.assertEqual(len(review["recommendation"]["suggestions"]), 1)

        status, _, stored = await self.request(
            "GET", f"/lifeswitch/plan/revisions/{revision_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            stored["revision"]["proposed_document"]["primary_goal"],
            plan_document()["primary_goal"],
        )

        status, _, _ = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/propose",
            idempotency_key=f"propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        status, _, conflict = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/recommendations",
            json_body={"focus": "whole_plan"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["detail"]["code"], "revision_not_draft")

    async def test_owner_lifecycle_is_explicit_and_versioned(self) -> None:
        status, _, payload = await self.request("GET", "/lifeswitch/plan/active")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["active_plan"])

        status, _, draft = await self.create_draft()
        self.assertEqual(status, 201)
        self.assertEqual(draft["state"], "draft")
        self.assertFalse(draft["active_plan_changed"])
        revision_id = draft["revision_id"]

        status, _, review = await self.request(
            "GET", f"/lifeswitch/plan/revisions/{revision_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(review["revision"]["state"], "draft")
        self.assertFalse(review["revision"]["can_approve"])

        status, _, proposal = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/propose",
            idempotency_key=f"propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposal["state"], "proposed")
        self.assertEqual(proposal["validation_status"], "valid")
        self.assertGreater(proposal["change_count"], 0)
        self.assertFalse(proposal["active_plan_changed"])

        status, _, review = await self.request(
            "GET", f"/lifeswitch/plan/revisions/{revision_id}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(review["revision"]["can_approve"])
        self.assertTrue(review["revision"]["base_is_current"])
        self.assertGreater(len(review["revision"]["changes"]), 0)

        status, _, activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/approve-and-activate",
            idempotency_key=f"approve-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(activation["active_plan_changed"])
        self.assertEqual(activation["version_number"], 1)

        status, _, active = await self.request("GET", "/lifeswitch/plan/active")
        self.assertEqual(status, 200)
        self.assertEqual(active["active_plan"]["version_number"], 1)
        self.assertEqual(active["active_plan"]["document"]["phase"], "cut")

        status, _, history = await self.request(
            "GET", "/lifeswitch/plan/versions?" + urlencode({"limit": 20})
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(history["versions"]), 1)
        version_id = history["versions"][0]["plan_version_id"]
        status, _, version = await self.request(
            "GET", f"/lifeswitch/plan/versions/{version_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(version["plan_version"]["source_revision_id"], revision_id)

        status, _, second_draft = await self.create_draft(
            document=plan_document(calorie_lower=1700),
            base_plan_version_id=activation["plan_version_id"],
        )
        self.assertEqual(status, 201)
        second_revision_id = second_draft["revision_id"]
        status, _, _ = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{second_revision_id}/propose",
            idempotency_key=f"second-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        status, _, second_activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{second_revision_id}/approve-and-activate",
            idempotency_key=f"second-approve-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(second_activation["version_number"], 2)

        status, _, first_page = await self.request(
            "GET", "/lifeswitch/plan/versions?" + urlencode({"limit": 1})
        )
        self.assertEqual(status, 200)
        self.assertEqual(first_page["versions"][0]["version_number"], 2)
        self.assertEqual(first_page["next_before_version"], 2)
        status, _, second_page = await self.request(
            "GET",
            "/lifeswitch/plan/versions?"
            + urlencode({"limit": 1, "before_version": first_page["next_before_version"]}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(second_page["versions"][0]["version_number"], 1)
        self.assertIsNone(second_page["next_before_version"])

    async def test_legacy_profile_adoption_creates_one_draft_and_never_auto_activates(self) -> None:
        legacy_profile_id = uuid.uuid4()
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(
                """
                insert into lifeswitch_plan.plan_profile (
                  plan_profile_id, owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets,
                  recovery_targets, monitoring_rules, coach_notes,
                  is_active, updated_at
                ) values (
                  $1, $2,
                  'cut', 'Cut to 16% body fat',
                  'Reduce body-fat percentage while maintaining strength.',
                  '2026-07-01'::date, '2026-07-15'::date, 'weekly',
                  '{"body_fat_percent":20}'::jsonb,
                  '{"calorie_target":{"lower":1800,"upper":2100},"protein_grams_minimum":160}'::jsonb,
                  '{"strength_sessions_per_week":3}'::jsonb,
                  '{}'::jsonb,
                  '{"steps_minimum":7000}'::jsonb,
                  '{}'::jsonb,
                  '{"review_every_days":7}'::jsonb,
                  '', true, '2026-07-19T12:00:00Z'::timestamptz
                )
                """,
                legacy_profile_id,
                self.owner,
            )
        finally:
            await conn.close()

        first_attempt, second_attempt = await asyncio.gather(
            self.request(
                "POST",
                "/lifeswitch/plan/revisions/adopt-current-profile",
                idempotency_key=f"adopt-{uuid.uuid4()}",
            ),
            self.request(
                "POST",
                "/lifeswitch/plan/revisions/adopt-current-profile",
                idempotency_key=f"adopt-concurrent-{uuid.uuid4()}",
            ),
        )
        self.assertEqual(first_attempt[0], 201)
        self.assertEqual(second_attempt[0], 201)
        adoption_payloads = (first_attempt[2], second_attempt[2])
        self.assertEqual(len({payload["revision_id"] for payload in adoption_payloads}), 1)
        self.assertEqual(
            sorted(payload["created"] for payload in adoption_payloads),
            [False, True],
        )
        adopted = next(payload for payload in adoption_payloads if payload["created"])
        self.assertEqual(adopted["state"], "draft")
        self.assertFalse(adopted["active_plan_changed"])
        self.assertEqual(
            adopted["source"]["legacy_plan_profile_id"],
            str(legacy_profile_id),
        )

        status, _, workspace = await self.request("GET", "/lifeswitch/plan/workspace")
        self.assertEqual(status, 200)
        self.assertIsNone(workspace["active_plan"])
        self.assertEqual(
            workspace["capabilities"],
            {"can_edit": True, "can_approve": True},
        )
        self.assertEqual(workspace["open_revision"]["revision_id"], adopted["revision_id"])
        self.assertEqual(
            workspace["open_revision"]["source"]["legacy_plan_profile_id"],
            str(legacy_profile_id),
        )

        status, _, coach_workspace = await self.request(
            "GET",
            "/lifeswitch/plan/workspace",
            principal="coach",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            coach_workspace["capabilities"],
            {"can_edit": True, "can_approve": False},
        )
        status, _, viewer_workspace = await self.request(
            "GET",
            "/lifeswitch/plan/workspace",
            principal="viewer",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            viewer_workspace["capabilities"],
            {"can_edit": False, "can_approve": False},
        )

        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(
                """
                update lifeswitch_plan.plan_profile
                set nutrition_targets =
                      '{"calorie_target":{"lower":1750,"upper":2050},"protein_grams_minimum":165}'::jsonb,
                    updated_at = '2026-07-19T13:00:00Z'::timestamptz
                where owner_user_id = $1
                """,
                self.owner,
            )
        finally:
            await conn.close()

        refresh_key = f"adopt-refresh-{uuid.uuid4()}"
        status, _, refreshed = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/adopt-current-profile/refresh",
            idempotency_key=refresh_key,
        )
        self.assertEqual(status, 200)
        self.assertTrue(refreshed["refreshed"])
        self.assertEqual(refreshed["revision_id"], adopted["revision_id"])
        status, _, replayed_refresh = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/adopt-current-profile/refresh",
            idempotency_key=refresh_key,
        )
        self.assertEqual(status, 200)
        self.assertEqual(replayed_refresh["revision_id"], adopted["revision_id"])

        status, _, workspace = await self.request("GET", "/lifeswitch/plan/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(
            workspace["open_revision"]["proposed_document"]["nutrition_targets"][
                "calorie_target"
            ]["lower"],
            1750,
        )
        self.assertEqual(
            workspace["open_revision"]["source"]["legacy_profile_updated_at"],
            "2026-07-19T13:00:00+00:00",
        )

        status, _, active = await self.request("GET", "/lifeswitch/plan/active")
        self.assertEqual(status, 200)
        self.assertIsNone(active["active_plan"])

        status, _, replay = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/adopt-current-profile",
            idempotency_key=f"adopt-again-{uuid.uuid4()}",
        )
        self.assertEqual(status, 201)
        self.assertEqual(replay["revision_id"], adopted["revision_id"])
        self.assertFalse(replay["created"])

        status, _, denied = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/adopt-current-profile",
            principal="coach",
            idempotency_key=f"coach-adopt-{uuid.uuid4()}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["detail"]["code"], "owner_approval_required")

        revision_id = adopted["revision_id"]
        status, _, proposal = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/propose",
            idempotency_key=f"adopt-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertIn(proposal["validation_status"], {"valid", "valid_with_warnings"})

        status, _, activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/approve-and-activate",
            idempotency_key=f"adopt-approve-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(activation["active_plan_changed"])

        conn = await asyncpg.connect(self.dsn)
        try:
            counts = await conn.fetchrow(
                """
                select
                  (select count(*) from lifeswitch_agentic.plan_revisions
                   where owner_user_id = $1) as revision_count,
                  (select count(*) from lifeswitch_agentic.plan_revision_events
                   where owner_user_id = $1
                     and event_type = 'plan_revision_legacy_adopted') as adoption_event_count,
                  (select count(*) from lifeswitch_agentic.plan_revision_events
                   where owner_user_id = $1
                     and event_type = 'plan_revision_legacy_refreshed') as refresh_event_count,
                  (select activation_type from lifeswitch_agentic.plan_versions
                   where owner_user_id = $1 and status = 'active') as activation_type
                """,
                self.owner,
            )
        finally:
            await conn.close()
        self.assertEqual(counts["revision_count"], 1)
        self.assertEqual(counts["adoption_event_count"], 1)
        self.assertEqual(counts["refresh_event_count"], 1)
        self.assertEqual(counts["activation_type"], "owner_approval")

    async def test_post_activation_profile_changes_create_a_new_approvable_version(self) -> None:
        status, _, initial_draft = await self.create_draft()
        self.assertEqual(status, 201)
        status, _, _ = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{initial_draft['revision_id']}/propose",
            idempotency_key=f"initial-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        status, _, initial_activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{initial_draft['revision_id']}/approve-and-activate",
            idempotency_key=f"initial-activate-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(initial_activation["version_number"], 1)

        legacy_profile_id = uuid.uuid4()
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(
                """
                insert into lifeswitch_plan.plan_profile (
                  plan_profile_id, owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets,
                  recovery_targets, monitoring_rules, coach_notes,
                  is_active, updated_at
                ) values (
                  $1, $2,
                  'cut', 'Cut to 16% body fat',
                  'Reduce body-fat percentage while maintaining strength.',
                  '2026-07-01'::date, '2026-07-15'::date, 'weekly',
                  '{"body_fat_percent":20}'::jsonb,
                  '{"calorie_target":{"lower":1700,"upper":2100},"protein_grams_minimum":160}'::jsonb,
                  '{"strength_sessions_per_week":3}'::jsonb,
                  '{}'::jsonb,
                  '{"steps_minimum":7000}'::jsonb,
                  '{}'::jsonb,
                  '{"review_every_days":7}'::jsonb,
                  '', true, '2026-07-19T14:00:00Z'::timestamptz
                )
                """,
                legacy_profile_id,
                self.owner,
            )
        finally:
            await conn.close()

        import_keys = [
            f"profile-revision-{uuid.uuid4()}",
            f"profile-revision-concurrent-{uuid.uuid4()}",
        ]
        import_attempts = await asyncio.gather(
            *(
                self.request(
                    "POST",
                    "/lifeswitch/plan/revisions/from-current-profile",
                    principal="coach",
                    idempotency_key=key,
                )
                for key in import_keys
            )
        )
        self.assertEqual(sorted(attempt[0] for attempt in import_attempts), [201, 409])
        successful_index = next(
            index for index, attempt in enumerate(import_attempts) if attempt[0] == 201
        )
        import_key = import_keys[successful_index]
        imported = import_attempts[successful_index][2]
        self.assertTrue(imported["created"])
        self.assertTrue(imported["imported"])
        self.assertFalse(imported["active_plan_changed"])
        self.assertEqual(
            imported["base_plan_version_id"],
            initial_activation["plan_version_id"],
        )
        self.assertEqual(imported["source"]["action"], "revision")

        status, _, replayed = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/from-current-profile",
            principal="coach",
            idempotency_key=import_key,
        )
        self.assertEqual(status, 201)
        self.assertEqual(replayed["revision_id"], imported["revision_id"])
        self.assertFalse(replayed["created"])

        status, _, conflict = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/from-current-profile",
            idempotency_key=f"second-open-{uuid.uuid4()}",
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["detail"]["code"], "state_conflict")

        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(
                """
                update lifeswitch_plan.plan_profile
                set nutrition_targets =
                      '{"calorie_target":{"lower":1650,"upper":2050},"protein_grams_minimum":165}'::jsonb,
                    updated_at = '2026-07-19T15:00:00Z'::timestamptz
                where owner_user_id = $1
                """,
                self.owner,
            )
        finally:
            await conn.close()

        refresh_key = f"profile-refresh-{uuid.uuid4()}"
        for _ in range(2):
            status, _, refreshed = await self.request(
                "POST",
                f"/lifeswitch/plan/revisions/{imported['revision_id']}/refresh-current-profile",
                principal="coach",
                idempotency_key=refresh_key,
            )
            self.assertEqual(status, 200)
            self.assertTrue(refreshed["refreshed"])

        status, _, workspace = await self.request(
            "GET",
            "/lifeswitch/plan/workspace",
            principal="coach",
        )
        self.assertEqual(status, 200)
        self.assertEqual(workspace["active_plan"]["version_number"], 1)
        self.assertEqual(
            workspace["open_revision"]["proposed_document"]["nutrition_targets"][
                "calorie_target"
            ]["lower"],
            1650,
        )
        self.assertEqual(workspace["open_revision"]["source"]["source_action"], "refresh")

        status, _, proposal = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{imported['revision_id']}/propose",
            principal="coach",
            idempotency_key=f"profile-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertGreater(proposal["change_count"], 0)
        status, _, activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{imported['revision_id']}/approve-and-activate",
            idempotency_key=f"profile-activate-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(activation["version_number"], 2)

        status, _, no_changes = await self.request(
            "POST",
            "/lifeswitch/plan/revisions/from-current-profile",
            idempotency_key=f"profile-no-change-{uuid.uuid4()}",
        )
        self.assertEqual(status, 409)
        self.assertEqual(no_changes["detail"]["code"], "no_plan_changes")

        conn = await asyncpg.connect(self.dsn)
        try:
            counts = await conn.fetchrow(
                """
                select
                  (select count(*) from lifeswitch_agentic.plan_revision_events
                   where owner_user_id = $1
                     and event_type = 'plan_revision_legacy_imported') as import_count,
                  (select count(*) from lifeswitch_agentic.plan_revision_events
                   where owner_user_id = $1
                     and event_type = 'plan_revision_legacy_refreshed') as refresh_count,
                  (select count(*) from lifeswitch_agentic.plan_versions
                   where owner_user_id = $1 and status = 'active') as active_count,
                  (select count(*) from lifeswitch_agentic.plan_versions
                   where owner_user_id = $1 and status = 'superseded') as superseded_count
                """,
                self.owner,
            )
        finally:
            await conn.close()
        self.assertEqual(counts["import_count"], 1)
        self.assertEqual(counts["refresh_count"], 1)
        self.assertEqual(counts["active_count"], 1)
        self.assertEqual(counts["superseded_count"], 1)

    async def test_coach_can_draft_but_only_owner_can_activate(self) -> None:
        status, _, draft = await self.create_draft(principal="coach")
        self.assertEqual(status, 201)
        revision_id = draft["revision_id"]
        status, _, _ = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/propose",
            principal="coach",
            idempotency_key=f"coach-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)

        status, _, review = await self.request(
            "GET",
            f"/lifeswitch/plan/revisions/{revision_id}",
            principal="coach",
        )
        self.assertEqual(status, 200)
        self.assertFalse(review["revision"]["can_approve"])

        status, _, denied = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/approve-and-activate",
            principal="coach",
            idempotency_key=f"coach-approve-{uuid.uuid4()}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["detail"]["code"], "owner_approval_required")

        status, _, approved = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{revision_id}/approve-and-activate",
            principal="owner",
            idempotency_key=f"owner-approve-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["active_plan_changed"])

    async def test_unchanged_revision_cannot_be_proposed(self) -> None:
        status, _, initial = await self.create_draft()
        self.assertEqual(status, 201)
        status, _, _ = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{initial['revision_id']}/propose",
            idempotency_key=f"unchanged-initial-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)
        status, _, activation = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{initial['revision_id']}/approve-and-activate",
            idempotency_key=f"unchanged-initial-activate-{uuid.uuid4()}",
        )
        self.assertEqual(status, 200)

        status, _, unchanged = await self.create_draft(
            document=plan_document(),
            base_plan_version_id=activation["plan_version_id"],
        )
        self.assertEqual(status, 201)
        status, _, rejected = await self.request(
            "POST",
            f"/lifeswitch/plan/revisions/{unchanged['revision_id']}/propose",
            idempotency_key=f"unchanged-propose-{uuid.uuid4()}",
        )
        self.assertEqual(status, 409)
        self.assertEqual(rejected["detail"]["code"], "no_plan_changes")

        status, _, active = await self.request("GET", "/lifeswitch/plan/active")
        self.assertEqual(status, 200)
        self.assertEqual(active["active_plan"]["version_number"], 1)

    async def test_viewer_cannot_write(self) -> None:
        status, _, payload = await self.create_draft(principal="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(payload["detail"]["code"], "permission_denied")

    async def test_cross_account_resources_are_indistinguishable_from_missing(self) -> None:
        status, _, draft = await self.create_draft()
        self.assertEqual(status, 201)
        status, _, payload = await self.request(
            "GET",
            f"/lifeswitch/plan/revisions/{draft['revision_id']}",
            principal="other-owner",
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["detail"]["code"], "entity_out_of_scope")
        self.assertEqual(payload["detail"]["message"], "Resource unavailable.")

    async def test_write_requires_idempotency_key(self) -> None:
        status, _, payload = await self.request(
            "POST",
            "/lifeswitch/plan/revisions",
            json_body={"document": plan_document()},
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["detail"][0]["loc"][-1], "Idempotency-Key")

    async def test_duplicate_command_replays_and_conflicting_input_is_rejected(self) -> None:
        key = f"duplicate-{uuid.uuid4()}"
        status, _, first = await self.create_draft(key=key)
        self.assertEqual(status, 201)
        status, _, replay = await self.create_draft(key=key)
        self.assertEqual(status, 201)
        self.assertEqual(replay, first)

        status, _, conflict = await self.create_draft(
            key=key,
            document=plan_document(calorie_lower=1700),
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["detail"]["code"], "idempotency_conflict")

        conn = await asyncpg.connect(self.dsn)
        try:
            count = await conn.fetchval(
                """
                select count(*)
                from lifeswitch_agentic.plan_revisions
                where owner_user_id = $1
                """,
                self.owner,
            )
        finally:
            await conn.close()
        self.assertEqual(count, 1)

    async def test_body_identity_override_and_unknown_plan_fields_are_rejected(self) -> None:
        status, _, payload = await self.request(
            "POST",
            "/lifeswitch/plan/revisions",
            idempotency_key=f"body-identity-{uuid.uuid4()}",
            json_body={
                "document": plan_document(),
                "owner_user_id": str(self.other_owner),
            },
        )
        self.assertEqual(status, 422)
        self.assertIn(
            payload["detail"][0]["type"],
            {"value_error.extra", "extra_forbidden"},
        )

        bad_document = plan_document()
        bad_document["biomarkers"] = {"a1c": 5.4}
        status, _, payload = await self.create_draft(document=bad_document)
        self.assertEqual(status, 422)
        self.assertEqual(payload["detail"]["code"], "unknown_plan_field")


if __name__ == "__main__":
    unittest.main()
