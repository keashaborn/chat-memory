from __future__ import annotations

import ast
import dataclasses
import contextlib
import copy
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from uuid import UUID
from unittest import mock

from rag_engine.memory_v1_governed_claim_transition_v1 import (
    GovernedClaimMaterializationReceiptV1,
    GovernedClaimMaterializationResultV1,
    GovernedClaimSupportReceiptV1,
    GovernedClaimSupportResultV1,
    GovernedClaimTransitionError,
    ReviewedProjectionMaterializationV1,
    ReviewedSupportPromotionV1,
    VerifiedActorContextV1,
    materialize_reviewed_claim_v1,
    promote_independently_reviewed_claim_v1,
)
from scripts.memory_v1_v5_2_projection_dispatch import build_projection


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
PLAN = UUID("33333333-3333-4333-8333-333333333333")
PROJECTION_REVIEW = UUID("44444444-4444-4444-8444-444444444444")
PROJECTION_REQUEST = UUID("55555555-5555-4555-8555-555555555555")
CLAIM = UUID("66666666-6666-4666-8666-666666666666")
PROJECTION_EVENT = UUID("77777777-7777-4777-8777-777777777777")
ASSESSMENT_REVIEW_REQUEST = UUID("88888888-8888-4888-8888-888888888888")
ASSESSMENT_REVIEW = UUID("99999999-9999-4999-8999-999999999999")
ASSESSMENT_APPLY_REQUEST = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ASSESSMENT_APPLY_EVENT = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ASSESSMENT = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
OUTBOX = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def materialization_command() -> ReviewedProjectionMaterializationV1:
    return ReviewedProjectionMaterializationV1(
        plan_id=PLAN,
        projection_ref="p01",
        projection_review_id=PROJECTION_REVIEW,
        projection_request_id=PROJECTION_REQUEST,
        projection_apply_manifest_sha256=HASH_A,
    )


def support_command(
    receipt: GovernedClaimMaterializationReceiptV1,
) -> ReviewedSupportPromotionV1:
    return ReviewedSupportPromotionV1(
        materialization_receipt_sha256=receipt.receipt_sha256,
        claim_id=receipt.claim_id,
        assessment_review_request_id=ASSESSMENT_REVIEW_REQUEST,
        assessment_review_id=ASSESSMENT_REVIEW,
        assessment_review_manifest_sha256=HASH_C,
        assessment_apply_request_id=ASSESSMENT_APPLY_REQUEST,
        assessment_apply_manifest_sha256=HASH_D,
    )


class FakeDurableState:
    def __init__(self) -> None:
        self.materialized = False
        self.supported = False


class FakeTransaction:
    def __init__(self, conn: "FakeConnection", kwargs: dict[str, object]) -> None:
        self.conn = conn
        self.kwargs = kwargs

    async def __aenter__(self) -> object:
        if self.conn._in_transaction:
            raise AssertionError("test fake does not permit nested transactions")
        self.conn._in_transaction = True
        self.conn.events.append(("transaction_enter", self.kwargs))
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> object:
        if exc_type is not None:
            self.conn.events.append(("transaction_rollback", ()))
            self.conn._discard_pending()
            self.conn._in_transaction = False
            return False
        self.conn._commit_pending()
        if self.conn.commit_failure:
            self.conn.events.append(("transaction_commit_failure", ()))
            self.conn._in_transaction = False
            raise RuntimeError("synthetic secret-like commit failure")
        self.conn.events.append(("transaction_commit", ()))
        self.conn._in_transaction = False
        return False


class FakeBinder:
    def __init__(
        self,
        *,
        owner: UUID = OWNER_A,
        authentication_manifest_sha256: str = HASH_E,
        fail: bool = False,
        bind_other_owner: bool = False,
    ) -> None:
        self.owner = owner
        self.authentication_manifest_sha256 = authentication_manifest_sha256
        self.fail = fail
        self.bind_other_owner = bind_other_owner
        self.calls = 0

    async def bind_verified_actor(
        self,
        conn: "FakeConnection",
    ) -> VerifiedActorContextV1:
        self.calls += 1
        if self.fail:
            raise RuntimeError("postgres://user:secret@example.invalid/private")
        bound = OWNER_B if self.bind_other_owner else self.owner
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(bound))
        return VerifiedActorContextV1(
            owner_user_id=self.owner,
            authentication_manifest_sha256=self.authentication_manifest_sha256,
        )


