from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

os.environ.setdefault(
    "POSTGRES_DSN",
    "postgresql://invalid:invalid@127.0.0.1:1/invalid",
)

from rag_engine import memory_v1_governed_claim_lifecycle_router_v1 as lifecycle


OWNER = UUID("11111111-1111-4111-8111-111111111111")
CLAIM = UUID("22222222-2222-4222-8222-222222222222")
OPERATION = UUID("33333333-3333-4333-8333-333333333333")
HASH = "a" * 64


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Connection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_query: str | None = None
        self.fetchrow_query: str | None = None
        self.fetch_args: tuple[object, ...] | None = None
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def transaction(self, **kwargs: object) -> _Transaction:
        self.executed.append(("transaction", (kwargs,)))
        return _Transaction()

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        return "SELECT 1"

    async def fetchval(self, query: str, *args: object) -> UUID:
        self.executed.append((query, args))
        return OWNER

    async def fetch(self, query: str, *args: object):
        self.fetch_query = query
        self.fetch_args = args
        return [
            {
                "claim_id": CLAIM,
                "status": "supported",
                "current_revision_number": 2,
                "canonical_text": "The owner prefers concise answers.",
                "predicate": "preference.response_style",
                "confidence": 0.9,
                "sensitivity": "low",
                "updated_at": None,
                "claim_state_sha256": HASH,
            }
        ]

    async def fetchrow(self, query: str, *args: object):
        self.fetchrow_query = query
        self.fetch_args = args
        self.fetchrow_calls.append((query, args))
        if "memory.record_owner_evidence_v1" in query:
            return {
                "evidence_id": UUID("66666666-6666-4666-8666-666666666666"),
                "outcome": "applied",
                "content_sha256": "b" * 64,
            }
        return {
            "outcome": "applied",
            "event_id": UUID("44444444-4444-4444-8444-444444444444"),
            "resulting_revision_number": 3,
            "outbox_id": UUID("55555555-5555-4555-8555-555555555555"),
            "outbox_dispatch_held": True,
        }

    async def close(self) -> None:
        self.closed = True


