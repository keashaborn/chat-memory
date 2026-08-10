from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
import unittest
from uuid import UUID

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.http_store import (
    OwnerStoreError,
    PostgresOwnerStore,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
CLAIM_A = UUID("22222222-2222-4222-8222-222222222222")
PROPOSAL_A = UUID("33333333-3333-4333-8333-333333333333")
OPERATION_A = UUID("44444444-4444-4444-8444-444444444444")
AUTH_SHA256 = "a" * 64


def actor(scope: ActorScope) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER_A,
        actor_id=OWNER_A,
        role=ActorRole.OWNER,
        scopes=(scope,),
        authentication_manifest_sha256=AUTH_SHA256,
        authenticated_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.transaction_entries += 1

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.connection.transaction_exits.append(exc_type)


class _Acquire:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_Connection":
        self.connection.acquire_entries += 1
        return self.connection

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.connection.acquire_exits.append(exc_type)


class _Connection:
    def __init__(self) -> None:
        self.acquire_entries = 0
        self.acquire_exits: list[Any] = []
        self.transaction_entries = 0
        self.transaction_exits: list[Any] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.bound_owner: UUID = OWNER_A
        self.error: Exception | None = None

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_calls.append((query, args))
        return self.bound_owner

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.fetchrow_calls.append((query, args))
        if self.error is not None:
            raise self.error
        if "read_status" in query:
            return {
                "active_claims": 1,
                "pending_proposals": 2,
                "pending_projection": 0,
                "failed_projection": 0,
                "last_transition_at": None,
            }
        if "review_proposal" in query:
            return {
                "outcome": args[2],
                "claim_id": CLAIM_A,
                "revision_id": UUID("55555555-5555-4555-8555-555555555555"),
                "outbox_id": UUID("66666666-6666-4666-8666-666666666666"),
            }
        if "correct_claim" in query:
            return {
                "outcome": "pending_review",
                "proposal_id": PROPOSAL_A,
                "proposal_sha256": "b" * 64,
                "review_operation_id": OPERATION_A,
            }
        return {"outcome": "requested", "outbox_id": PROPOSAL_A}

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((query, args))
        if "list_claims" in query:
            if args[0] not in (None, CLAIM_A):
                return []
            return [
                {
                    "claim_id": CLAIM_A,
                    "lifecycle_state": "active",
                    "revision_sha256": "c" * 64,
                }
            ]
        if "list_proposals" in query:
            return [{"proposal_id": PROPOSAL_A, "proposal_sha256": "d" * 64}]
        if "read_operation" in query:
            return [
                {
                    "operation_id": OPERATION_A,
                    "transition_code": "claim_retracted",
                }
            ]
        return []


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _HungAcquire:
    async def __aenter__(self) -> None:
        await asyncio.Event().wait()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _HungPool:
    def acquire(self) -> _HungAcquire:
        return _HungAcquire()


class _DatabaseFailure(RuntimeError):
    sqlstate = "42501"


class PostgresOwnerStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = _Connection()
        self.store = PostgresOwnerStore(_Pool(self.connection))

    def assert_owner_context(self) -> None:
        self.assertEqual(self.connection.acquire_entries, 1)
        self.assertEqual(self.connection.transaction_entries, 1)
        self.assertEqual(self.connection.acquire_exits, [None])
        self.assertEqual(self.connection.transaction_exits, [None])
        self.assertEqual(
            self.connection.execute_calls,
            [
                ("SET LOCAL statement_timeout = '5s'", ()),
                ("SET LOCAL lock_timeout = '2s'", ()),
                (
                    "SELECT pg_catalog.set_config('app.user_id',$1::text,true)",
                    (str(OWNER_A),),
                ),
                (
                    "SELECT pg_catalog.set_config("
                    "'app.auth_context_sha256',$1::text,true)",
                    (AUTH_SHA256,),
                ),
            ],
        )
        self.assertEqual(
            self.connection.fetchval_calls,
            [("SELECT memory_private.current_owner_id()", ())],
        )

    async def test_status_binds_verified_owner_inside_short_transaction(self) -> None:
        result = await self.store.status(actor(ActorScope.READ_CLAIMS))
        self.assertEqual(result["active_claims"], 1)
        self.assert_owner_context()

    async def test_scope_is_checked_before_pool_acquisition(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "actor_scope_denied"):
            await self.store.status(actor(ActorScope.MUTATE_CLAIMS))
        self.assertEqual(self.connection.acquire_entries, 0)

    async def test_database_owner_mismatch_fails_closed(self) -> None:
        self.connection.bound_owner = UUID(
            "99999999-9999-4999-8999-999999999999"
        )
        with self.assertRaisesRegex(
            OwnerStoreError, "database_owner_context_mismatch"
        ):
            await self.store.list_claims(actor(ActorScope.READ_CLAIMS))
        self.assertEqual(self.connection.transaction_exits, [OwnerStoreError])

    async def test_missing_or_malformed_database_owner_is_server_failure(self) -> None:
        for bound_owner in (None, "not-a-database-uuid"):
            with self.subTest(bound_owner=bound_owner):
                connection = _Connection()
                connection.bound_owner = bound_owner  # type: ignore[assignment]
                store = PostgresOwnerStore(_Pool(connection))
                with self.assertRaisesRegex(
                    OwnerStoreError, "database_unavailable"
                ):
                    await store.status(actor(ActorScope.READ_CLAIMS))
                self.assertEqual(connection.transaction_exits, [OwnerStoreError])

    async def test_pool_acquisition_is_bounded_by_operation_timeout(self) -> None:
        store = PostgresOwnerStore(
            _HungPool(),
            operation_timeout_seconds=0.01,
        )
        with self.assertRaisesRegex(OwnerStoreError, "database_unavailable"):
            await store.status(actor(ActorScope.READ_CLAIMS))

    async def test_claim_reads_are_closed_and_owner_is_not_an_argument(self) -> None:
        claims = await self.store.list_claims(actor(ActorScope.READ_CLAIMS))
        claim = await self.store.get_claim(
            actor(ActorScope.READ_CLAIMS), CLAIM_A
        )
        self.assertEqual(claims[0]["claim_id"], str(CLAIM_A))
        self.assertEqual(claim["claim_id"], str(CLAIM_A))
        first_query, first_args = self.connection.fetch_calls[0]
        self.assertIn("memory_private.list_claims", first_query)
        self.assertEqual(first_args, (None, 100))
        self.assertNotIn(str(OWNER_A), first_args)

    async def test_review_uses_exact_static_procedure_and_no_owner_argument(self) -> None:
        body = {
            "decision": "admit",
            "expected_predicate_catalog_sha256": "1" * 64,
            "expected_proposal_sha256": "2" * 64,
            "expected_selected_sha256": "3" * 64,
            "expected_selection_binding_sha256": "4" * 64,
            "expected_source_sha256": "5" * 64,
            "operation_id": str(OPERATION_A),
            "reason_codes": ["explicit_owner_review"],
        }
        result = await self.store.review_proposal(
            actor(ActorScope.REVIEW_PROPOSALS), PROPOSAL_A, body
        )
        query, arguments = self.connection.fetchrow_calls[-1]
        self.assertIn("memory_private.review_proposal", query)
        self.assertEqual(result["claim_id"], str(CLAIM_A))
        self.assertEqual(result["outcome"], "admitted")
        self.assertEqual(arguments[0:3], (OPERATION_A, PROPOSAL_A, "admitted"))
        self.assertNotIn(OWNER_A, arguments)

    async def test_reject_review_maps_to_exact_database_decision(self) -> None:
        body = {
            "decision": "reject",
            "expected_predicate_catalog_sha256": "1" * 64,
            "expected_proposal_sha256": "2" * 64,
            "expected_selected_sha256": "3" * 64,
            "expected_selection_binding_sha256": "4" * 64,
            "expected_source_sha256": "5" * 64,
            "operation_id": str(OPERATION_A),
            "reason_codes": ["not_durable", "proposal_incorrect"],
        }
        result = await self.store.review_proposal(
            actor(ActorScope.REVIEW_PROPOSALS), PROPOSAL_A, body
        )
        _, arguments = self.connection.fetchrow_calls[-1]
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(arguments[0:3], (OPERATION_A, PROPOSAL_A, "rejected"))
        self.assertEqual(arguments[-1], ["not_durable", "proposal_incorrect"])

    async def test_correction_serializes_only_the_closed_replacement(self) -> None:
        body = {
            "operation_id": str(OPERATION_A),
            "expected_revision_sha256": "6" * 64,
            "expected_state_sha256": "7" * 64,
            "expected_predicate_catalog_sha256": "8" * 64,
            "replacement": {
                "epistemic_state": "supported",
                "object_display_name": None,
                "object_entity_type": None,
                "object_kind": "literal",
                "object_literal": "amber",
                "sensitivity": "ordinary",
            },
        }
        await self.store.correct_claim(
            actor(ActorScope.MUTATE_CLAIMS), CLAIM_A, body
        )
        query, arguments = self.connection.fetchrow_calls[-1]
        self.assertIn("memory_private.correct_claim", query)
        self.assertIn("$6::text::jsonb", query)
        self.assertEqual(
            arguments[-1],
            '{"epistemic_state":"supported","object_display_name":null,'
            '"object_entity_type":null,"object_kind":"literal",'
            '"object_literal":"amber","sensitivity":"ordinary"}',
        )

    async def test_unknown_database_failures_are_content_free(self) -> None:
        self.connection.error = _DatabaseFailure("sensitive database detail")
        with self.assertRaisesRegex(
            OwnerStoreError, "database_authority_denied"
        ) as captured:
            await self.store.status(actor(ActorScope.READ_CLAIMS))
        self.assertNotIn("sensitive", str(captured.exception))

    async def test_operation_receipt_is_owner_scoped_and_structured(self) -> None:
        result = await self.store.get_operation(
            actor(ActorScope.READ_CLAIMS), OPERATION_A
        )
        self.assertEqual(result["operation_id"], str(OPERATION_A))
        self.assertEqual(
            result["events"][0]["transition_code"], "claim_retracted"
        )


if __name__ == "__main__":
    unittest.main()