class FakeConnection:
    def __init__(
        self,
        *,
        already_in_transaction: bool = False,
        session_user: str = "brains_app",
        commit_failure: bool = False,
        reviewer_type: str = "user",
        reviewer_ref: str = str(OWNER_A),
        materialization_status: str | None = None,
        final_status: str = "supported",
        outbox_held: object = True,
        outbox_claimable: object = False,
        outbox_insert_value: object = OUTBOX,
        ambiguous_authority: bool = False,
        observation_count: object = 2,
        authority_owner: UUID = OWNER_A,
        durable_state: FakeDurableState | None = None,
    ) -> None:
        self._in_transaction = already_in_transaction
        self.session_user = session_user
        self.commit_failure = commit_failure
        self.reviewer_type = reviewer_type
        self.reviewer_ref = reviewer_ref
        self.materialization_status = materialization_status
        self.final_status = final_status
        self.outbox_held = outbox_held
        self.outbox_claimable = outbox_claimable
        self.outbox_insert_value = outbox_insert_value
        self.ambiguous_authority = ambiguous_authority
        self.observation_count = observation_count
        self.authority_owner = authority_owner
        self.durable_state = durable_state or FakeDurableState()
        self.bound_actor: UUID | None = None
        self._pending_materialized = False
        self._pending_supported = False
        self.events: list[tuple[str, object]] = []

    def _discard_pending(self) -> None:
        self._pending_materialized = False
        self._pending_supported = False

    def _commit_pending(self) -> None:
        if self._pending_materialized:
            self.durable_state.materialized = True
        if self._pending_supported:
            self.durable_state.materialized = True
            self.durable_state.supported = True
        self._discard_pending()

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    def transaction(self, **kwargs: object) -> FakeTransaction:
        return FakeTransaction(self, dict(kwargs))

    async def execute(self, query: str, *args: object) -> object:
        if "set_config('app.user_id'" in query:
            self.bound_actor = UUID(str(args[0]))
            self.events.append(("actor_bound", args))
            return "SELECT 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        if query == "SELECT session_user":
            self.events.append(("session_user", ()))
            return self.session_user
        if query == "SELECT memory.current_actor_user_id()":
            self.events.append(("current_actor", ()))
            return self.bound_actor
        if "held_outbox_insert" in query:
            self.events.append(("held_outbox_insert", args))
            if self.outbox_insert_value is not None:
                self._pending_supported = True
            return self.outbox_insert_value
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "apply_projection_v5" in query:
            self.events.append(("projection_apply", args))
            self._pending_materialized = True
            return {
                "apply_event_id": PROJECTION_EVENT,
                "outcome": "applied",
                "lane": "claim",
                "aggregate_id": CLAIM,
                "revision_number": 1,
                "rows_written": 4 + int(self.observation_count),
            }
        if "apply_claim_assessment_v5" in query:
            self.events.append(("assessment_apply", args))
            return {
                "event_id": ASSESSMENT_APPLY_EVENT,
                "outcome": "applied",
                "assessment_id": ASSESSMENT,
                "resulting_revision_number": 2,
                "rows_written": 5,
            }
        raise AssertionError(f"unexpected fetchrow: {query}")

    def _materialization_row(self) -> dict[str, object]:
        status = self.materialization_status or (
            "supported" if self.durable_state.supported else "candidate"
        )
        return {
            "contract_version": (
                "memory_v1_governed_claim_materialization_authority_v1"
            ),
            "owner_user_id": self.authority_owner,
            "claim_id": CLAIM,
            "claim_status": status,
            "current_revision_number": 2 if status == "supported" else 1,
            "projection_request_id": PROJECTION_REQUEST,
            "plan_id": PLAN,
            "projection_ref": "p01",
            "projection_review_id": PROJECTION_REVIEW,
            "projection_apply_event_id": PROJECTION_EVENT,
            "projection_apply_manifest_sha256": HASH_A,
            "observation_count": self.observation_count,
            "observation_authority_sha256": HASH_B,
        }

    def _support_review_row(self) -> dict[str, object]:
        return {
            "contract_version": (
                "memory_v1_governed_claim_assessment_apply_ready_v1"
            ),
            "owner_user_id": self.authority_owner,
            "claim_id": CLAIM,
            "claim_status": "candidate",
            "claim_revision_number": 1,
            "projection_request_id": PROJECTION_REQUEST,
            "plan_id": PLAN,
            "projection_ref": "p01",
            "projection_review_id": PROJECTION_REVIEW,
            "projection_apply_event_id": PROJECTION_EVENT,
            "projection_apply_manifest_sha256": HASH_A,
            "observation_count": self.observation_count,
            "observation_authority_sha256": HASH_B,
            "assessment_review_request_id": ASSESSMENT_REVIEW_REQUEST,
            "assessment_review_id": ASSESSMENT_REVIEW,
            "assessment_review_manifest_sha256": HASH_C,
            "assessment_apply_manifest_sha256": HASH_D,
            "reviewer_type": self.reviewer_type,
            "reviewer_ref": self.reviewer_ref,
            "action": "promote_supported",
            "expected_from_status": "candidate",
            "target_status": "supported",
        }

    def _supported_row(self) -> dict[str, object]:
        return {
            "contract_version": "memory_v1_governed_claim_supported_held_v1",
            "owner_user_id": self.authority_owner,
            "claim_id": CLAIM,
            "claim_status": self.final_status,
            "claim_revision_number": 2,
            "projection_request_id": PROJECTION_REQUEST,
            "plan_id": PLAN,
            "projection_ref": "p01",
            "projection_review_id": PROJECTION_REVIEW,
            "projection_apply_event_id": PROJECTION_EVENT,
            "projection_apply_manifest_sha256": HASH_A,
            "observation_count": self.observation_count,
            "observation_authority_sha256": HASH_B,
            "assessment_review_request_id": ASSESSMENT_REVIEW_REQUEST,
            "assessment_review_id": ASSESSMENT_REVIEW,
            "assessment_review_manifest_sha256": HASH_C,
            "assessment_apply_request_id": ASSESSMENT_APPLY_REQUEST,
            "assessment_apply_event_id": ASSESSMENT_APPLY_EVENT,
            "assessment_id": ASSESSMENT,
            "assessment_apply_manifest_sha256": HASH_D,
            "outbox_id": OUTBOX,
            "projection_eligibility_held": self.outbox_held,
            "projection_worker_claimable": self.outbox_claimable,
        }

    def _projection_apply_ready_row(self) -> dict[str, object]:
        return {
            "contract_version": (
                "memory_v1_governed_claim_projection_apply_ready_v1"
            ),
            "owner_user_id": self.authority_owner,
            "plan_id": PLAN,
            "projection_ref": "p01",
            "lane": "claim",
            "target_action": "create",
            "review_state": "manual_review_required",
            "current_revision_number": 0,
            "projection_review_id": PROJECTION_REVIEW,
            "projection_review_manifest_sha256": HASH_C,
            "projection_apply_manifest_sha256": HASH_A,
            "observation_count": self.observation_count,
            "observation_authority_sha256": HASH_B,
        }

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        if "reconcile_materialization_request" in query:
            self.events.append(("reconcile_materialization_request", args))
            rows = (
                [
                    {
                        "claim_id": CLAIM,
                        "projection_apply_event_id": PROJECTION_EVENT,
                    }
                ]
                if self.durable_state.materialized
                else []
            )
        elif "reconcile_supported_request" in query:
            self.events.append(("reconcile_supported_request", args))
            rows = (
                [
                    {
                        "assessment_apply_event_id": ASSESSMENT_APPLY_EVENT,
                        "assessment_id": ASSESSMENT,
                        "outbox_id": OUTBOX,
                    }
                ]
                if self.durable_state.supported
                else []
            )
        elif "projection_apply_ready" in query:
            self.events.append(("projection_apply_ready", args))
            rows = [self._projection_apply_ready_row()]
        elif "materialization_authority" in query:
            self.events.append(("materialization_authority", args))
            rows = [self._materialization_row()]
        elif "support_review_authority" in query:
            self.events.append(("support_review_authority", args))
            rows = [self._support_review_row()]
        elif "supported_authority" in query:
            self.events.append(("supported_authority", args))
            rows = [self._supported_row()]
        else:
            raise AssertionError(f"unexpected fetch: {query}")
        return rows + rows[:1] if self.ambiguous_authority else rows


async def applied_materialization(
    *,
    binder: FakeBinder | None = None,
) -> GovernedClaimMaterializationResultV1:
    return await materialize_reviewed_claim_v1(
        FakeConnection(),
        binder or FakeBinder(),
        materialization_command(),
    )


