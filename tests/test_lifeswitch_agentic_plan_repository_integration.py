from __future__ import annotations

import asyncio
import os
import unittest
import uuid

import asyncpg

from lifeswitch_agentic.plan_domain import (
    PlanDocumentV1,
    PlanDomainError,
    RevisionState,
    RevisionTrigger,
)
from lifeswitch_agentic.plan_repository import ActivationResult, PlanRepository, uuid7


def plan(*, calorie_lower: int = 1800, steps_minimum: int = 7000) -> PlanDocumentV1:
    return PlanDocumentV1.from_mapping(
        {
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
            "activity_targets": {"steps_minimum": steps_minimum},
            "recovery_targets": {},
            "monitoring_rules": {"review_every_days": 7},
            "coach_notes": "",
        }
    )


class UUID7Test(unittest.TestCase):
    def test_uuid7_has_rfc_variant_and_version(self) -> None:
        value = uuid7()
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)


class PlanRepositoryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dsn = os.getenv("LIFESWITCH_AGENTIC_TEST_DSN", "").strip()
        if not self.dsn:
            self.skipTest("LIFESWITCH_AGENTIC_TEST_DSN is not configured")
        self.conn = await asyncpg.connect(self.dsn)
        self.repo = PlanRepository()

    async def asyncTearDown(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            await conn.close()

    async def _activate_initial(self, owner: uuid.UUID) -> ActivationResult:
        draft = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(),
            trigger=RevisionTrigger.INITIAL_PLAN,
            base_plan_version_id=None,
            author_type="owner",
            author_actor_user_id=owner,
            request_id=f"initial-draft-{uuid.uuid4()}",
        )
        proposal = await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            actor_user_id=owner,
            request_id=f"initial-proposal-{uuid.uuid4()}",
        )
        self.assertEqual(proposal.state, RevisionState.PROPOSED)
        return await self.repo.approve_and_activate(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            approving_actor_user_id=owner,
            owner_timezone="America/Chicago",
            request_id=f"initial-approval-{uuid.uuid4()}",
        )

    async def test_initial_plan_activation_is_versioned_and_history_is_immutable(self) -> None:
        owner = uuid.uuid4()
        result = await self._activate_initial(owner)
        self.assertEqual(result.version_number, 1)
        self.assertIsNone(result.prior_plan_version_id)

        state = await self.conn.fetchrow(
            """
            select active_plan_version_id, next_version_number
            from lifeswitch_agentic.plan_owner_state
            where owner_user_id = $1
            """,
            owner,
        )
        self.assertEqual(state["active_plan_version_id"], result.plan_version_id)
        self.assertEqual(state["next_version_number"], 2)

        revision = await self.conn.fetchrow(
            """
            select state, activated_plan_version_id
            from lifeswitch_agentic.plan_revisions
            where owner_user_id = $1 and id = $2
            """,
            owner,
            result.revision_id,
        )
        self.assertEqual(revision["state"], "activated")
        self.assertEqual(revision["activated_plan_version_id"], result.plan_version_id)

        change_id = await self.conn.fetchval(
            """
            select id from lifeswitch_agentic.plan_revision_changes
            where plan_revision_id = $1 limit 1
            """,
            result.revision_id,
        )
        self.assertIsNotNone(change_id)
        with self.assertRaises(asyncpg.PostgresError) as caught:
            await self.conn.execute(
                "update lifeswitch_agentic.plan_revision_changes set rationale = 'mutated' where id = $1",
                change_id,
            )
        self.assertEqual(caught.exception.sqlstate, "55000")

    async def test_database_rejects_cross_owner_revision_base(self) -> None:
        owner = uuid.uuid4()
        other_owner = uuid.uuid4()
        active = await self._activate_initial(owner)
        proposed = plan()
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.conn.execute(
                """
                insert into lifeswitch_agentic.plan_revisions (
                  id, owner_user_id, base_plan_version_id, state,
                  author_type, author_actor_user_id, trigger_type,
                  proposed_document, proposed_document_sha256, schema_version
                ) values (
                  $1, $2, $3, 'draft', 'owner', $2, 'owner_request',
                  $4::jsonb, $5, 1
                )
                """,
                uuid.uuid4(),
                other_owner,
                active.plan_version_id,
                proposed.canonical_json(),
                proposed.sha256(),
            )

    async def test_non_owner_cannot_approve(self) -> None:
        owner = uuid.uuid4()
        coach = uuid.uuid4()
        draft = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(),
            trigger=RevisionTrigger.INITIAL_PLAN,
            base_plan_version_id=None,
            author_type="coach",
            author_actor_user_id=coach,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            actor_user_id=coach,
        )
        with self.assertRaises(PlanDomainError) as caught:
            await self.repo.approve_and_activate(
                self.conn,
                owner_user_id=owner,
                revision_id=draft.revision_id,
                approving_actor_user_id=coach,
                owner_timezone="America/Chicago",
            )
        self.assertEqual(caught.exception.code, "owner_approval_required")
        active_count = await self.conn.fetchval(
            "select count(*) from lifeswitch_agentic.plan_versions where owner_user_id = $1",
            owner,
        )
        self.assertEqual(active_count, 0)

    async def test_concurrent_approvals_activate_one_revision_and_conflict_the_other(self) -> None:
        owner = uuid.uuid4()
        initial = await self._activate_initial(owner)
        first = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(calorie_lower=1750),
            trigger=RevisionTrigger.OWNER_REQUEST,
            base_plan_version_id=initial.plan_version_id,
            author_type="owner",
            author_actor_user_id=owner,
        )
        second = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(steps_minimum=8000),
            trigger=RevisionTrigger.OWNER_REQUEST,
            base_plan_version_id=initial.plan_version_id,
            author_type="owner",
            author_actor_user_id=owner,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=first.revision_id,
            actor_user_id=owner,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=second.revision_id,
            actor_user_id=owner,
        )

        first_conn = await asyncpg.connect(self.dsn)
        second_conn = await asyncpg.connect(self.dsn)
        try:
            outcomes = await asyncio.gather(
                self.repo.approve_and_activate(
                    first_conn,
                    owner_user_id=owner,
                    revision_id=first.revision_id,
                    approving_actor_user_id=owner,
                    owner_timezone="America/Chicago",
                ),
                self.repo.approve_and_activate(
                    second_conn,
                    owner_user_id=owner,
                    revision_id=second.revision_id,
                    approving_actor_user_id=owner,
                    owner_timezone="America/Chicago",
                ),
                return_exceptions=True,
            )
        finally:
            await first_conn.close()
            await second_conn.close()

        activated = [value for value in outcomes if isinstance(value, ActivationResult)]
        conflicts = [
            value
            for value in outcomes
            if isinstance(value, PlanDomainError) and value.code == "state_conflict"
        ]
        self.assertEqual(len(activated), 1)
        self.assertEqual(len(conflicts), 1)

        states = await self.conn.fetch(
            """
            select state from lifeswitch_agentic.plan_revisions
            where owner_user_id = $1 and id = any($2::uuid[])
            order by state
            """,
            owner,
            [first.revision_id, second.revision_id],
        )
        self.assertEqual({row["state"] for row in states}, {"activated", "conflicted"})
        versions = await self.conn.fetch(
            """
            select version_number, status
            from lifeswitch_agentic.plan_versions
            where owner_user_id = $1
            order by version_number
            """,
            owner,
        )
        self.assertEqual(
            [(row["version_number"], row["status"]) for row in versions],
            [(1, "superseded"), (2, "active")],
        )


if __name__ == "__main__":
    unittest.main()
