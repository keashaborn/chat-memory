from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    selection_binding_sha256,
)
from rag_engine.governed_memory.postgres_adapter import EXTRACTION_LEASE_FIELDS
from rag_engine.governed_memory.projection import (
    EMBEDDING_MODEL,
    PROJECTION_CONTRACT_SHA256,
    RELATIONAL_RENDERER_SHA256,
)
from rag_engine.governed_memory.runtime.once_worker import (
    ExtractionWork,
    WorkKind,
    ProjectionDeleteWork,
    ProjectionUpsertWork,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    DispatchReceipt,
    EMBEDDING_ENDPOINT_SHA256,
    OpenAIOutcomeUnknownFailure,
    embedding_request_sha256,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
    QdrantUpsertReceipt,
)
from rag_engine.governed_memory.runtime.pilot_marker import (
    pilot_marker_receipt_sha256,
)
from rag_engine.governed_memory.runtime.worker_postgres import (
    PROJECTION_LEASE_FIELDS,
    WORKER_RUNTIME_CONTRACT_SHA256,
    PostgresOnceWorkerRepository,
)
from tests.memory._fixtures import (
    EVIDENCE_A,
    JOB_A,
    MESSAGE_A,
    NOW,
    OPERATION_A,
    OWNER_A,
    PREDICATE_CATALOG,
    PROVIDER_CALL_A,
    PROJECTION_A,
    REVISION_A,
    SOURCE_TEXT,
    THREAD_A,
    WINDOW_A,
    make_claim_row,
    make_projection_outbox,
    make_selected_evidence,
)


LEASE_TOKEN = UUID("10101010-1010-4010-8010-101010101010")
CONTEXT_MESSAGE = UUID("20202020-2020-4020-8020-202020202020")
PILOT_ID = "governed_memory_pilot_synthetic"
PILOT_CONTRACT_SHA256 = "a" * 64
AUTHORIZATION_RECEIPT_SHA256 = "b" * 64


class FakeTransaction:
    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        fetch_results: list[list[dict[str, object]]] | None = None,
        fetchrow_results: list[dict[str, object] | None] | None = None,
        fetchval_results: list[object] | None = None,
    ) -> None:
        self.fetch_results = deque(fetch_results or [])
        self.fetchrow_results = deque(fetchrow_results or [])
        self.fetchval_results = deque(fetchval_results or [])
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((sql, args))
        return self.fetch_results.popleft()

    async def fetchrow(
        self,
        sql: str,
        *args: object,
    ) -> dict[str, object] | None:
        self.fetchrow_calls.append((sql, args))
        return self.fetchrow_results.popleft()

    async def fetchval(self, sql: str, *args: object) -> object:
        if not self.fetchval_results:
            raise AssertionError(f"unexpected fetchval: {sql} {args!r}")
        self.fetch_calls.append((sql, args))
        return self.fetchval_results.popleft()

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()


def repository(connection: FakeConnection) -> PostgresOnceWorkerRepository:
    return PostgresOnceWorkerRepository(
        connection,
        worker_id="governed-memory-pilot-worker-1",
        extraction_model="synthetic-extraction-model",
        predicate_catalog=PREDICATE_CATALOG,
        expected_pilot_id=PILOT_ID,
        expected_pilot_contract_sha256=PILOT_CONTRACT_SHA256,
        expected_authorization_receipt_sha256=(
            AUTHORIZATION_RECEIPT_SHA256
        ),
    )