class GovernedClaimTransitionV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_a_consumes_review_and_commits_before_receipt(self) -> None:
        conn = FakeConnection()
        result = await materialize_reviewed_claim_v1(
            conn,
            FakeBinder(),
            materialization_command(),
        )
        self.assertEqual(result.outcome, "applied")
        self.assertEqual(result.rows_written, 6)
        self.assertEqual(result.receipt.claim_revision_number, 1)
        self.assertEqual(result.receipt.observation_count, 2)
        self.assertEqual(conn.events[-1][0], "transaction_commit")
        self.assertFalse(conn.is_in_transaction())
        names = [name for name, _ in conn.events]
        self.assertIn("projection_apply_ready", names)
        self.assertNotIn("assessment_apply", names)
        self.assertNotIn("held_outbox_insert", names)

    async def test_stage_a_replay_is_read_only_zero_write(self) -> None:
        first = await applied_materialization(
            binder=FakeBinder(authentication_manifest_sha256=HASH_E)
        )
        conn = FakeConnection()
        result = await materialize_reviewed_claim_v1(
            conn,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialization_command(),
            prior_receipt=first.receipt,
        )
        self.assertEqual(result.outcome, "replayed")
        self.assertEqual(result.rows_written, 0)
        self.assertEqual(
            result.receipt.canonical_json_bytes(),
            first.receipt.canonical_json_bytes(),
        )
        self.assertEqual(
            result.receipt.actor_authentication_manifest_sha256,
            HASH_E,
        )
        enter = next(value for name, value in conn.events if name == "transaction_enter")
        self.assertEqual(enter, {"isolation": "serializable", "readonly": True})
        names = [name for name, _ in conn.events]
        self.assertNotIn("projection_apply", names)

    async def test_stage_a_receiptless_retry_reconciles_lost_commit_ack(self) -> None:
        durable = FakeDurableState()
        first = FakeConnection(durable_state=durable, commit_failure=True)
        with self.assertRaisesRegex(
            GovernedClaimTransitionError,
            "materialization transaction failed",
        ):
            await materialize_reviewed_claim_v1(
                first,
                FakeBinder(authentication_manifest_sha256=HASH_E),
                materialization_command(),
            )
        self.assertTrue(durable.materialized)

        retry = FakeConnection(durable_state=durable)
        result = await materialize_reviewed_claim_v1(
            retry,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialization_command(),
        )
        self.assertEqual(result.outcome, "replayed")
        self.assertEqual(result.rows_written, 0)
        self.assertEqual(
            result.receipt.actor_authentication_manifest_sha256,
            "f" * 64,
        )
        names = [name for name, _ in retry.events]
        self.assertIn("reconcile_materialization_request", names)
        self.assertIn("materialization_authority", names)
        self.assertNotIn("projection_apply_ready", names)
        self.assertNotIn("projection_apply", names)

    async def test_stage_b_consumes_separate_review_and_holds_index_event(self) -> None:
        materialized = await applied_materialization()
        conn = FakeConnection()
        result = await promote_independently_reviewed_claim_v1(
            conn,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialized.receipt,
            support_command(materialized.receipt),
        )
        self.assertEqual(result.outcome, "applied")
        self.assertEqual(result.rows_written, 6)
        self.assertEqual(result.derived_index_eligibility_events_created, 1)
        self.assertEqual(result.derived_index_eligibility_events_held, 1)
        self.assertEqual(result.qdrant_direct_writes, 0)
        self.assertEqual(result.automatic_promotions, 0)
        self.assertTrue(result.receipt.outbox_dispatch_held)
        self.assertEqual(conn.events[-1][0], "transaction_commit")
        names = [name for name, _ in conn.events]
        self.assertIn("support_review_authority", names)
        self.assertIn("assessment_apply", names)
        self.assertIn("held_outbox_insert", names)

    async def test_stage_b_replay_is_read_only_and_creates_nothing(self) -> None:
        materialized = await applied_materialization()
        first = await promote_independently_reviewed_claim_v1(
            FakeConnection(),
            FakeBinder(authentication_manifest_sha256=HASH_E),
            materialized.receipt,
            support_command(materialized.receipt),
        )
        conn = FakeConnection()
        replay = await promote_independently_reviewed_claim_v1(
            conn,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialized.receipt,
            support_command(materialized.receipt),
            prior_receipt=first.receipt,
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.rows_written, 0)
        self.assertEqual(replay.derived_index_eligibility_events_created, 0)
        self.assertEqual(
            replay.receipt.actor_authentication_manifest_sha256,
            HASH_E,
        )
        names = [name for name, _ in conn.events]
        self.assertNotIn("assessment_apply", names)
        self.assertNotIn("held_outbox_insert", names)

    async def test_stage_b_receiptless_retry_reconciles_lost_commit_ack(self) -> None:
        durable = FakeDurableState()
        materialized = await materialize_reviewed_claim_v1(
            FakeConnection(durable_state=durable),
            FakeBinder(),
            materialization_command(),
        )
        command = support_command(materialized.receipt)
        first = FakeConnection(durable_state=durable, commit_failure=True)
        with self.assertRaisesRegex(
            GovernedClaimTransitionError,
            "support transaction failed",
        ):
            await promote_independently_reviewed_claim_v1(
                first,
                FakeBinder(authentication_manifest_sha256=HASH_E),
                materialized.receipt,
                command,
            )
        self.assertTrue(durable.supported)

        retry = FakeConnection(durable_state=durable)
        result = await promote_independently_reviewed_claim_v1(
            retry,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialized.receipt,
            command,
        )
        self.assertEqual(result.outcome, "replayed")
        self.assertEqual(result.rows_written, 0)
        self.assertEqual(result.derived_index_eligibility_events_created, 0)
        self.assertEqual(
            result.receipt.actor_authentication_manifest_sha256,
            "f" * 64,
        )
        names = [name for name, _ in retry.events]
        self.assertIn("reconcile_supported_request", names)
        self.assertIn("supported_authority", names)
        self.assertNotIn("support_review_authority", names)
        self.assertNotIn("assessment_apply", names)
        self.assertNotIn("held_outbox_insert", names)

    async def test_stage_a_prior_receipt_replays_after_stage_b_support(self) -> None:
        durable = FakeDurableState()
        materialized = await materialize_reviewed_claim_v1(
            FakeConnection(durable_state=durable),
            FakeBinder(authentication_manifest_sha256=HASH_E),
            materialization_command(),
        )
        await promote_independently_reviewed_claim_v1(
            FakeConnection(durable_state=durable),
            FakeBinder(),
            materialized.receipt,
            support_command(materialized.receipt),
        )
        self.assertTrue(durable.supported)

        conn = FakeConnection(durable_state=durable)
        replay = await materialize_reviewed_claim_v1(
            conn,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialization_command(),
            prior_receipt=materialized.receipt,
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.rows_written, 0)
        self.assertEqual(
            replay.receipt.canonical_json_bytes(),
            materialized.receipt.canonical_json_bytes(),
        )
        enter = next(value for name, value in conn.events if name == "transaction_enter")
        self.assertEqual(enter, {"isolation": "serializable", "readonly": True})
        self.assertNotIn("projection_apply", [name for name, _ in conn.events])

    async def test_stage_a_receiptless_reconciles_after_stage_b_support(self) -> None:
        durable = FakeDurableState()
        materialized = await materialize_reviewed_claim_v1(
            FakeConnection(durable_state=durable),
            FakeBinder(authentication_manifest_sha256=HASH_E),
            materialization_command(),
        )
        await promote_independently_reviewed_claim_v1(
            FakeConnection(durable_state=durable),
            FakeBinder(),
            materialized.receipt,
            support_command(materialized.receipt),
        )

        conn = FakeConnection(durable_state=durable)
        replay = await materialize_reviewed_claim_v1(
            conn,
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialization_command(),
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.rows_written, 0)
        self.assertEqual(
            replay.receipt.actor_authentication_manifest_sha256,
            "f" * 64,
        )
        names = [name for name, _ in conn.events]
        self.assertIn("reconcile_materialization_request", names)
        self.assertIn("materialization_authority", names)
        self.assertNotIn("projection_apply_ready", names)
        self.assertNotIn("projection_apply", names)

    async def test_nested_transaction_is_rejected_before_actor_or_database(self) -> None:
        conn = FakeConnection(already_in_transaction=True)
        binder = FakeBinder()
        with self.assertRaisesRegex(GovernedClaimTransitionError, "top-level"):
            await materialize_reviewed_claim_v1(
                conn,
                binder,
                materialization_command(),
            )
        self.assertEqual(binder.calls, 0)
        self.assertEqual(conn.events, [])

    async def test_commit_failure_returns_no_receipt_and_context_does_not_leak(self) -> None:
        conn = FakeConnection(commit_failure=True)
        with self.assertRaisesRegex(
            GovernedClaimTransitionError,
            "materialization transaction failed",
        ) as caught:
            await materialize_reviewed_claim_v1(
                conn,
                FakeBinder(),
                materialization_command(),
            )
        self.assertFalse(conn.is_in_transaction())
        self.assertEqual(conn.events[-1][0], "transaction_commit_failure")
        self.assertNotIn("secret", str(caught.exception).lower())

    async def test_binding_failure_is_sanitized(self) -> None:
        conn = FakeConnection()
        with self.assertRaisesRegex(
            GovernedClaimTransitionError,
            "verified actor binding failed",
        ) as caught:
            await materialize_reviewed_claim_v1(
                conn,
                FakeBinder(fail=True),
                materialization_command(),
            )
        self.assertNotIn("postgres", str(caught.exception).lower())
        self.assertEqual(conn.events[-1][0], "transaction_rollback")

    async def test_command_cannot_select_owner_and_bound_actor_mismatch_fails(self) -> None:
        self.assertNotIn("owner_user_id", materialization_command().__dataclass_fields__)
        conn = FakeConnection()
        with self.assertRaisesRegex(GovernedClaimTransitionError, "differs"):
            await materialize_reviewed_claim_v1(
                conn,
                FakeBinder(bind_other_owner=True),
                materialization_command(),
            )
        self.assertNotIn("projection_apply", [name for name, _ in conn.events])

    async def test_cross_owner_authority_is_rejected_before_followup_writes(self) -> None:
        conn = FakeConnection(authority_owner=OWNER_B)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "owner_user_id drifted"):
            await materialize_reviewed_claim_v1(
                conn,
                FakeBinder(owner=OWNER_A),
                materialization_command(),
            )
        self.assertEqual(conn.events[-1][0], "transaction_rollback")

    async def test_system_assessment_review_is_never_consumed(self) -> None:
        materialized = await applied_materialization()
        conn = FakeConnection(reviewer_type="system")
        with self.assertRaisesRegex(GovernedClaimTransitionError, "owner"):
            await promote_independently_reviewed_claim_v1(
                conn,
                FakeBinder(),
                materialized.receipt,
                support_command(materialized.receipt),
            )
        names = [name for name, _ in conn.events]
        self.assertNotIn("assessment_apply", names)
        self.assertNotIn("held_outbox_insert", names)

    async def test_user_review_for_another_owner_is_never_consumed(self) -> None:
        materialized = await applied_materialization()
        conn = FakeConnection(reviewer_ref=str(OWNER_B))
        with self.assertRaisesRegex(GovernedClaimTransitionError, "owner"):
            await promote_independently_reviewed_claim_v1(
                conn,
                FakeBinder(),
                materialized.receipt,
                support_command(materialized.receipt),
            )
        names = [name for name, _ in conn.events]
        self.assertNotIn("assessment_apply", names)
        self.assertNotIn("held_outbox_insert", names)

    async def test_support_requires_same_owner_but_allows_fresh_authentication(self) -> None:
        materialized = await applied_materialization(
            binder=FakeBinder(authentication_manifest_sha256=HASH_E)
        )
        result = await promote_independently_reviewed_claim_v1(
            FakeConnection(),
            FakeBinder(authentication_manifest_sha256="f" * 64),
            materialized.receipt,
            support_command(materialized.receipt),
        )
        self.assertNotEqual(
            result.receipt.actor_authentication_manifest_sha256,
            materialized.receipt.actor_authentication_manifest_sha256,
        )

        conn = FakeConnection(authority_owner=OWNER_B)
        with self.assertRaises(GovernedClaimTransitionError):
            await promote_independently_reviewed_claim_v1(
                conn,
                FakeBinder(owner=OWNER_B),
                materialized.receipt,
                support_command(materialized.receipt),
            )
        self.assertNotIn("assessment_apply", [name for name, _ in conn.events])

    async def test_outbox_conflict_or_dispatchable_state_rolls_back_support(self) -> None:
        materialized = await applied_materialization()
        command = support_command(materialized.receipt)
        conflict = FakeConnection(outbox_insert_value=None)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "was not created"):
            await promote_independently_reviewed_claim_v1(
                conflict,
                FakeBinder(),
                materialized.receipt,
                command,
            )
        self.assertEqual(conflict.events[-1][0], "transaction_rollback")

        dispatchable = FakeConnection(outbox_held=False)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "not held"):
            await promote_independently_reviewed_claim_v1(
                dispatchable,
                FakeBinder(),
                materialized.receipt,
                command,
            )
        self.assertEqual(dispatchable.events[-1][0], "transaction_rollback")

        claimable = FakeConnection(outbox_claimable=True)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "claimable"):
            await promote_independently_reviewed_claim_v1(
                claimable,
                FakeBinder(),
                materialized.receipt,
                command,
            )
        self.assertEqual(claimable.events[-1][0], "transaction_rollback")

    async def test_ambiguous_or_tampered_authority_rolls_back(self) -> None:
        for conn, message in (
            (FakeConnection(ambiguous_authority=True), "ambiguous"),
            (FakeConnection(materialization_status="supported"), "status drifted"),
            (FakeConnection(observation_count=True), "integer"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(GovernedClaimTransitionError, message):
                    await materialize_reviewed_claim_v1(
                        conn,
                        FakeBinder(),
                        materialization_command(),
                    )
                self.assertEqual(conn.events[-1][0], "transaction_rollback")

    async def test_prior_receipts_must_bind_exact_commands(self) -> None:
        materialized = await applied_materialization()
        changed_materialization = dataclasses.replace(
            materialization_command(),
            projection_ref="p02",
        )
        conn = FakeConnection()
        with self.assertRaisesRegex(GovernedClaimTransitionError, "does not bind"):
            await materialize_reviewed_claim_v1(
                conn,
                FakeBinder(),
                changed_materialization,
                prior_receipt=materialized.receipt,
            )
        self.assertEqual(conn.events, [])

        changed_support = dataclasses.replace(
            support_command(materialized.receipt),
            assessment_review_manifest_sha256="0" * 64,
        )
        with self.assertRaisesRegex(GovernedClaimTransitionError, "drifted"):
            await promote_independently_reviewed_claim_v1(
                FakeConnection(),
                FakeBinder(),
                materialized.receipt,
                changed_support,
            )

    def test_commands_are_strict_hash_stable_and_have_no_owner(self) -> None:
        first = materialization_command()
        self.assertEqual(
            first.command_manifest_sha256,
            materialization_command().command_manifest_sha256,
        )
        self.assertNotIn("owner_user_id", first.material())
        with self.assertRaises(GovernedClaimTransitionError):
            dataclasses.replace(first, projection_ref="claim")
        with self.assertRaises(GovernedClaimTransitionError):
            dataclasses.replace(first, projection_apply_manifest_sha256="A" * 64)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "UUID"):
            dataclasses.replace(first, plan_id=str(PLAN))

    def test_receipt_tamper_and_integer_aliases_fail(self) -> None:
        receipt = GovernedClaimMaterializationReceiptV1.create(
            owner_user_id_sha256=HASH_A,
            actor_authentication_manifest_sha256=HASH_B,
            command_manifest_sha256=materialization_command().command_manifest_sha256,
            plan_id=PLAN,
            projection_ref="p01",
            projection_review_id=PROJECTION_REVIEW,
            projection_request_id=PROJECTION_REQUEST,
            claim_id=CLAIM,
            projection_apply_event_id=PROJECTION_EVENT,
            projection_apply_manifest_sha256=HASH_A,
            observation_count=2,
            observation_authority_sha256=HASH_E,
        )
        with self.assertRaisesRegex(GovernedClaimTransitionError, "receipt SHA"):
            dataclasses.replace(receipt, claim_id=OWNER_B)
        with self.assertRaisesRegex(GovernedClaimTransitionError, "integer"):
            dataclasses.replace(receipt, observation_count=True)

    def test_result_counters_cannot_hide_promotion_or_qdrant_writes(self) -> None:
        materialization = GovernedClaimMaterializationReceiptV1.create(
            owner_user_id_sha256=HASH_A,
            actor_authentication_manifest_sha256=HASH_B,
            command_manifest_sha256=materialization_command().command_manifest_sha256,
            plan_id=PLAN,
            projection_ref="p01",
            projection_review_id=PROJECTION_REVIEW,
            projection_request_id=PROJECTION_REQUEST,
            claim_id=CLAIM,
            projection_apply_event_id=PROJECTION_EVENT,
            projection_apply_manifest_sha256=HASH_A,
            observation_count=2,
            observation_authority_sha256=HASH_E,
        )
        support = GovernedClaimSupportReceiptV1.create(
            owner_user_id_sha256=HASH_A,
            actor_authentication_manifest_sha256=HASH_B,
            materialization_receipt_sha256=materialization.receipt_sha256,
            command_manifest_sha256=support_command(materialization).command_manifest_sha256,
            claim_id=CLAIM,
            projection_apply_event_id=PROJECTION_EVENT,
            projection_apply_manifest_sha256=HASH_A,
            observation_authority_sha256=HASH_E,
            assessment_review_request_id=ASSESSMENT_REVIEW_REQUEST,
            assessment_review_id=ASSESSMENT_REVIEW,
            assessment_review_manifest_sha256=HASH_C,
            assessment_apply_request_id=ASSESSMENT_APPLY_REQUEST,
            assessment_apply_event_id=ASSESSMENT_APPLY_EVENT,
            assessment_id=ASSESSMENT,
            assessment_apply_manifest_sha256=HASH_D,
            outbox_id=OUTBOX,
        )
        for kwargs in (
            {"qdrant_direct_writes": 1},
            {"automatic_promotions": 1},
            {"derived_index_eligibility_events_created": 0},
            {"derived_index_eligibility_events_held": 0},
            {"rows_written": True},
        ):
            with self.subTest(kwargs=kwargs):
                values = {"outcome": "applied", "rows_written": 6, "receipt": support}
                values.update(kwargs)
                with self.assertRaises(GovernedClaimTransitionError):
                    GovernedClaimSupportResultV1(**values)

    def test_runtime_has_no_self_review_provider_network_or_qdrant_client(self) -> None:
        import rag_engine.memory_v1_governed_claim_transition_v1 as module

        source = inspect.getsource(module)
        for forbidden in (
            "review_claim_assessment_v5(",
            "preflight_claim_assessment_review_v5(",
            "preflight_projection_apply_v5(",
            "import asyncpg",
            "import requests",
            "import httpx",
            "QdrantClient",
            "OpenAI",
            "ON CONFLICT",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("'infinity'::timestamptz", source)
        self.assertIn("derived_index_eligibility_events_held", source)

    def test_module_file_contains_no_command_selected_owner_field(self) -> None:
        source = Path(inspect.getsourcefile(materialize_reviewed_claim_v1) or "").read_text()
        command_block = source.split("class ReviewedProjectionMaterializationV1:", 1)[1]
        command_block = command_block.split("class GovernedClaimMaterializationReceiptV1:", 1)[0]
        self.assertNotIn("owner_user_id", command_block)
        self.assertIn("bind_verified_actor", source)


class CloneSocketContractV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.clone_path = Path(__file__).with_name(
            "memory_v1_governed_claim_transition_clone_v1.py"
        )
        cls.source = cls.clone_path.read_text()
        module = ast.parse(cls.source)
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name)
                and target.id == "_CLONE_SOCKET_PATH_PATTERN"
                for target in node.targets
            ):
                cls.pattern = ast.literal_eval(node.value)
                break
        else:
            raise AssertionError("clone socket path pattern is absent")

    def test_socket_path_contract_is_stable_exact_and_controller_independent(self) -> None:
        compiled = re.compile(self.pattern)
        accepted = (
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef01234567/child-work/socket"
        )
        self.assertIsNotNone(compiled.fullmatch(accepted))
        rejected = (
            "/tmp/memory-transition-clone.abc123/socket",
            "/home/ubuntu/phase6-postcommit-v25."
            "0123456789abcdef01234567/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v2."
            "0123456789abcdef01234567/child-work/socket",
            "/home/other/phase6-postcommit-v1."
            "0123456789abcdef01234567/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef0123456/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef012345678/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789ABCDEF01234567/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdeg01234567/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef01234567/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef01234567/extra/child-work/socket",
            "/home/ubuntu//phase6-postcommit-v1."
            "0123456789abcdef01234567/child-work/socket",
            "/home/ubuntu/phase6-postcommit-v1."
            "0123456789abcdef01234567/child-work/../socket",
            accepted + "/",
            accepted + ".other",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(compiled.fullmatch(value))

    def test_socket_filesystem_and_dsn_guards_execute_in_isolated_process(self) -> None:
        child = textwrap.dedent(
            """
            import importlib.util
            import os
            import sys
            import tempfile
            from pathlib import Path
            from unittest import mock
            from urllib.parse import quote

            def require(condition, message):
                if not condition:
                    raise AssertionError(message)


            source = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location("phase6_clone_socket_test", source)
            if spec is None or spec.loader is None:
                raise AssertionError("clone import spec is unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)

                direct = base / "direct" / "child-work" / "socket"
                direct.mkdir(parents=True)
                require(
                    module._clone_socket_components_are_safe(direct), "direct path rejected"
                )

                target_socket = base / "target-socket"
                target_socket.mkdir()
                socket_link = base / "socket-link" / "child-work" / "socket"
                socket_link.parent.mkdir(parents=True)
                socket_link.symlink_to(target_socket, target_is_directory=True)
                require(
                    not module._clone_socket_components_are_safe(socket_link),
                    "socket symlink accepted",
                )

                real_child = base / "real-child"
                (real_child / "socket").mkdir(parents=True)
                child_link = base / "child-link" / "child-work"
                child_link.parent.mkdir()
                child_link.symlink_to(real_child, target_is_directory=True)
                require(
                    not module._clone_socket_components_are_safe(child_link / "socket"),
                    "child-work symlink accepted",
                )

                real_root = base / "real-root"
                (real_root / "child-work" / "socket").mkdir(parents=True)
                root_link = base / "root-link"
                root_link.symlink_to(real_root, target_is_directory=True)
                require(
                    not module._clone_socket_components_are_safe(
                        root_link / "child-work" / "socket"
                    ),
                    "run-root symlink accepted",
                )

                nondirectory = base / "file-root" / "child-work" / "socket"
                nondirectory.parent.mkdir(parents=True)
                nondirectory.write_text("not a directory")
                require(
                    not module._clone_socket_components_are_safe(nondirectory),
                    "non-directory socket path accepted",
                )
                missing = base / "missing" / "child-work" / "socket"
                require(
                    not module._clone_socket_components_are_safe(missing),
                    "nonexistent socket path accepted",
                )

            socket_path = (
                "/home/ubuntu/phase6-postcommit-v1."
                "0123456789abcdef01234567/child-work/socket"
            )
            encoded = quote(socket_path, safe="")
            database = "memory_transition_clone_contract"
            valid = (
                f"postgresql://brains_app:secret@/{database}?host={encoded}"
            )
            environment = {
                "MEMORY_V1_TRANSITION_CLONE": "authorized",
                "MEMORY_V1_TRANSITION_CLONE_SOCKET": socket_path,
                "MEMORY_V1_TRANSITION_CLONE_DATABASE": database,
                "CLONE_DSN": valid,
            }
            invalid = (
                f"postgresql://brains_app:secret@/{database}?host=%2Fwrong",
                valid + f"&host={encoded}",
                valid + "&sslmode=disable",
                f"postgresql://brains_app:secret@localhost/{database}?host={encoded}",
                f"postgresql://brains_app:secret@localhost:5432/{database}?host={encoded}",
                f"postgresql://other:secret@/{database}?host={encoded}",
                f"postgresql://brains_app:@/{database}?host={encoded}",
                f"postgresql://brains_app:secret@/memory_transition_clone_other?host={encoded}",
                f"http://brains_app:secret@/{database}?host={encoded}",
            )
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                module, "_clone_socket_directory_is_safe", return_value=True
            ):
                require(
                    module._isolated_dsn("CLONE_DSN", "brains_app") == valid,
                    "valid exact Unix-socket DSN rejected",
                )
                for dsn in invalid:
                    os.environ["CLONE_DSN"] = dsn
                    try:
                        module._isolated_dsn("CLONE_DSN", "brains_app")
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError(f"invalid DSN accepted: {dsn}")
                os.environ["CLONE_DSN"] = valid
                del os.environ["MEMORY_V1_TRANSITION_CLONE"]
                try:
                    module._isolated_dsn("CLONE_DSN", "brains_app")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("missing clone authorization token accepted")
            """
        )
        child_environment = dict(os.environ)
        child_environment["PYTHONOPTIMIZE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.clone_path.resolve())],
            cwd=self.clone_path.parent.parent,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_socket_contract_keeps_exact_query_and_transport_guards(self) -> None:
        for required in (
            'query != {"host": [socket_path]}',
            "parsed.hostname is not None",
            "parsed.username != expected_username",
            "not parsed.password",
            "parsed.port is not None",
            "database != os.environ[\"MEMORY_V1_TRANSITION_CLONE_DATABASE\"]",
        ):
            self.assertIn(required, self.source)


@unittest.skip(
    "historical V24 stage-batch fixture is not part of the current production lineage"
)
class StageBatchAuthorityFixtureV24Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture_path = Path(__file__).with_name(
            "memory_v1_v5_2_stage_batch_fixture.py"
        )
        spec = importlib.util.spec_from_file_location(
            "phase6_stage_batch_fixture_v24",
            fixture_path,
        )
        if spec is None or spec.loader is None:
            raise AssertionError("stage fixture import spec is unavailable")
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.review = Path(self.temp.name) / "review"
        with contextlib.redirect_stdout(io.StringIO()):
            self.fixture.prepare(self.review)

    def reviewed_bundles(self) -> list[tuple[dict[str, Any], str]]:
        manifest = json.loads((self.review / "manifest.json").read_text())
        return [
            (json.loads(Path(item["path"]).read_text()), item["sha256"])
            for item in manifest["bundles"]
        ]

    def test_applied_manifest_is_three_substantive_uuid_bound_bundles(self) -> None:
        reviewed = self.reviewed_bundles()
        self.assertEqual(
            [bundle["case_id"] for bundle, _ in reviewed],
            list(self.fixture.APPLIED_CASES),
        )
        manifest = json.loads((self.review / "manifest.json").read_text())
        stance_manifest = next(
            item
            for item in manifest["bundles"]
            if Path(item["path"]).name == "owner-a-stance.json"
        )
        self.assertEqual(
            stance_manifest["expected_counts"],
            {
                "mentions": 1,
                "resolutions": 1,
                "candidates": 1,
                "observations": 2,
                "temporals": 2,
            },
        )
        for bundle, _ in reviewed:
            packet = json.loads(bundle["extraction_packet_text"])
            self.assertEqual(
                str(UUID(packet["source_envelope"]["job_id"])),
                packet["source_envelope"]["job_id"],
            )
            self.assertGreaterEqual(
                len(packet["entity_mentions"])
                + len(packet["observations"])
                + len(packet["comparison_hints"]),
                1,
            )
            self.assertEqual(packet["deferrals"], [])
        stance_bundle = next(
            bundle for bundle, _ in reviewed
            if bundle["case_id"] == "clone-owner-a-stance"
        )
        stance_packet = json.loads(stance_bundle["extraction_packet_text"])
        observations = stance_packet["observations"]
        self.assertEqual(
            [observation["observation_ref"] for observation in observations],
            ["o01", "o02"],
        )
        self.assertEqual(
            [observation["predicate"] for observation in observations],
            ["stance.reported", "stance.reported"],
        )
        self.assertEqual(
            [
                observation["predicate_registry_status"]
                for observation in observations
            ],
            ["governed", "governed"],
        )
        self.assertEqual(
            observations[0]["object"]["value"],
            {
                "topic_key": "evidence.expert_consensus",
                "topic_text": "expert consensus as evidence",
                "position": (
                    "expert consensus should be treated as evidence, "
                    "not absolute fact"
                ),
                "orientation": "supports",
                "context": "epistemic evaluation",
            },
        )
        self.assertEqual(
            observations[1]["object"]["value"],
            {
                "topic_key": "evidence.expert_consensus_as_absolute_fact",
                "topic_text": "expert consensus as absolute fact",
                "position": (
                    "expert consensus should not be treated as absolute fact"
                ),
                "orientation": "opposes",
                "context": "epistemic evaluation",
            },
        )
        content = (
            "I think expert consensus should be treated as evidence, "
            "not absolute fact."
        )
        expected_source_spans = [
            {
                "start": 0,
                "end": len(content),
                "span_sha256": self.fixture.digest(content),
            }
        ]
        self.assertEqual(
            [observation["source_spans"] for observation in observations],
            [expected_source_spans, expected_source_spans],
        )
        projections = []
        for index, observation in enumerate(observations, start=1):
            literal = observation["object"]
            projections.append(
                build_projection(
                    str(OWNER_A),
                    {
                        "owner_user_id": str(OWNER_A),
                        "predicate_registry_version": (
                            "memory_predicate_registry_v5_2"
                        ),
                        "evidence_status": "active",
                        "subject_entity_id": self.fixture.SELF_A,
                        "subject_entity_type": "self",
                        "subject_entity_status": "active",
                        "predicate": observation["predicate"],
                        "object_kind": "literal",
                        "object_literal": literal,
                        "object_literal_sha256": self.fixture.digest(
                            self.fixture.stable_json(literal)
                        ),
                        "polarity": observation["polarity"],
                        "modality": observation["modality"],
                        "projection_class": observation["projection_class"],
                        "surface_policy": observation["surface_policy"],
                        "project_scope": observation["project_scope"],
                        "temporal": observation["temporal"],
                        "observation_id": (
                            f"a3100000-0000-4000-8000-{index:012d}"
                        ),
                        "observation_sha256": str(index) * 64,
                    },
                )
            )
        self.assertEqual(
            [
                projection["identity"]["object_literal_sha256"]
                for projection in projections
            ],
            [
                "a94d87d3da5d6978cbfee13371a8458c23339d934f7ef6d004761f03ab6bc23f",
                "7d8edfbeabdf8d3dc9d3e26ad34b4e204fdb058f6145f504938c51d8ddabe631",
            ],
        )
        self.assertEqual(
            len(
                {
                    projection["identity"]["semantic_key_sha256"]
                    for projection in projections
                }
            ),
            2,
        )

    def test_projection_clone_uses_distinct_bound_sources_without_table_access(
        self,
    ) -> None:
        helper_path = Path(__file__).with_name(
            "memory_v1_v5_2_projection_dispatch_clone.py"
        )
        source = helper_path.read_text()
        ast.parse(source)
        self.assertIn(
            'os.environ["V5_2_SYSTEM_STANCE_OBSERVATION_ID"]', source
        )
        self.assertIn('"relation": "system_stance"', source)
        self.assertIn(
            '"system_stance": uuid.UUID("a4000000-0000-4000-8000-000000000003")',
            source,
        )
        self.assertIn("clone observation bindings must be distinct", source)
        self.assertEqual(
            source.count("await source_for_observation(conn, observation_id)"),
            1,
        )
        self.assertIn('sources[source_name]["observation_sha256"]', source)
        self.assertIn("memory.preflight_projection_source_v5_2", source)
        self.assertNotIn("FROM memory.observation", source)
        self.assertNotIn('packet["projector_version"] =', source)
        self.assertNotIn("_relation_fixture", source)
        plan_loop = source[
            source.index("for name, source_name in plan_sources.items():") :
            source.index("await actor(conn, OWNER_B)")
        ]
        constraint_reset = 'await conn.execute("SET CONSTRAINTS ALL DEFERRED")'
        self.assertEqual(plan_loop.count(constraint_reset), 1)
        self.assertLess(
            plan_loop.index("result = await conn.fetchrow("),
            plan_loop.index(constraint_reset),
        )
        self.assertLess(
            plan_loop.index(constraint_reset),
            plan_loop.index('if result is None or result["outcome"] != "applied"'),
        )
        self.assertLess(
            plan_loop.index(constraint_reset),
            plan_loop.index("replay = await conn.fetchrow("),
        )

    def test_empty_forged_and_cross_owner_cases_are_negative_manifests(self) -> None:
        main = (self.review / "manifest.json").read_text()
        self.assertNotIn("owner-a-empty.json", main)
        self.assertNotIn("owner-a-forged-component.json", main)
        self.assertNotIn("owner-b-empty.json", main)
        negatives = {
            "empty-manifest.json": "owner-a-empty.json",
            "forged-component-manifest.json": "owner-a-forged-component.json",
            "cross-owner-manifest.json": "owner-b-empty.json",
        }
        for manifest_name, bundle_name in negatives.items():
            manifest = json.loads((self.review / manifest_name).read_text())
            self.assertEqual(len(manifest["bundles"]), 1)
            self.assertEqual(Path(manifest["bundles"][0]["path"]).name, bundle_name)

    def test_authority_sql_is_exact_owner_only_and_content_bound(self) -> None:
        path = self.review / "authority-seed.sql"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        sql = path.read_text()
        self.assertEqual(sql.count("INSERT INTO memory.evidence_intake_terminal("), 3)
        self.assertEqual(sql.count("INSERT INTO memory.evidence_extraction_job("), 3)
        self.assertEqual(
            sql.count("INSERT INTO memory.evidence_extraction_packet_v5_local("), 3
        )
        self.assertEqual(
            sql.count("INSERT INTO memory.v5_2_local_packet_route_event("), 3
        )
        self.assertEqual(sql.count("$packet$::jsonb,true,0,0,"), 3)
        assertion_prefix, verification_tail = sql.split("DO $verify_1$", 1)
        self.assertEqual(
            assertion_prefix.count(
                "packet.normalized_packet IS NOT DISTINCT FROM $packet$"
            ),
            3,
        )
        self.assertEqual(assertion_prefix.count("SELECT 1 / CASE WHEN ("), 3)
        self.assertEqual(
            assertion_prefix.count(
                "packet.packet_storage_sha256=encode(public.digest("
            ),
            3,
        )
        self.assertNotIn("$packet$", verification_tail)
        for bundle, bundle_sha in self.reviewed_bundles():
            self.assertIn(bundle_sha, sql)
            self.assertIn(bundle["source_report"]["sha256"], sql)
            self.assertIn(bundle["extraction_packet_sha256"], sql)
            self.assertIn(bundle["extraction_packet_text"], sql)
        for forbidden in (
            "CREATE TABLE",
            "ALTER TABLE",
            "DROP ",
            "DISABLE TRIGGER",
            "UPDATE memory.",
            "DELETE FROM memory.",
        ):
            self.assertNotIn(forbidden, sql)

    def test_authority_renderer_rejects_injected_or_noncanonical_input(self) -> None:
        reviewed = self.reviewed_bundles()
        wrong_identifier = copy.deepcopy(reviewed)
        wrong_identifier[0][0]["evidence_id"] += "'"
        with self.assertRaisesRegex(RuntimeError, "canonical UUID"):
            self.fixture.authority_seed_sql(wrong_identifier)

        wrong_hash = copy.deepcopy(reviewed)
        wrong_hash[0] = (wrong_hash[0][0], "0" * 63 + "'")
        with self.assertRaisesRegex(RuntimeError, "lowercase SHA-256"):
            self.fixture.authority_seed_sql(wrong_hash)

        wrong_packet = copy.deepcopy(reviewed)
        extraction = json.loads(wrong_packet[0][0]["extraction_packet_text"])
        extraction["packet_findings"] = ["$packet$"]
        text = self.fixture.stable_json(extraction)
        wrong_packet[0][0]["extraction_packet_text"] = text
        wrong_packet[0][0]["extraction_packet_sha256"] = self.fixture.digest(text)
        with self.assertRaisesRegex(RuntimeError, "SQL delimiter"):
            self.fixture.authority_seed_sql(wrong_packet)

    def test_authority_write_is_no_clobber_cleanup_safe_and_stable(self) -> None:
        path = Path(self.temp.name) / "no-clobber.sql"
        self.fixture.secure_write_bytes(path, b"first\n")
        with self.assertRaises(FileExistsError):
            self.fixture.secure_write_bytes(path, b"second\n")
        self.assertEqual(path.read_bytes(), b"first\n")

        failing = Path(self.temp.name) / "failed.sql"
        with mock.patch.object(self.fixture.os, "fsync", side_effect=OSError("fsync")):
            with self.assertRaisesRegex(OSError, "fsync"):
                self.fixture.secure_write_bytes(failing, b"partial\n")
        self.assertFalse(failing.exists())
        for case_id in self.fixture.APPLIED_CASES:
            first = self.fixture.authority_uuid(case_id, "packet")
            self.assertEqual(first, self.fixture.authority_uuid(case_id, "packet"))
            self.assertEqual(str(UUID(first)), first)


class TransitionRuntimeSQLSafetyTest(unittest.TestCase):
    def test_held_outbox_claim_parameter_is_explicitly_uuid_typed(self) -> None:
        runtime = (
            Path(__file__).resolve().parents[1]
            / "rag_engine"
            / "memory_v1_governed_claim_transition_v1.py"
        ).read_text()
        self.assertIn("$1,'claim',$2::uuid,'upsert'", runtime)
        self.assertIn("'claim_id',$2::uuid,'revision_number',2", runtime)


if __name__ == "__main__":
    unittest.main()
