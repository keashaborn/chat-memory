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
from lifeswitch_agentic.outbox_repository import OutboxRepository


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
            idempotency_key=f"initial-draft-{uuid.uuid4()}",
            author_actor_user_id=owner,
            request_id=f"initial-draft-{uuid.uuid4()}",
        )
        proposal = await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            idempotency_key=f"initial-proposal-{uuid.uuid4()}",
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
            idempotency_key=f"initial-approval-{uuid.uuid4()}",
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

        with self.assertRaises(asyncpg.PostgresError) as receipt_mutation:
            await self.conn.execute(
                """
                update lifeswitch_agentic.command_receipts
                set response = '{"outcome":"mutated"}'::jsonb
                where owner_user_id = $1 and state = 'completed'
                """,
                owner,
            )
        self.assertEqual(receipt_mutation.exception.sqlstate, "55000")

        outbox_event_id = await self.conn.fetchval(
            """
            select id from lifeswitch_agentic.outbox_events
            where owner_user_id = $1 limit 1
            """,
            owner,
        )
        with self.assertRaises(asyncpg.PostgresError) as premature_completion:
            await self.conn.execute(
                """
                update lifeswitch_agentic.outbox_events
                set handled_at = now()
                where id = $1
                """,
                outbox_event_id,
            )
        self.assertEqual(premature_completion.exception.sqlstate, "55000")

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
            idempotency_key=f"coach-draft-{uuid.uuid4()}",
            author_actor_user_id=coach,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            idempotency_key=f"coach-proposal-{uuid.uuid4()}",
            actor_user_id=coach,
        )
        with self.assertRaises(PlanDomainError) as caught:
            await self.repo.approve_and_activate(
                self.conn,
                owner_user_id=owner,
                revision_id=draft.revision_id,
                approving_actor_user_id=coach,
                owner_timezone="America/Chicago",
                idempotency_key=f"coach-approval-{uuid.uuid4()}",
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
            idempotency_key=f"first-draft-{uuid.uuid4()}",
            author_actor_user_id=owner,
        )
        second = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(steps_minimum=8000),
            trigger=RevisionTrigger.OWNER_REQUEST,
            base_plan_version_id=initial.plan_version_id,
            author_type="owner",
            idempotency_key=f"second-draft-{uuid.uuid4()}",
            author_actor_user_id=owner,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=first.revision_id,
            idempotency_key=f"first-proposal-{uuid.uuid4()}",
            actor_user_id=owner,
        )
        await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=second.revision_id,
            idempotency_key=f"second-proposal-{uuid.uuid4()}",
            actor_user_id=owner,
        )

        first_conn = await asyncpg.connect(self.dsn)
        second_conn = await asyncpg.connect(self.dsn)
        first_approval_key = f"first-approval-{uuid.uuid4()}"
        second_approval_key = f"second-approval-{uuid.uuid4()}"
        try:
            outcomes = await asyncio.gather(
                self.repo.approve_and_activate(
                    first_conn,
                    owner_user_id=owner,
                    revision_id=first.revision_id,
                    approving_actor_user_id=owner,
                    owner_timezone="America/Chicago",
                    idempotency_key=first_approval_key,
                ),
                self.repo.approve_and_activate(
                    second_conn,
                    owner_user_id=owner,
                    revision_id=second.revision_id,
                    approving_actor_user_id=owner,
                    owner_timezone="America/Chicago",
                    idempotency_key=second_approval_key,
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

        conflict_index = next(
            index
            for index, value in enumerate(outcomes)
            if isinstance(value, PlanDomainError) and value.code == "state_conflict"
        )
        conflicted_revision = (first.revision_id, second.revision_id)[conflict_index]
        conflicted_key = (first_approval_key, second_approval_key)[conflict_index]
        with self.assertRaises(PlanDomainError) as replayed_conflict:
            await self.repo.approve_and_activate(
                self.conn,
                owner_user_id=owner,
                revision_id=conflicted_revision,
                approving_actor_user_id=owner,
                owner_timezone="America/Chicago",
                idempotency_key=conflicted_key,
            )
        self.assertEqual(replayed_conflict.exception.code, "state_conflict")

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

    async def test_concurrent_duplicate_create_replays_one_durable_result(self) -> None:
        owner = uuid.uuid4()
        key = f"duplicate-create-{uuid.uuid4()}"
        first_conn = await asyncpg.connect(self.dsn)
        second_conn = await asyncpg.connect(self.dsn)
        try:
            results = await asyncio.gather(
                self.repo.create_draft(
                    first_conn,
                    owner_user_id=owner,
                    document=plan(),
                    trigger=RevisionTrigger.INITIAL_PLAN,
                    base_plan_version_id=None,
                    author_type="owner",
                    idempotency_key=key,
                    author_actor_user_id=owner,
                ),
                self.repo.create_draft(
                    second_conn,
                    owner_user_id=owner,
                    document=plan(),
                    trigger=RevisionTrigger.INITIAL_PLAN,
                    base_plan_version_id=None,
                    author_type="owner",
                    idempotency_key=key,
                    author_actor_user_id=owner,
                ),
            )
        finally:
            await first_conn.close()
            await second_conn.close()

        self.assertEqual(results[0], results[1])
        revision_count = await self.conn.fetchval(
            "select count(*) from lifeswitch_agentic.plan_revisions where owner_user_id = $1",
            owner,
        )
        receipt_count = await self.conn.fetchval(
            """
            select count(*) from lifeswitch_agentic.command_receipts
            where owner_user_id = $1 and idempotency_key = $2 and state = 'completed'
            """,
            owner,
            key,
        )
        self.assertEqual(revision_count, 1)
        self.assertEqual(receipt_count, 1)

    async def test_same_idempotency_key_with_different_input_is_rejected(self) -> None:
        owner = uuid.uuid4()
        key = f"input-conflict-{uuid.uuid4()}"
        first = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(),
            trigger=RevisionTrigger.INITIAL_PLAN,
            base_plan_version_id=None,
            author_type="owner",
            idempotency_key=key,
            author_actor_user_id=owner,
        )
        with self.assertRaises(PlanDomainError) as caught:
            await self.repo.create_draft(
                self.conn,
                owner_user_id=owner,
                document=plan(calorie_lower=1700),
                trigger=RevisionTrigger.INITIAL_PLAN,
                base_plan_version_id=None,
                author_type="owner",
                idempotency_key=key,
                author_actor_user_id=owner,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        stored = await self.conn.fetchval(
            """
            select proposed_document_sha256
            from lifeswitch_agentic.plan_revisions
            where owner_user_id = $1 and id = $2
            """,
            owner,
            first.revision_id,
        )
        self.assertEqual(stored, plan().sha256())

    async def test_completed_commands_replay_after_revision_state_advances(self) -> None:
        owner = uuid.uuid4()
        draft = await self.repo.create_draft(
            self.conn,
            owner_user_id=owner,
            document=plan(),
            trigger=RevisionTrigger.INITIAL_PLAN,
            base_plan_version_id=None,
            author_type="owner",
            idempotency_key=f"advance-draft-{uuid.uuid4()}",
            author_actor_user_id=owner,
        )
        proposal_key = f"advance-proposal-{uuid.uuid4()}"
        approval_key = f"advance-approval-{uuid.uuid4()}"
        proposal = await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            idempotency_key=proposal_key,
            actor_user_id=owner,
        )
        activation = await self.repo.approve_and_activate(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            approving_actor_user_id=owner,
            owner_timezone="America/Chicago",
            idempotency_key=approval_key,
        )

        proposal_replay = await self.repo.propose_revision(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            idempotency_key=proposal_key,
            actor_user_id=owner,
        )
        activation_replay = await self.repo.approve_and_activate(
            self.conn,
            owner_user_id=owner,
            revision_id=draft.revision_id,
            approving_actor_user_id=owner,
            owner_timezone="America/Chicago",
            idempotency_key=approval_key,
        )
        self.assertEqual(proposal_replay, proposal)
        self.assertEqual(activation_replay, activation)

        event_count = await self.conn.fetchval(
            """
            select count(*) from lifeswitch_agentic.outbox_events
            where owner_user_id = $1
              and event_type in ('plan_revision.proposed', 'plan_version.activated')
            """,
            owner,
        )
        self.assertEqual(event_count, 2)

    async def test_outbox_claim_release_reclaim_and_completion_are_lease_safe(self) -> None:
        owner = uuid.uuid4()
        await self._activate_initial(owner)
        outbox = OutboxRepository()

        first_claim = await outbox.claim_batch(
            self.conn,
            worker_id="worker.one",
            limit=100,
            lease_seconds=120,
        )
        self.assertGreaterEqual(len(first_claim), 1)
        event = first_claim[0]
        released = await outbox.release_failed(
            self.conn,
            event_id=event.event_id,
            claim_token=event.claim_token,
            error_code="synthetic_failure",
            retry_after_seconds=0,
        )
        self.assertTrue(released)

        second_claim = await outbox.claim_batch(
            self.conn,
            worker_id="worker.two",
            limit=1,
            lease_seconds=120,
        )
        self.assertEqual(len(second_claim), 1)
        reclaimed = second_claim[0]
        self.assertEqual(reclaimed.event_id, event.event_id)
        self.assertNotEqual(reclaimed.claim_token, event.claim_token)
        self.assertEqual(reclaimed.attempt_count, 2)

        stale_completion = await outbox.complete_delivery(
            self.conn,
            event_id=event.event_id,
            claim_token=event.claim_token,
            consumer_name="analysis.scheduler",
            handler_version="v1",
        )
        self.assertFalse(stale_completion)
        completed = await outbox.complete_delivery(
            self.conn,
            event_id=reclaimed.event_id,
            claim_token=reclaimed.claim_token,
            consumer_name="analysis.scheduler",
            handler_version="v1",
        )
        self.assertTrue(completed)
        replayed_completion = await outbox.complete_delivery(
            self.conn,
            event_id=reclaimed.event_id,
            claim_token=reclaimed.claim_token,
            consumer_name="analysis.scheduler",
            handler_version="v1",
        )
        self.assertTrue(replayed_completion)

        delivery_count = await self.conn.fetchval(
            """
            select count(*) from lifeswitch_agentic.outbox_deliveries
            where outbox_event_id = $1
            """,
            event.event_id,
        )
        self.assertEqual(delivery_count, 1)

    async def test_skip_locked_workers_claim_distinct_events(self) -> None:
        owner = uuid.uuid4()
        await self._activate_initial(owner)
        first_conn = await asyncpg.connect(self.dsn)
        second_conn = await asyncpg.connect(self.dsn)
        outbox = OutboxRepository()
        try:
            claimed = await asyncio.gather(
                outbox.claim_batch(first_conn, worker_id="worker.alpha", limit=1),
                outbox.claim_batch(second_conn, worker_id="worker.beta", limit=1),
            )
        finally:
            await first_conn.close()
            await second_conn.close()
        event_ids = {batch[0].event_id for batch in claimed if batch}
        self.assertEqual(len(event_ids), 2)


if __name__ == "__main__":
    unittest.main()