class GovernedClaimLifecycleRouterV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_list_uses_only_governed_read_function(self) -> None:
        conn = _Connection()
        with (
            patch.object(lifecycle, "DSN", "postgresql://clone"),
            patch.object(
                lifecycle,
                "require_memory_actor_v1",
                AsyncMock(return_value=str(OWNER)),
            ),
            patch.object(lifecycle.asyncpg, "connect", AsyncMock(return_value=conn)),
        ):
            response = await lifecycle.list_owner_governed_claims_v1(
                OWNER,
                SimpleNamespace(),
                25,
            )
        body = json.loads(response.body)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["claims"][0]["claim_id"], str(CLAIM))
        self.assertEqual(conn.fetch_args, (25,))
        self.assertIn(
            "memory.read_owner_governed_claim_lifecycle_v1",
            conn.fetch_query or "",
        )
        self.assertNotIn("memory.claim ", conn.fetch_query or "")
        self.assertTrue(conn.closed)
        self.assertIn("private, no-store", response.headers["cache-control"])

    async def test_retraction_derives_two_bound_operations_and_retains_history(self) -> None:
        conn = _Connection()
        value = lifecycle.GovernedClaimRetractionV1(
            operation_id=OPERATION,
            expected_revision_number=2,
            expected_claim_state_sha256=HASH,
            reason="This is no longer true.",
        )
        expected = lifecycle._operation_ids(OWNER, CLAIM, OPERATION)
        with (
            patch.object(lifecycle, "DSN", "postgresql://clone"),
            patch.object(
                lifecycle,
                "require_memory_actor_v1",
                AsyncMock(return_value=str(OWNER)),
            ),
            patch.object(lifecycle.asyncpg, "connect", AsyncMock(return_value=conn)),
        ):
            response = await lifecycle.retract_owner_governed_claim_v1(
                OWNER,
                CLAIM,
                value,
                SimpleNamespace(),
            )
        body = json.loads(response.body)
        self.assertEqual(conn.fetch_args[:2], expected)
        self.assertEqual(conn.fetch_args[2], CLAIM)
        self.assertIn(
            "memory.retract_owner_governed_claim_v1",
            conn.fetchrow_query or "",
        )
        self.assertNotIn("memory.claim ", conn.fetchrow_query or "")
        self.assertTrue(body["history_retained"])
        self.assertFalse(body["physical_claim_deleted"])
        self.assertTrue(body["outbox_dispatch_held"])

    async def test_correction_retracts_and_records_reviewable_evidence_atomically(self) -> None:
        conn = _Connection()
        value = lifecycle.GovernedClaimCorrectionV1(
            operation_id=OPERATION,
            expected_revision_number=2,
            expected_claim_state_sha256=HASH,
            reason="My preference changed.",
            replacement_text="  I now prefer detailed answers.  ",
        )
        expected = lifecycle._correction_operation_ids(OWNER, CLAIM, OPERATION)
        with (
            patch.object(lifecycle, "DSN", "postgresql://clone"),
            patch.object(
                lifecycle,
                "require_memory_actor_v1",
                AsyncMock(return_value=str(OWNER)),
            ),
            patch.object(lifecycle.asyncpg, "connect", AsyncMock(return_value=conn)),
        ):
            response = await lifecycle.correct_owner_governed_claim_v1(
                OWNER,
                CLAIM,
                value,
                SimpleNamespace(),
            )
        body = json.loads(response.body)
        self.assertEqual(len(conn.fetchrow_calls), 2)
        retract_query, retract_args = conn.fetchrow_calls[0]
        evidence_query, evidence_args = conn.fetchrow_calls[1]
        self.assertIn("memory.retract_owner_governed_claim_v1", retract_query)
        self.assertEqual(retract_args[:2], expected)
        self.assertIn("memory.record_owner_evidence_v1", evidence_query)
        self.assertIn("'high'::memory.sensitivity_level", evidence_query)
        self.assertEqual(
            evidence_args[:4],
            (
                "frontend/governed-claim-correction:user",
                f"governed-claim-correction:{CLAIM}:{OPERATION}",
                "I now prefer detailed answers.",
                f"governed-claim-correction:{CLAIM}",
            ),
        )
        metadata = json.loads(str(evidence_args[4]))
        self.assertEqual(metadata["corrected_claim_id"], str(CLAIM))
        self.assertTrue(metadata["replacement_requires_governed_review"])
        self.assertNotIn("I now prefer detailed answers.", metadata.values())
        self.assertEqual(
            body["replacement_status"],
            "evidence_recorded_pending_governed_review",
        )
        self.assertFalse(body["automatic_claim_promotion"])
        self.assertTrue(body["history_retained"])
        self.assertTrue(conn.closed)

    def test_model_fails_closed_on_extra_fields_and_bad_hash(self) -> None:
        with self.assertRaises(ValueError):
            lifecycle.GovernedClaimRetractionV1(
                operation_id=OPERATION,
                expected_revision_number=2,
                expected_claim_state_sha256="bad",
                reason="retract",
            )

        with self.assertRaises(ValueError):
            lifecycle.GovernedClaimCorrectionV1(
                operation_id=OPERATION,
                expected_revision_number=2,
                expected_claim_state_sha256=HASH,
                reason="correct",
                replacement_text="é" * 16_385,
            )
        with self.assertRaises(ValueError):
            lifecycle.GovernedClaimRetractionV1.model_validate(
                {
                    "operation_id": str(OPERATION),
                    "expected_revision_number": 2,
                    "expected_claim_state_sha256": HASH,
                    "reason": "retract",
                    "unexpected": True,
                }
            )

    def test_uuid_parser_accepts_only_canonical_lowercase_hyphenated_text(self) -> None:
        value = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        self.assertEqual(str(lifecycle._canonical_uuid(value)), value)
        for malformed in (value.upper(), value.replace("-", ""), "{" + value + "}", 1):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                lifecycle._canonical_uuid(malformed)

    def test_router_exposes_inspect_retract_and_semantic_delete_only(self) -> None:
        paths = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in lifecycle.router.routes
        }
        self.assertIn(("/{owner_user_id}", ("GET",)), paths)
        self.assertIn(("/{owner_user_id}/{claim_id}/retract", ("POST",)), paths)
        self.assertIn(("/{owner_user_id}/{claim_id}/correct", ("POST",)), paths)
        self.assertIn(("/{owner_user_id}/{claim_id}", ("DELETE",)), paths)


if __name__ == "__main__":
    unittest.main()