def projection_lease(*, operation: str) -> dict[str, object]:
    lifecycle = "active" if operation == "upsert" else "deletion_pending"
    sequence = 1 if operation == "upsert" else 2
    claim = make_claim_row(
        lifecycle_state=lifecycle,
        projection_sequence=sequence,
    )
    outbox = make_projection_outbox(claim, operation=operation)
    values: dict[str, object] = {
        "owner_user_id": UUID(str(claim["owner_user_id"])),
        "outbox_id": PROJECTION_A,
        "claim_id": UUID(str(claim["claim_id"])),
        "revision_id": UUID(str(claim["revision_id"])),
        "revision_number": claim["revision_number"],
        "operation_id": UUID(str(outbox["operation_id"])),
        "operation": operation,
        "sequence_number": outbox["sequence_number"],
        "point_id": UUID(str(claim["claim_id"])),
        "collection_alias": QDRANT_ALIAS,
        "revision_sha256": claim["revision_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "predicate_catalog_sha256": claim["predicate_catalog_sha256"],
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "dimensions": 3_072,
        "embedding_model": EMBEDDING_MODEL,
        "renderer_sha256": RELATIONAL_RENDERER_SHA256,
        "projection_manifest_sha256": outbox["projection_manifest_sha256"],
        "retrieval_text": (
            claim["retrieval_text"] if operation == "upsert" else None
        ),
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "embedding_input_sha256": claim["retrieval_text_sha256"],
        "source_sha256": claim["source_sha256"],
        "lifecycle_state": lifecycle,
        "is_current": True,
        "predicate": claim["predicate"] if operation == "upsert" else None,
        "epistemic_state": (
            claim["epistemic_state"] if operation == "upsert" else None
        ),
        "sensitivity": claim["sensitivity"] if operation == "upsert" else None,
        "domains": claim["domains"] if operation == "upsert" else [],
        "intents": claim["intents"] if operation == "upsert" else [],
        "surface": claim["surface"] if operation == "upsert" else None,
        "requires_explicit": (
            claim["requires_explicit"] if operation == "upsert" else False
        ),
        "projectable": claim["projectable"] if operation == "upsert" else False,
        "valid_from": claim["valid_from"],
        "valid_to": claim["valid_to"],
        "claim_updated_at": NOW,
        "state_sha256": claim["state_sha256"],
        "semantic_key_sha256": claim["semantic_key_sha256"],
        "claim_identity_sha256": claim["claim_identity_sha256"],
        "subject_entity_key": claim["subject_entity_key"],
        "subject_entity_type": claim["subject_entity_type"],
        "subject_display_name": claim["subject_display_name"],
        "object_kind": claim["object_kind"],
        "object_entity_key": claim["object_entity_key"],
        "object_entity_type": claim["object_entity_type"],
        "object_display_name": claim["object_display_name"],
        "object_literal": claim["object_literal"],
        "lease_token": LEASE_TOKEN,
        "lease_expires_at": NOW + timedelta(minutes=2),
    }
    return {field: values[field] for field in PROJECTION_LEASE_FIELDS}


def extraction_lease_with_context() -> dict[str, object]:
    evidence = make_selected_evidence()
    context_sha256 = sha256(b"Synthetic assistant question?").hexdigest()
    selection = selection_binding_sha256(
        owner_user_id=OWNER_A,
        source_kind="conversation_message",
        source_message_id=MESSAGE_A,
        source_thread_id=THREAD_A,
        source_window_id=WINDOW_A,
        window_sha256=str(evidence["window_sha256"]),
        source_sha256=str(evidence["source_sha256"]),
        selected_sha256=str(evidence["selected_sha256"]),
        start_utf8=0,
        end_utf8=len(SOURCE_TEXT.encode("utf-8")),
        context_message_id=CONTEXT_MESSAGE,
        context_sha256=context_sha256,
    )
    values: dict[str, object] = {
        "owner_user_id": OWNER_A,
        "job_id": JOB_A,
        "evidence_id": EVIDENCE_A,
        "source_kind": "conversation_message",
        "source_message_id": MESSAGE_A,
        "source_thread_id": THREAD_A,
        "source_window_id": WINDOW_A,
        "source_window_sha256": evidence["window_sha256"],
        "source_sha256": evidence["source_sha256"],
        "selected_sha256": evidence["selected_sha256"],
        "selection_binding_sha256": selection,
        "selected_start_utf8": 0,
        "selected_end_utf8": len(SOURCE_TEXT.encode("utf-8")),
        "context_message_id": CONTEXT_MESSAGE,
        "context_sha256": context_sha256,
        "review_excerpt": SOURCE_TEXT,
        "predicate_catalog_sha256": (
            "5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded"
        ),
        "attempt_number": 1,
        "lease_token": LEASE_TOKEN,
        "lease_expires_at": NOW + timedelta(minutes=2),
        "provider_call_id": PROVIDER_CALL_A,
        "provider_operation_id": OPERATION_A,
    }
    return {field: values[field] for field in EXTRACTION_LEASE_FIELDS}


def pilot_marker_row(
    *,
    pilot_id: str = PILOT_ID,
    pilot_contract_sha256: str = PILOT_CONTRACT_SHA256,
    authorization_receipt_sha256: str = AUTHORIZATION_RECEIPT_SHA256,
    started_at: datetime = NOW,
) -> dict[str, object]:
    operation_id = UUID("30303030-3030-4030-8030-303030303030")
    return {
        "pilot_ever_started": True,
        "pilot_id": pilot_id,
        "operation_id": operation_id,
        "pilot_contract_sha256": pilot_contract_sha256,
        "authorization_receipt_sha256": authorization_receipt_sha256,
        "started_at": started_at,
        "marker_receipt_sha256": pilot_marker_receipt_sha256(
            pilot_id=pilot_id,
            operation_id=operation_id,
            pilot_contract_sha256=pilot_contract_sha256,
            authorization_receipt_sha256=authorization_receipt_sha256,
            started_at=started_at,
        ),
    }


class ProjectionLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_automatic_admission_receipt(self) -> None:
        proposal_id = UUID("40404040-4040-4040-8040-404040404040")
        connection = FakeConnection(fetch_results=[[{
            "outcome": "admitted", "proposal_id": proposal_id,
            "claim_id": UUID("41414141-4141-4141-8141-414141414141"),
            "revision_id": UUID("42424242-4242-4242-8242-424242424242"),
            "outbox_id": UUID("43434343-4343-4343-8343-434343434343"),
        }]])
        receipt = await repository(connection).auto_admit_one_ordinary_proposal()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.work_kind, WorkKind.ADMISSION)
        self.assertEqual(receipt.work_id, proposal_id)
        self.assertRegex(receipt.receipt_sha256, r"^[0-9a-f]{64}$")

    async def test_bounded_automatic_admission_no_work(self) -> None:
        connection = FakeConnection(fetch_results=[[]])
        receipt = await repository(connection).auto_admit_one_ordinary_proposal()
        self.assertIsNone(receipt)

    async def test_embedding_dispatch_is_durable_and_exactly_bound(self) -> None:
        lease = projection_lease(operation="upsert")
        connection = FakeConnection(
            fetch_results=[[], [lease]],
            fetchrow_results=[{"outcome": "dispatched", "dispatched_at": NOW}],
        )
        store = repository(connection)
        work = await store.claim_one()
        assert isinstance(work, ProjectionUpsertWork)
        receipt = DispatchReceipt(
            operation="embeddings.create",
            model=EMBEDDING_MODEL,
            endpoint_sha256=EMBEDDING_ENDPOINT_SHA256,
            input_sha256=str(lease["embedding_input_sha256"]),
            request_body_sha256="d" * 64,
        )
        await store.mark_projection_embedding_dispatched(work, receipt)
        sql, arguments = connection.fetchrow_calls[0]
        self.assertIn("mark_projection_embedding_dispatched", sql)
        self.assertEqual(
            arguments,
            (
                lease["outbox_id"],
                lease["lease_token"],
                "embeddings.create",
                EMBEDDING_MODEL,
                EMBEDDING_ENDPOINT_SHA256,
                lease["embedding_input_sha256"],
                "d" * 64,
                embedding_request_sha256(receipt),
            ),
        )

    def test_unknown_embedding_dispatch_is_terminal_not_retryable(self) -> None:
        self.assertEqual(
            PostgresOnceWorkerRepository._projection_failure(
                OpenAIOutcomeUnknownFailure(
                    "provider_timeout_after_dispatch"
                )
            ),
            ("failed_terminal", "embedding_dispatch_outcome_unknown"),
        )

    async def test_upsert_normalizes_closed_authoritative_contract(self) -> None:
        connection = FakeConnection(fetch_results=[[], [projection_lease(operation="upsert")]])
        work = await repository(connection).claim_one()
        self.assertIsInstance(work, ProjectionUpsertWork)
        assert isinstance(work, ProjectionUpsertWork)
        self.assertEqual(work.claim["projection_sequence"], 1)
        self.assertEqual(work.claim["object_literal"], "cobalt")
        self.assertEqual(tuple(sorted(work.claim)), tuple(sorted(make_claim_row())))
        self.assertEqual(len(connection.fetch_calls), 2)
        self.assertIn("lease_extraction_jobs", connection.fetch_calls[0][0])
        self.assertIn("lease_projection_jobs", connection.fetch_calls[1][0])
        self.assertEqual(
            connection.fetch_calls[1][1],
            (
                "governed-memory-pilot-worker-1",
                120,
                WORKER_RUNTIME_CONTRACT_SHA256,
            ),
        )

    async def test_delete_does_not_normalize_null_upsert_surface(self) -> None:
        connection = FakeConnection(fetch_results=[[], [projection_lease(operation="delete")]])
        work = await repository(connection).claim_one()
        self.assertIsInstance(work, ProjectionDeleteWork)
        assert isinstance(work, ProjectionDeleteWork)
        self.assertEqual(work.delete_command["projection_sequence"], 2)
        self.assertEqual(
            work.delete_command["physical_collection"],
            QDRANT_PHYSICAL_COLLECTION,
        )

    async def test_applied_completion_accepts_exact_database_replay(self) -> None:
        connection = FakeConnection(
            fetch_results=[[], [projection_lease(operation="upsert")]],
            fetchrow_results=[{"outcome": "applied"}, {"outcome": "replayed"}],
        )
        store = repository(connection)
        work = await store.claim_one()
        assert isinstance(work, ProjectionUpsertWork)
        result = QdrantUpsertReceipt(
            payload_sha256="a" * 64,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=UUID(str(work.claim["claim_id"])),
            resolved_by_readback=False,
            vector_sha256="b" * 64,
            verification_receipt_sha256="c" * 64,
        )
        await store.complete(work, result)
        await store.complete(work, result)
        self.assertEqual(len(connection.fetchrow_calls), 2)
        self.assertEqual(
            connection.fetchrow_calls[0][1],
            connection.fetchrow_calls[1][1],
        )


class ProjectionLeaseMigrationContractTests(unittest.TestCase):
    def test_forward_sql_return_columns_match_repository_exactly(self) -> None:
        sql = Path(
            "governed-memory-migrations/0001_foundation/forward.pgsql"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"CREATE FUNCTION memory_private\.lease_projection_jobs\(.*?\)"
            r"\nRETURNS TABLE\((.*?)\)\nLANGUAGE plpgsql",
            sql,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        assert match is not None
        columns = tuple(
            line.strip().removesuffix(",").split()[0]
            for line in match.group(1).splitlines()
            if line.strip()
        )
        self.assertEqual(columns, PROJECTION_LEASE_FIELDS)


class PersistentLaneSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_lane_uses_one_exact_successor_rpc(self) -> None:
        connection = FakeConnection(fetchval_results=["projection"])
        lane = await repository(connection).next_worker_lane()
        self.assertEqual(lane, "projection")
        self.assertEqual(
            connection.fetch_calls,
            [("SELECT memory_private.next_worker_lane()", ())],
        )

    async def test_lane_claim_does_not_probe_the_other_downstream_lane(self) -> None:
        extraction_connection = FakeConnection(fetch_results=[[]])
        self.assertIsNone(
            await repository(extraction_connection).claim_one_for_lane(
                "extraction"
            )
        )
        self.assertEqual(len(extraction_connection.fetch_calls), 1)
        self.assertIn(
            "lease_extraction_jobs",
            extraction_connection.fetch_calls[0][0],
        )

        projection_connection = FakeConnection(fetch_results=[[]])
        self.assertIsNone(
            await repository(projection_connection).claim_one_for_lane(
                "projection"
            )
        )
        self.assertEqual(len(projection_connection.fetch_calls), 1)
        self.assertIn(
            "lease_projection_jobs",
            projection_connection.fetch_calls[0][0],
        )


class PilotIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_marker_is_accepted_only_inside_authorized_window(self) -> None:
        connection = FakeConnection(
            fetch_results=[[pilot_marker_row()]],
            fetchval_results=[NOW + timedelta(hours=23, minutes=59)],
        )
        self.assertTrue(await repository(connection).pilot_ever_started())
        self.assertEqual(len(connection.fetch_calls), 2)
        self.assertIn("read_pilot_marker", connection.fetch_calls[0][0])
        self.assertEqual(
            connection.fetch_calls[1][0],
            "SELECT pg_catalog.transaction_timestamp()",
        )

    async def test_stale_or_future_marker_refuses_before_claim(self) -> None:
        cases = (
            (NOW - timedelta(days=365), NOW, "stale"),
            (NOW, NOW + timedelta(hours=24), "boundary"),
            (NOW + timedelta(microseconds=1), NOW, "future"),
        )
        for started_at, transaction_time, label in cases:
            connection = FakeConnection(
                fetch_results=[[pilot_marker_row(started_at=started_at)]],
                fetchval_results=[transaction_time],
            )
            with self.subTest(label=label), self.assertRaisesRegex(
                ContractViolation,
                "pilot_marker_outside_authorized_window",
            ):
                await repository(connection).pilot_ever_started()
            self.assertEqual(len(connection.fetch_calls), 2)

    async def test_self_consistent_wrong_marker_identity_refuses_before_claim(self) -> None:
        mutations = (
            {"pilot_id": "governed_memory_pilot_stale"},
            {"pilot_contract_sha256": "c" * 64},
            {"authorization_receipt_sha256": "d" * 64},
        )
        for mutation in mutations:
            connection = FakeConnection(
                fetch_results=[[pilot_marker_row(**mutation)]],
            )
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                ContractViolation,
                "pilot_marker_identity_mismatch",
            ):
                await repository(connection).pilot_ever_started()
            self.assertEqual(len(connection.fetch_calls), 1)
            self.assertIn(
                "read_pilot_marker",
                connection.fetch_calls[0][0],
            )


class ExtractionLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_binding_becomes_typed_local_failure_not_request(self) -> None:
        connection = FakeConnection(fetch_results=[[extraction_lease_with_context()]])
        work = await repository(connection).claim_one()
        self.assertIsInstance(work, ExtractionWork)
        assert isinstance(work, ExtractionWork)
        self.assertIsNone(work.provider_request)
        self.assertEqual(
            work.local_failure_code,
            "local_serialization_failed_before_send",
        )


if __name__ == "__main__":
    unittest.main()
