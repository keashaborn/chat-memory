from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from typing import Any, AsyncIterator, Mapping
import unittest
from uuid import UUID

if os.environ.get("GM_VALIDATION_RUN") == "1":
    import asyncpg
else:  # Keep offline discovery free of the integration dependency.
    asyncpg = None  # type: ignore[assignment]

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.contracts import canonical_sha256
from rag_engine.governed_memory.conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
)
from rag_engine.governed_memory.conversation_source import (
    split_leased_chat_log_message,
)
from rag_engine.governed_memory.deletion_contracts import (
    BoundConversationDeletion,
    ConversationDeletionRequestV1,
    ConversationErasureState,
    DeletionAuthority,
    DeletionCoordinatorOutcome,
    DeletionSelectorKind,
    deletion_binding_sha256,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy
from rag_engine.governed_memory.extraction import (
    CANONICAL_PREDICATE_CATALOG_SHA256,
    build_provider_request,
    validate_provider_result,
)
from rag_engine.governed_memory.postgres_adapter import (
    extraction_lease_to_provider_inputs,
    normalize_postgres_record,
)
from rag_engine.governed_memory.projection import (
    PROJECTION_OUTBOX_FIELDS,
    build_projection_delete,
    build_projection_point,
)
from rag_engine.governed_memory.runtime.deletion_coordinator import (
    InactiveDeletionCoordinator,
)
from rag_engine.governed_memory.runtime.deletion_postgres import (
    PostgresConversationDeletionRepository,
    PostgresSuccessorDeletionRepository,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    DispatchReceipt,
    EMBEDDING_ENDPOINT_SHA256,
    embedding_request_body_sha256,
    embedding_request_sha256,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QdrantWriteOutcomeUnknown,
)
from rag_engine.governed_memory.runtime.worker_postgres import (
    WORKER_RUNTIME_CONTRACT_SHA256,
)
from rag_engine.governed_memory.worker import process_ingest_item
from tests.memory._fixtures import (
    PREDICATE_CATALOG,
    SOURCE_TEXT,
    deterministic_vector,
    make_provider_output,
)
from tests.memory_integration.test_governed_memory_http_vertical_slice import (
    ALIAS,
    AsyncQdrantRestTransport,
    ExactQdrantAdapter,
    PHYSICAL_A,
    QdrantRest,
    RUN_MARKER,
    _LoopbackTcpRelay,
    _validated_relay_target,
)


MODEL = "synthetic-extraction-model"
SCHEMA = "governed-memory-extraction"
SCHEMA_SHA256 = sha256(SCHEMA.encode("utf-8")).hexdigest()
PRIVACY_MANIFEST_SHA256 = sha256(
    b"governed-memory-phase6e-deletion-privacy"
).hexdigest()
AUTH_CONTEXT_SHA256 = "a" * 64
PHYSICAL_COLLECTION = PHYSICAL_A

OWNER_A = UUID("61000000-0000-4000-8000-000000000001")
OWNER_B = UUID("61000000-0000-4000-8000-000000000002")
CONFLICT_OWNER = UUID("61000000-0000-4000-8000-000000000003")
THREAD_A = UUID("62000000-0000-4000-8000-000000000001")
THREAD_B = UUID("62000000-0000-4000-8000-000000000002")
CONFLICT_THREAD = UUID("62000000-0000-4000-8000-000000000003")
USER_MESSAGE_A = UUID("63000000-0000-4000-8000-000000000001")
ASSISTANT_MESSAGE_A = UUID("63000000-0000-4000-8000-000000000002")
USER_MESSAGE_B = UUID("63000000-0000-4000-8000-000000000003")
ASSISTANT_MESSAGE_B = UUID("63000000-0000-4000-8000-000000000004")
ATTACHMENT_A_MESSAGE = UUID("64000000-0000-4000-8000-000000000001")
ATTACHMENT_A_THREAD = UUID("64000000-0000-4000-8000-000000000002")
ATTACHMENT_B_MESSAGE = UUID("64000000-0000-4000-8000-000000000003")
ATTACHMENT_B_THREAD = UUID("64000000-0000-4000-8000-000000000004")
RESPONSE_A = UUID("65000000-0000-4000-8000-000000000001")
RESPONSE_B = UUID("65000000-0000-4000-8000-000000000002")
DELETE_OPERATION = UUID("66000000-0000-4000-8000-000000000001")
SESSION_A = UUID("67000000-0000-4000-8000-000000000001")
WORKER_ACTOR = UUID("67000000-0000-4000-8000-000000000002")

RESILIENCE_OWNER = UUID("71000000-0000-4000-8000-000000000001")
RESILIENCE_THREAD = UUID("71000000-0000-4000-8000-000000000002")
RESILIENCE_ATTACHMENT = UUID("71000000-0000-4000-8000-000000000003")
RESILIENCE_OPERATION = UUID("71000000-0000-4000-8000-000000000004")
FUTURE_OWNER = UUID("72000000-0000-4000-8000-000000000001")
FUTURE_THREAD = UUID("72000000-0000-4000-8000-000000000002")
FUTURE_MESSAGE = UUID("72000000-0000-4000-8000-000000000003")
FUTURE_OPERATION = UUID("72000000-0000-4000-8000-000000000004")
EXHAUSTED_OWNER = UUID("73000000-0000-4000-8000-000000000001")
EXHAUSTED_THREAD = UUID("73000000-0000-4000-8000-000000000002")
EXHAUSTED_MESSAGE = UUID("73000000-0000-4000-8000-000000000003")
EXHAUSTED_OPERATION = UUID("73000000-0000-4000-8000-000000000004")
RESILIENCE_MESSAGE_IDS = tuple(
    UUID(f"74000000-0000-4000-8000-{ordinal:012d}")
    for ordinal in range(1, 502)
)

CATALOG_OPERATION_IDS = tuple(
    UUID(f"68000000-0000-4000-8000-{ordinal:012d}")
    for ordinal in range(1, 6)
)

LIFESWITCH_TABLES = (
    "accounts",
    "daily_food_logs",
    "libraries",
    "measurements",
    "weightlifting_sessions",
    "workouts",
)

SUCCESSOR_OWNER_TABLES = (
    "answer_binding",
    "audit_event",
    "claim",
    "claim_deletion_receipt",
    "claim_evidence",
    "claim_revision",
    "entity",
    "erased_chat_message_tombstone",
    "evidence",
    "extraction_job",
    "projection_outbox",
    "proposal",
    "provider_call",
    "source_erasure_claim",
    "source_erasure_operation",
    "source_erasure_receipt",
    "source_erasure_target",
)


class _SyntheticCommittedCrash(RuntimeError):
    pass


class _CrashAfterCommittedCall:
    """Raise once after a real repository call has returned successfully."""

    def __init__(self, delegate: Any, method_name: str) -> None:
        self._delegate = delegate
        self._method_name = method_name
        self.crashed = False

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._delegate, name)
        if name != self._method_name:
            return value

        async def crash_after_commit(*args: Any, **kwargs: Any) -> Any:
            result = await value(*args, **kwargs)
            if not self.crashed:
                self.crashed = True
                raise _SyntheticCommittedCrash(name)
            return result

        return crash_after_commit


class _DeleteOutcomeUnknownAfterCommitTransport:
    """Commit one real point deletion, then hide its transport response."""

    def __init__(self, delegate: AsyncQdrantRestTransport) -> None:
        self._delegate = delegate
        self.injected = False

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        result = await self._delegate.request(method, path, body)
        if (
            not self.injected
            and method == "POST"
            and path
            == f"/collections/{PHYSICAL_COLLECTION}/points/delete?wait=true"
        ):
            self.injected = True
            raise QdrantWriteOutcomeUnknown(
                "synthetic_delete_response_lost_after_commit"
            )
        return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _snapshot(value: Any) -> tuple[int, str]:
    payload = json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return len(payload), sha256(payload).hexdigest()


@unittest.skipUnless(
    os.environ.get("GM_VALIDATION_RUN") == "1",
    "requires explicitly authorized disposable successor infrastructure",
)
class ConversationDeletionDisposableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert asyncpg is not None
        host = os.environ.get("GM_VALIDATION_POSTGRES_HOST")
        port_text = os.environ.get("GM_VALIDATION_POSTGRES_PORT")
        if host != "127.0.0.1" or port_text != "55443":
            self.fail("successor PostgreSQL endpoint is not isolated")
        if os.environ.get("GM_VALIDATION_QDRANT_URL") != (
            "http://127.0.0.1:6339"
        ):
            self.fail("successor Qdrant endpoint is not isolated")
        postgres_target = _validated_relay_target(
            "GM_VALIDATION_POSTGRES_CONTAINER_IP"
        )
        qdrant_target = _validated_relay_target(
            "GM_VALIDATION_QDRANT_CONTAINER_IP"
        )
        if postgres_target == qdrant_target:
            self.fail("disposable relay targets are not distinct")
        self.relays: list[_LoopbackTcpRelay] = []
        try:
            for local_port, target_host, target_port in (
                (55443, postgres_target, 5432),
                (6339, qdrant_target, 6333),
            ):
                relay = _LoopbackTcpRelay(local_port, target_host, target_port)
                self.relays.append(relay)
                relay.start()
        except BaseException:
            self._close_relays()
            raise
        self.addAsyncCleanup(asyncio.to_thread, self._close_relays)

        self.host = host
        self.port = int(port_text)
        self.connections: list[Any] = []
        self.successor_admin = await self._connect(
            "postgres", "successor_disposable_only", "governed_memory"
        )
        self.conversation_admin = await self._connect(
            "postgres", "successor_disposable_only", "memory"
        )
        self.owner_api = await self._connect(
            "governed_memory_api",
            "successor_api_disposable_only",
            "governed_memory",
        )
        self.conversation_api = await self._connect(
            "governed_memory_api",
            "successor_api_disposable_only",
            "memory",
        )
        self.successor_worker = await self._connect(
            "governed_memory_worker",
            "successor_worker_disposable_only",
            "governed_memory",
        )
        self.conversation_worker = await self._connect(
            "governed_memory_worker",
            "successor_worker_disposable_only",
            "memory",
        )
        self.brains = await self._connect(
            "brains_app", "successor_brains_app_disposable_only", "memory"
        )
        self.qdrant = QdrantRest(os.environ.get("GM_VALIDATION_QDRANT_URL", ""))
        self.qdrant.remove_all()
        self.addCleanup(self.qdrant.remove_all)

    async def asyncTearDown(self) -> None:
        for connection in reversed(self.connections):
            await connection.close()

    def _close_relays(self) -> None:
        for relay in reversed(self.relays):
            relay.close()

    async def _connect(self, user: str, password: str, database: str) -> Any:
        assert asyncpg is not None
        connection = await asyncpg.connect(
            user=user,
            password=password,
            database=database,
            host=self.host,
            port=self.port,
            command_timeout=30,
        )
        await connection.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        self.connections.append(connection)
        return connection

    @asynccontextmanager
    async def _owner_context(
        self, connection: Any, owner_user_id: UUID
    ) -> AsyncIterator[None]:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.set_config('app.user_id',$1::text,true)",
                str(owner_user_id),
            )
            await connection.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256',$1::text,true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    @asynccontextmanager
    async def _erasure_owner_context(
        self, owner_user_id: UUID
    ) -> AsyncIterator[None]:
        async with self.conversation_api.transaction():
            await self.conversation_api.execute(
                "SET LOCAL ROLE memory_erasure_requester"
            )
            await self.conversation_api.execute(
                "SELECT pg_catalog.set_config('app.user_id',$1::text,true)",
                str(owner_user_id),
            )
            await self.conversation_api.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256',$1::text,true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    @staticmethod
    def _deletion_command(
        *, owner_user_id: UUID, operation_id: UUID, thread_id: UUID | None
    ) -> BoundConversationDeletion:
        request = ConversationDeletionRequestV1(
            operation_id=operation_id,
            selector_kind=(
                DeletionSelectorKind.ALL_CONVERSATIONS
                if thread_id is None
                else DeletionSelectorKind.THREAD
            ),
            thread_id=thread_id,
            confirmation_sha256="c" * 64,
        )
        authority = DeletionAuthority(
            owner_user_id=owner_user_id,
            actor_id=owner_user_id,
            session_id=SESSION_A,
            authentication_manifest_sha256=AUTH_CONTEXT_SHA256,
        )
        return BoundConversationDeletion(
            authority=authority,
            request=request,
            binding_sha256=deletion_binding_sha256(
                owner_user_id=owner_user_id,
                operation_id=operation_id,
                request_sha256=request.request_sha256,
            ),
        )

    async def _assert_catalog_clean(self) -> None:
        self.assertIsNone(
            await self.conversation_admin.fetchval(
                "SELECT memory_ingest_private.assert_chat_deletion_catalog()"
            )
        )

    async def _assert_request_refused_without_state(
        self,
        *,
        operation_id: UUID,
        expected_failure: DeletionRepositoryFailure,
    ) -> DeletionRepositoryError:
        command = self._deletion_command(
            owner_user_id=CONFLICT_OWNER,
            operation_id=operation_id,
            thread_id=CONFLICT_THREAD,
        )
        with self.assertRaises(DeletionRepositoryError) as raised:
            await PostgresConversationDeletionRepository(
                self.conversation_api
            ).request_erasure(command)
        self.assertEqual(raised.exception.failure, expected_failure)
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT pg_catalog.count(*) FROM "
                "memory_ingest_private.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                operation_id,
            ),
            0,
        )
        return raised.exception

    async def _prove_runtime_catalog_refusals(self) -> None:
        await self.conversation_admin.execute(
            "INSERT INTO public.threads("
            "id,owner_user_id,user_id,title,created_at,updated_at) "
            "VALUES($1::uuid,$2::uuid,$2::text,'Catalog probe',"
            "pg_catalog.clock_timestamp()-interval '1 hour',"
            "pg_catalog.clock_timestamp()-interval '1 hour')",
            CONFLICT_THREAD,
            CONFLICT_OWNER,
        )

        cases = (
            (
                "unknown_fk",
                CATALOG_OPERATION_IDS[0],
                (
                    "CREATE TABLE public.phase6e_unknown_thread_fk("
                    "row_id uuid PRIMARY KEY,owner_user_id uuid NOT NULL,"
                    "thread_id uuid NOT NULL,"
                    "CONSTRAINT phase6e_unknown_thread_fk_edge "
                    "FOREIGN KEY(owner_user_id,thread_id) "
                    "REFERENCES public.threads(owner_user_id,id) "
                    "ON DELETE RESTRICT)"
                ),
                "DROP TABLE public.phase6e_unknown_thread_fk",
            ),
            (
                "unknown_trigger",
                CATALOG_OPERATION_IDS[1],
                (
                    "CREATE FUNCTION public.phase6e_unknown_delete_trigger() "
                    "RETURNS trigger LANGUAGE plpgsql SET search_path TO "
                    "pg_catalog AS $function$ BEGIN RETURN OLD; END $function$;"
                    "CREATE TRIGGER phase6e_unknown_delete_trigger "
                    "AFTER DELETE ON public.chat_log FOR EACH ROW EXECUTE "
                    "FUNCTION public.phase6e_unknown_delete_trigger()"
                ),
                (
                    "DROP TRIGGER phase6e_unknown_delete_trigger "
                    "ON public.chat_log;"
                    "DROP FUNCTION public.phase6e_unknown_delete_trigger()"
                ),
            ),
            (
                "unknown_rule",
                CATALOG_OPERATION_IDS[2],
                (
                    "CREATE RULE phase6e_unknown_delete_rule AS ON DELETE "
                    "TO public.chat_log DO INSTEAD NOTHING"
                ),
                "DROP RULE phase6e_unknown_delete_rule ON public.chat_log",
            ),
            (
                "unknown_inheritance",
                CATALOG_OPERATION_IDS[3],
                (
                    "CREATE TABLE public.phase6e_unknown_chat_child() "
                    "INHERITS(public.chat_log)"
                ),
                "DROP TABLE public.phase6e_unknown_chat_child",
            ),
        )
        for case_name, operation_id, create_sql, drop_sql in cases:
            with self.subTest(catalog_case=case_name):
                await self.conversation_admin.execute(create_sql)
                try:
                    failure = await self._assert_request_refused_without_state(
                        operation_id=operation_id,
                        expected_failure=(
                            DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
                        ),
                    )
                    self.assertEqual(failure.status_code, 503)
                    self.assertTrue(failure.retryable)
                finally:
                    await self.conversation_admin.execute(drop_sql)
                await self._assert_catalog_clean()

        await self.conversation_admin.execute(
            "CREATE TABLE memory.project_thread_binding_event("
            "event_id uuid PRIMARY KEY,thread_id uuid NOT NULL,"
            "CONSTRAINT project_thread_binding_event_thread_fk "
            "FOREIGN KEY(thread_id) REFERENCES public.threads(id) "
            "ON DELETE RESTRICT)"
        )
        try:
            failure = await self._assert_request_refused_without_state(
                operation_id=CATALOG_OPERATION_IDS[4],
                expected_failure=(
                    DeletionRepositoryFailure.LEGACY_PROJECT_THREAD_DEPENDENCY
                ),
            )
            self.assertEqual(
                failure.code, "governed_project_thread_erasure_required"
            )
            self.assertEqual(failure.status_code, 409)
            self.assertFalse(failure.retryable)
        finally:
            await self.conversation_admin.execute(
                "DROP TABLE memory.project_thread_binding_event"
            )
        await self._assert_catalog_clean()
        await self.conversation_admin.execute(
            "DELETE FROM public.threads WHERE id=$1::uuid",
            CONFLICT_THREAD,
        )

    async def _prove_exact_auxiliary_foreign_keys(self) -> int:
        rows = await self.conversation_admin.fetch(
            "SELECT child_ns.nspname AS child_schema,child.relname AS child_table,"
            "constraint_row.conname,"
            "constraint_row.confdeltype::text AS confdeltype,"
            "pg_catalog.array_agg(child_attribute.attname "
            "ORDER BY key_row.ordinality) AS child_columns,"
            "parent_ns.nspname AS parent_schema,parent.relname AS parent_table,"
            "pg_catalog.array_agg(parent_attribute.attname "
            "ORDER BY key_row.ordinality) AS parent_columns "
            "FROM pg_catalog.pg_constraint AS constraint_row "
            "JOIN pg_catalog.pg_class AS child "
            "ON child.oid=constraint_row.conrelid "
            "JOIN pg_catalog.pg_namespace AS child_ns "
            "ON child_ns.oid=child.relnamespace "
            "JOIN pg_catalog.pg_class AS parent "
            "ON parent.oid=constraint_row.confrelid "
            "JOIN pg_catalog.pg_namespace AS parent_ns "
            "ON parent_ns.oid=parent.relnamespace "
            "JOIN LATERAL ROWS FROM ("
            "pg_catalog.unnest(constraint_row.conkey),"
            "pg_catalog.unnest(constraint_row.confkey)"
            ") WITH ORDINALITY "
            "AS key_row(child_attnum,parent_attnum,ordinality) ON true "
            "JOIN pg_catalog.pg_attribute AS child_attribute "
            "ON child_attribute.attrelid=child.oid "
            "AND child_attribute.attnum=key_row.child_attnum "
            "JOIN pg_catalog.pg_attribute AS parent_attribute "
            "ON parent_attribute.attrelid=parent.oid "
            "AND parent_attribute.attnum=key_row.parent_attnum "
            "WHERE constraint_row.contype='f' "
            "AND (child_ns.nspname,child.relname) IN "
            "(('public','chat_attachments'),"
            "('public','active_thread_selection'),"
            "('trusted_web','response_transcript_v1')) "
            "GROUP BY child_ns.nspname,child.relname,constraint_row.conname,"
            "constraint_row.confdeltype,parent_ns.nspname,parent.relname "
            "ORDER BY child_ns.nspname,child.relname,constraint_row.conname"
        )
        observed = {
            (
                row["child_schema"],
                row["child_table"],
                row["conname"],
                row["confdeltype"],
                tuple(row["child_columns"]),
                row["parent_schema"],
                row["parent_table"],
                tuple(row["parent_columns"]),
            )
            for row in rows
        }
        expected = {
            (
                "public",
                "active_thread_selection",
                "active_thread_selection_owner_thread_fk",
                "c",
                ("owner_user_id", "thread_id"),
                "public",
                "threads",
                ("owner_user_id", "id"),
            ),
            (
                "public",
                "chat_attachments",
                "chat_attachments_message_owner_thread_fk",
                "c",
                ("message_id", "owner_user_id", "thread_id"),
                "public",
                "chat_log",
                ("id", "owner_user_id", "thread_id"),
            ),
            (
                "public",
                "chat_attachments",
                "chat_attachments_thread_owner_fk",
                "c",
                ("thread_id", "owner_user_id"),
                "public",
                "threads",
                ("id", "owner_user_id"),
            ),
            (
                "trusted_web",
                "response_transcript_v1",
                "response_transcript_v1_assistant_chat_log_id_fkey",
                "c",
                ("assistant_chat_log_id", "owner_user_id", "thread_id"),
                "public",
                "chat_log",
                ("id", "owner_user_id", "thread_id"),
            ),
            (
                "trusted_web",
                "response_transcript_v1",
                "response_transcript_v1_user_chat_log_id_fkey",
                "c",
                ("user_chat_log_id", "owner_user_id", "thread_id"),
                "public",
                "chat_log",
                ("id", "owner_user_id", "thread_id"),
            ),
        }
        self.assertEqual(observed, expected)
        return len(observed)

    @staticmethod
    def _projection_outbox(row: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "owner_user_id": str(row["owner_user_id"]),
            "claim_id": str(row["claim_id"]),
            "revision_id": str(row["revision_id"]),
            "operation_id": str(row["operation_id"]),
            "operation": row["operation"],
            "sequence_number": row["sequence_number"],
            "revision_sha256": row["revision_sha256"],
            "selection_binding_sha256": row[
                "selection_binding_sha256"
            ],
            "retrieval_text_sha256": row["retrieval_text_sha256"],
            "embedding_input_sha256": row["embedding_input_sha256"],
            "projection_contract_sha256": row[
                "projection_contract_sha256"
            ],
            "projection_manifest_sha256": row[
                "projection_manifest_sha256"
            ],
            "state": "claimed",
        }
        if tuple(result) != PROJECTION_OUTBOX_FIELDS:
            raise AssertionError("projection lease is not a closed outbox")
        return result

    async def _mark_embedding_dispatch(
        self, lease: Mapping[str, Any]
    ) -> None:
        receipt = DispatchReceipt(
            operation="embeddings.create",
            model="text-embedding-3-large",
            endpoint_sha256=EMBEDDING_ENDPOINT_SHA256,
            input_sha256=str(lease["embedding_input_sha256"]),
            request_body_sha256=embedding_request_body_sha256(
                str(lease["retrieval_text"])
            ),
        )
        request_sha256 = embedding_request_sha256(receipt)
        row = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.mark_projection_embedding_dispatched("
            "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::text,$7::text,"
            "$8::text)",
            lease["outbox_id"],
            lease["lease_token"],
            receipt.operation,
            receipt.model,
            receipt.endpoint_sha256,
            receipt.input_sha256,
            receipt.request_body_sha256,
            request_sha256,
        )
        self.assertEqual(row["outcome"], "dispatched")

    async def _read_claim(
        self, owner_user_id: UUID, claim_id: UUID
    ) -> dict[str, Any]:
        async with self._owner_context(self.owner_api, owner_user_id):
            rows = await self.owner_api.fetch(
                "SELECT * FROM memory_private.read_claim_candidates("
                "$1::uuid[])",
                [claim_id],
            )
        self.assertEqual(len(rows), 1)
        return normalize_postgres_record(dict(rows[0]))

    async def _seed_owner_chain(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        message_attachment_id: UUID,
        thread_attachment_id: UUID,
        response_id: UUID,
        exact_qdrant: ExactQdrantAdapter,
    ) -> dict[str, Any]:
        async with self._owner_context(self.brains, owner_user_id):
            source_created_at = await self.brains.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            policy = EligibilityPolicy(ingest_after=source_created_at)
            await self.brains.execute(
                "INSERT INTO public.threads("
                "id,owner_user_id,user_id,title,created_at,updated_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'Phase 6E synthetic',"
                "$3::timestamptz,$3::timestamptz)",
                thread_id,
                owner_user_id,
                source_created_at,
            )
            await self.brains.execute(
                "INSERT INTO public.chat_log("
                "id,owner_user_id,user_id,source,text,thread_id,created_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'frontend/chat:user',"
                "$3::text,$4::uuid,$5::timestamptz),"
                "($6::uuid,$2::uuid,$2::text,'frontend/chat:assistant',"
                "'Synthetic acknowledgement.',$4::uuid,"
                "$5::timestamptz+interval '1 microsecond')",
                user_message_id,
                owner_user_id,
                SOURCE_TEXT,
                thread_id,
                source_created_at,
                assistant_message_id,
            )
            # Capture the eligible text row before attachments exist. The
            # bridge must refuse attachment-bearing messages at enqueue,
            # lease, and read, so attachment content cannot cross the
            # conversational Memory boundary.
            enqueued = await self.brains.fetchrow(
                "SELECT * FROM memory_ingest_private."
                "enqueue_chat_log_message($1::uuid,$2::text)",
                user_message_id,
                policy.policy_sha256,
            )
        self.assertEqual(enqueued["outcome"], "enqueued")

        # These production-shaped auxiliary fixtures intentionally have no
        # disposable application-role grants.  Seed them through the isolated
        # fixture administrator without widening the brains_app authority.
        await self.conversation_admin.execute(
            "INSERT INTO public.active_thread_selection("
            "owner_user_id,thread_id) VALUES($1::uuid,$2::uuid)",
            owner_user_id,
            thread_id,
        )
        await self.conversation_admin.execute(
            "INSERT INTO trusted_web.response_transcript_v1("
            "response_id,owner_user_id,thread_id,user_chat_log_id,"
            "assistant_chat_log_id,transcript_sha256) VALUES("
            "$1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6::text)",
            response_id,
            owner_user_id,
            thread_id,
            user_message_id,
            assistant_message_id,
            canonical_sha256(
                "governed_memory.phase6e.synthetic_transcript",
                {
                    "assistant_message_id": assistant_message_id,
                    "owner_user_id": owner_user_id,
                    "thread_id": thread_id,
                    "user_message_id": user_message_id,
                },
            ),
        )

        bridge_lease = await self.conversation_worker.fetchrow(
            "SELECT * FROM memory_ingest_private.lease_memory_ingest("
            "'phase6e_bridge_worker',1,120)"
        )
        self.assertEqual(bridge_lease["owner_user_id"], owner_user_id)
        leased = await self.conversation_worker.fetchrow(
            "SELECT * FROM memory_ingest_private.read_leased_chat_log_message("
            "$1::uuid,$2::uuid)",
            bridge_lease["outbox_id"],
            bridge_lease["lease_token"],
        )
        lease_envelope, payload = split_leased_chat_log_message(dict(leased))
        transaction_time = await self.conversation_worker.fetchval(
            "SELECT pg_catalog.clock_timestamp()"
        )
        actor = VerifiedActor(
            owner_user_id=owner_user_id,
            actor_id=WORKER_ACTOR,
            session_id=owner_user_id,
            role=ActorRole.WORKER,
            scopes=(ActorScope.PROCESS_MEMORY_INGEST,),
            authentication_manifest_sha256=AUTH_CONTEXT_SHA256,
            authenticated_at=transaction_time,
        )
        plan = process_ingest_item(
            payload,
            lease_envelope=lease_envelope,
            actor=actor,
            expected_owner_user_id=owner_user_id,
            transaction_time=transaction_time,
        )
        self.assertEqual(plan["decision"], "send_external")
        selected = dict(plan["selected_evidence"])
        evidence = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.record_selected_evidence("
            "$1::uuid,$2::uuid,$3::text,$4::uuid,$5::uuid,$6::uuid,"
            "$7::text,$8::text,$9::text,$10::integer,$11::integer,"
            "$12::uuid,$13::text,$14::timestamptz,$15::text,$16::text,"
            "$17::text)",
            owner_user_id,
            user_message_id,
            bridge_lease["source_binding_sha256"],
            UUID(str(payload["exchange_id"])),
            thread_id,
            UUID(str(payload["window_id"])),
            payload["window_sha256"],
            payload["content_sha256"],
            selected["selected_sha256"],
            selected["start_utf8"],
            selected["end_utf8"],
            None,
            None,
            source_created_at,
            "send_external",
            policy.policy_sha256,
            selected["selected_text"],
        )
        self.assertEqual(evidence["outcome"], "accepted")
        self.assertEqual(
            await self.conversation_worker.fetchval(
                "SELECT memory_ingest_private.ack_memory_ingest("
                "$1::uuid,$2::uuid,'send_external'::text,$3::uuid,$4::uuid)",
                bridge_lease["outbox_id"],
                bridge_lease["lease_token"],
                evidence["evidence_id"],
                evidence["extraction_job_id"],
            ),
            "completed",
        )

        extraction = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.lease_extraction_jobs("
            "'phase6e_extractor',1,120,$1::text,$2::text,$3::text,$4::text,"
            "'responses.create'::text,$5::text,4096,30000)",
            "synthetic",
            MODEL,
            SCHEMA_SHA256,
            CANONICAL_PREDICATE_CATALOG_SHA256,
            PRIVACY_MANIFEST_SHA256,
        )
        self.assertEqual(extraction["owner_user_id"], owner_user_id)
        provider_inputs = extraction_lease_to_provider_inputs(dict(extraction))
        provider_request = build_provider_request(
            provider_inputs["job"],
            provider_inputs["evidence"],
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
            bounded_context=None,
        )
        dispatched = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.mark_provider_call_dispatched("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)",
            extraction["job_id"],
            extraction["lease_token"],
            extraction["provider_call_id"],
            provider_request["request_sha256"],
            extraction["selected_sha256"],
            extraction["selection_binding_sha256"],
            extraction["predicate_catalog_sha256"],
        )
        self.assertEqual(dispatched["outcome"], "dispatched")
        provider_batch = validate_provider_result(
            provider_request, make_provider_output()
        )
        completed = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'completed'::text,$4::text,$5::text,"
            "128,16,NULL::text,$6::jsonb)",
            extraction["job_id"],
            extraction["lease_token"],
            extraction["provider_call_id"],
            provider_request["request_sha256"],
            provider_batch["response_sha256"],
            provider_batch["proposals"],
        )
        self.assertEqual(completed["outcome"], "completed")
        self.assertEqual(completed["proposal_count"], 1)

        async with self._owner_context(self.owner_api, owner_user_id):
            proposals = await self.owner_api.fetch(
                "SELECT * FROM memory_private.list_proposals("
                "100,NULL::timestamptz,NULL::uuid)"
            )
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            admitted = await self.owner_api.fetchrow(
                "SELECT * FROM memory_private.review_proposal("
                "$1::uuid,$2::uuid,'admitted'::text,$3::text,$4::text,"
                "$5::text,$6::text,$7::text,"
                "ARRAY['explicit_owner_review']::text[])",
                proposal["operation_id"],
                proposal["proposal_id"],
                proposal["proposal_sha256"],
                proposal["source_sha256"],
                proposal["selected_sha256"],
                proposal["selection_binding_sha256"],
                proposal["predicate_catalog_sha256"],
            )
        self.assertEqual(admitted["outcome"], "admitted")
        claim_id = admitted["claim_id"]
        claim = await self._read_claim(owner_user_id, claim_id)

        projection = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase6e_projector',1,120,$1::text)",
            WORKER_RUNTIME_CONTRACT_SHA256,
        )
        self.assertEqual(projection["owner_user_id"], owner_user_id)
        self.assertEqual(projection["claim_id"], claim_id)
        await self._mark_embedding_dispatch(projection)
        point = build_projection_point(
            claim, self._projection_outbox(projection), deterministic_vector()
        )
        upsert = await exact_qdrant.upsert_projection_point(point)
        self.assertEqual(upsert.physical_collection, PHYSICAL_COLLECTION)
        finish = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.finish_projection_job("
            "$1::uuid,$2::uuid,'applied'::text,$3::text,$4::text,"
            "NULL::text,NULL::text,NULL::text)",
            projection["outbox_id"],
            projection["lease_token"],
            PHYSICAL_COLLECTION,
            point["payload"]["vector_sha256"],
        )
        self.assertEqual(finish["outcome"], "applied")
        self.assertFalse(finish["deletion_ready"])
        self.assertEqual(len(self.qdrant.retrieve(ALIAS, claim_id)), 1)

        # Associate synthetic attachments only after the text-only Memory
        # chain is complete. They are present for chat deletion proof but
        # were absent from every capture, lease, read, and provider input.
        attachment_text = "Synthetic disposable attachment."
        attachment_hash = sha256(attachment_text.encode("utf-8")).hexdigest()
        async with self._owner_context(self.brains, owner_user_id):
            await self.brains.execute(
                "INSERT INTO public.chat_attachments("
                "id,owner_user_id,thread_id,message_id,filename,media_type,"
                "content,content_sha256,byte_size,status) VALUES("
                "$1::uuid,$2::uuid,$3::uuid,$4::uuid,'message.txt',"
                "'text/plain',$5::text,$6::text,$7::integer,'ready'),("
                "$8::uuid,$2::uuid,$3::uuid,NULL::uuid,'thread.txt',"
                "'text/plain',$5::text,$6::text,$7::integer,'ready')",
                message_attachment_id,
                owner_user_id,
                thread_id,
                user_message_id,
                attachment_text,
                attachment_hash,
                len(attachment_text.encode("utf-8")),
                thread_attachment_id,
            )
        return {
            "assistant_message_id": assistant_message_id,
            "bridge_outbox_id": bridge_lease["outbox_id"],
            "claim_id": claim_id,
            "evidence_id": evidence["evidence_id"],
            "thread_id": thread_id,
            "user_message_id": user_message_id,
        }

    async def _seed_lifeswitch_sentinels(self) -> None:
        await self.conversation_admin.executemany(
            "INSERT INTO lifeswitch_fixture.accounts("
            "owner_user_id,payload_sha256) VALUES($1::uuid,$2::text)",
            [
                (
                    owner,
                    canonical_sha256(
                        "governed_memory.phase6e.lifeswitch.account",
                        {"owner_user_id": owner},
                    ),
                )
                for owner in (OWNER_A, OWNER_B)
            ],
        )
        row_ordinal = 1
        for table_name in LIFESWITCH_TABLES:
            if table_name == "accounts":
                continue
            values = []
            for owner in (OWNER_A, OWNER_B):
                row_id = UUID(
                    f"69000000-0000-4000-8000-{row_ordinal:012d}"
                )
                values.append(
                    (
                        row_id,
                        owner,
                        canonical_sha256(
                            "governed_memory.phase6e.lifeswitch.row",
                            {
                                "owner_user_id": owner,
                                "row_id": row_id,
                                "table_name": table_name,
                            },
                        ),
                    )
                )
                row_ordinal += 1
            await self.conversation_admin.executemany(
                f"INSERT INTO lifeswitch_fixture.{table_name}("
                "row_id,owner_user_id,payload_sha256) "
                "VALUES($1::uuid,$2::uuid,$3::text)",
                values,
            )

    async def _lifeswitch_snapshot(self) -> tuple[int, str]:
        manifest: list[dict[str, str]] = []
        for table_name in LIFESWITCH_TABLES:
            rows = await self.conversation_admin.fetch(
                f"SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                f"FROM lifeswitch_fixture.{table_name} AS value "
                "ORDER BY pg_catalog.to_jsonb(value)::text"
            )
            manifest.extend(
                {
                    "row": row["row_value"],
                    "table": table_name,
                }
                for row in rows
            )
        self.assertEqual(len(manifest), 12)
        return _snapshot(manifest)

    async def _owner_b_snapshot(self) -> tuple[int, str]:
        manifest: list[dict[str, str]] = []
        conversation_queries = (
            (
                "public.threads",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM public.threads AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
            (
                "public.chat_log",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM public.chat_log AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
            (
                "public.chat_attachments",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM public.chat_attachments AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
            (
                "public.active_thread_selection",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM public.active_thread_selection AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
            (
                "trusted_web.response_transcript_v1",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM trusted_web.response_transcript_v1 AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
            (
                "memory_ingest_private.memory_ingest_outbox",
                "SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                "FROM memory_ingest_private.memory_ingest_outbox AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
            ),
        )
        for table_name, query in conversation_queries:
            rows = await self.conversation_admin.fetch(query, OWNER_B)
            manifest.extend(
                {"database": "memory", "row": row["row_value"], "table": table_name}
                for row in rows
            )
        for table_name in SUCCESSOR_OWNER_TABLES:
            rows = await self.successor_admin.fetch(
                f"SELECT pg_catalog.to_jsonb(value)::text AS row_value "
                f"FROM memory.{table_name} AS value "
                "WHERE owner_user_id=$1::uuid "
                "ORDER BY pg_catalog.to_jsonb(value)::text",
                OWNER_B,
            )
            manifest.extend(
                {
                    "database": "governed_memory",
                    "row": row["row_value"],
                    "table": f"memory.{table_name}",
                }
                for row in rows
            )
        self.assertGreaterEqual(len(manifest), 14)
        return _snapshot(manifest)

    def _owner_b_qdrant_snapshot(self, claim_id: UUID) -> tuple[int, str]:
        alias_rows = self.qdrant.retrieve(ALIAS, claim_id)
        physical_rows = self.qdrant.retrieve(PHYSICAL_COLLECTION, claim_id)
        self.assertEqual(len(alias_rows), 1)
        self.assertEqual(alias_rows, physical_rows)
        return _snapshot(
            {"alias": alias_rows, "physical": physical_rows}
        )

    async def _seed_resilience_page_fixture(self) -> None:
        async with self._owner_context(self.brains, RESILIENCE_OWNER):
            source_created_at = await self.brains.fetchval(
                "SELECT pg_catalog.transaction_timestamp()-interval '1 day'"
            )
            await self.brains.execute(
                "INSERT INTO public.threads("
                "id,owner_user_id,user_id,title,created_at,updated_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'Phase 6E resilience',"
                "$3::timestamptz,$3::timestamptz)",
                RESILIENCE_THREAD,
                RESILIENCE_OWNER,
                source_created_at,
            )
            await self.brains.executemany(
                "INSERT INTO public.chat_log("
                "id,owner_user_id,user_id,source,text,thread_id,created_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'phase6e:synthetic',"
                "'Disposable resilience fixture.',$3::uuid,"
                "$4::timestamptz)",
                [
                    (
                        message_id,
                        RESILIENCE_OWNER,
                        RESILIENCE_THREAD,
                        source_created_at.replace(
                            microsecond=(ordinal - 1) % 1_000_000
                        ),
                    )
                    for ordinal, message_id in enumerate(
                        RESILIENCE_MESSAGE_IDS, start=1
                    )
                ],
            )
            attachment_text = "Disposable attachment movement fixture."
            attachment_sha256 = sha256(
                attachment_text.encode("utf-8")
            ).hexdigest()
            await self.brains.execute(
                "INSERT INTO public.chat_attachments("
                "id,owner_user_id,thread_id,message_id,filename,media_type,"
                "content,content_sha256,byte_size,status) VALUES("
                "$1::uuid,$2::uuid,$3::uuid,$4::uuid,'resilience.txt',"
                "'text/plain',$5::text,$6::text,$7::integer,'ready')",
                RESILIENCE_ATTACHMENT,
                RESILIENCE_OWNER,
                RESILIENCE_THREAD,
                RESILIENCE_MESSAGE_IDS[0],
                attachment_text,
                attachment_sha256,
                len(attachment_text.encode("utf-8")),
            )

    async def _seed_single_chat(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        message_id: UUID,
        future_dated: bool,
    ) -> None:
        async with self._owner_context(self.brains, owner_user_id):
            transaction_time = await self.brains.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            await self.brains.execute(
                "INSERT INTO public.threads("
                "id,owner_user_id,user_id,title,created_at,updated_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'Phase 6E boundary',"
                "$3::timestamptz-interval '2 hours',"
                "$3::timestamptz-interval '2 hours')",
                thread_id,
                owner_user_id,
                transaction_time,
            )
            await self.brains.execute(
                "INSERT INTO public.chat_log("
                "id,owner_user_id,user_id,source,text,thread_id,created_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'phase6e:synthetic',"
                "'Disposable single-row fixture.',$3::uuid,"
                "$4::timestamptz + CASE WHEN $5::boolean "
                "THEN interval '1 hour' ELSE -interval '1 hour' END)",
                message_id,
                owner_user_id,
                thread_id,
                transaction_time,
                future_dated,
            )

    async def _expire_active_erasure_lease(
        self,
        operation_id: UUID,
        *,
        attempt_count: int | None = None,
    ) -> Mapping[str, Any]:
        row = await self.conversation_admin.fetchrow(
            "UPDATE memory_ingest_private.source_erasure_operation "
            "SET lease_expires_at=created_at+interval '1 microsecond',"
            "attempt_count=COALESCE($2::integer,attempt_count) "
            "WHERE operation_id=$1::uuid AND lease_token IS NOT NULL "
            "RETURNING state,attempt_count,lease_token,lease_expires_at",
            operation_id,
            attempt_count,
        )
        self.assertIsNotNone(row)
        return dict(row)

    async def _crash_after_repository_boundary(
        self,
        *,
        method_name: str,
        repository: str,
    ) -> None:
        conversation: Any = PostgresConversationDeletionRepository(
            self.conversation_worker
        )
        successor: Any = PostgresSuccessorDeletionRepository(
            self.successor_worker
        )
        if repository == "conversation":
            crashing = _CrashAfterCommittedCall(conversation, method_name)
            conversation = crashing
        elif repository == "successor":
            crashing = _CrashAfterCommittedCall(successor, method_name)
            successor = crashing
        else:
            raise AssertionError("invalid crash repository")
        coordinator = InactiveDeletionCoordinator(
            conversation=conversation,
            successor=successor,
        )
        with self.assertRaisesRegex(
            _SyntheticCommittedCrash, f"^{method_name}$"
        ):
            await coordinator.advance_one()
        self.assertTrue(crashing.crashed)

    async def _finish_source_claim_qdrant_deletion(
        self,
        *,
        expected_claim_id: UUID,
        exact_qdrant: ExactQdrantAdapter,
    ) -> tuple[str, str]:
        lease = await self.successor_worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase6e_deletion_projector',1,120,$1::text)",
            WORKER_RUNTIME_CONTRACT_SHA256,
        )
        self.assertEqual(lease["claim_id"], expected_claim_id)
        self.assertEqual(lease["operation"], "delete")
        command = build_projection_delete(
            self._projection_outbox(lease),
            collection_alias=ALIAS,
            physical_collection=PHYSICAL_COLLECTION,
        )
        self.assertEqual(
            self.qdrant.aliases().get(ALIAS), PHYSICAL_COLLECTION
        )
        qdrant_receipt = await exact_qdrant.delete_projection_point(command)
        self.assertEqual(
            qdrant_receipt.point_id, UUID(str(command["point_id"]))
        )
        self.assertEqual(
            qdrant_receipt.collection_alias, ALIAS
        )
        self.assertEqual(
            qdrant_receipt.physical_collection, PHYSICAL_COLLECTION
        )
        self.assertFalse(self.qdrant.retrieve(ALIAS, command["point_id"]))
        self.assertFalse(
            self.qdrant.retrieve(PHYSICAL_COLLECTION, command["point_id"])
        )
        deletion_state = await self.successor_admin.fetchrow(
            "SELECT current_state_sha256 FROM memory.claim "
            "WHERE owner_user_id=$1::uuid AND claim_id=$2::uuid",
            OWNER_A,
            expected_claim_id,
        )
        async with self.successor_worker.transaction():
            receipt_time = await self.successor_worker.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            completed = await self.successor_worker.fetchrow(
                "SELECT * FROM memory_private.finish_projection_job("
                "$1::uuid,$2::uuid,'applied'::text,$3::text,NULL::text,"
                "$4::text,$5::text,NULL::text)",
                lease["outbox_id"],
                lease["lease_token"],
                PHYSICAL_COLLECTION,
                qdrant_receipt.absence_verification_sha256,
                qdrant_receipt.verification_receipt_sha256,
            )
            self.assertEqual(completed["outcome"], "applied")
            self.assertTrue(completed["deletion_ready"])
            finalized = await self.successor_worker.fetchrow(
                "SELECT * FROM memory_private.finalize_claim_deletion("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::uuid,$6::uuid,"
                "$7::text,$8::integer,$9::text,$10::text,$11::timestamptz,"
                "$12::timestamptz,$13::text,$14::text)",
                lease["operation_id"],
                OWNER_A,
                lease["claim_id"],
                deletion_state["current_state_sha256"],
                lease["outbox_id"],
                lease["revision_id"],
                lease["revision_sha256"],
                lease["sequence_number"],
                lease["projection_manifest_sha256"],
                PHYSICAL_COLLECTION,
                receipt_time,
                receipt_time,
                qdrant_receipt.absence_verification_sha256,
                qdrant_receipt.verification_receipt_sha256,
            )
            self.assertEqual(finalized["outcome"], "deleted")
        retained = await self.successor_admin.fetchval(
            "SELECT receipt_sha256 FROM memory.claim_deletion_receipt "
            "WHERE owner_user_id=$1::uuid AND claim_id=$2::uuid",
            OWNER_A,
            expected_claim_id,
        )
        self.assertRegex(retained, r"^[0-9a-f]{64}$")
        return qdrant_receipt.verification_receipt_sha256, retained

    async def test_deletion_resilience_boundaries(self) -> None:
        assert asyncpg is not None

        await self._seed_single_chat(
            owner_user_id=FUTURE_OWNER,
            thread_id=FUTURE_THREAD,
            message_id=FUTURE_MESSAGE,
            future_dated=True,
        )
        with self.assertRaises(asyncpg.PostgresError) as future_failure:
            async with self._erasure_owner_context(FUTURE_OWNER):
                await self.conversation_api.fetchrow(
                    "SELECT * FROM memory_ingest_private."
                    "begin_source_erasure("
                    "$1::uuid,'recent'::text,NULL::uuid,NULL::uuid,"
                    "3600::integer,$2::text)",
                    FUTURE_OPERATION,
                    "c" * 64,
                )
        self.assertEqual(future_failure.exception.sqlstate, "23514")
        self.assertEqual(
            future_failure.exception.message,
            "source erasure selector has future-dated chat rows",
        )
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT pg_catalog.count(*) FROM "
                "memory_ingest_private.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                FUTURE_OPERATION,
            ),
            0,
        )

        await self._seed_resilience_page_fixture()
        command = self._deletion_command(
            owner_user_id=RESILIENCE_OWNER,
            operation_id=RESILIENCE_OPERATION,
            thread_id=RESILIENCE_THREAD,
        )
        owner_repository = PostgresConversationDeletionRepository(
            self.conversation_api
        )
        initial = await owner_repository.request_erasure(command)
        self.assertEqual(initial.state, ConversationErasureState.FENCED)
        self.assertEqual(initial.target_count, 501)

        with self.assertRaises(asyncpg.PostgresError) as movement_failure:
            async with self._owner_context(self.brains, RESILIENCE_OWNER):
                await self.brains.execute(
                    "UPDATE public.chat_attachments SET message_id=$2::uuid "
                    "WHERE id=$1::uuid",
                    RESILIENCE_ATTACHMENT,
                    RESILIENCE_MESSAGE_IDS[1],
                )
        self.assertEqual(movement_failure.exception.sqlstate, "55000")
        self.assertEqual(
            movement_failure.exception.message,
            "attachment source erasure fence is active",
        )
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT message_id FROM public.chat_attachments "
                "WHERE id=$1::uuid",
                RESILIENCE_ATTACHMENT,
            ),
            RESILIENCE_MESSAGE_IDS[0],
        )

        conversation_repository = PostgresConversationDeletionRepository(
            self.conversation_worker
        )
        stale_lease = await conversation_repository.lease_erasure(
            worker_id="governed-memory-deletion-coordinator-1",
            lease_seconds=120,
        )
        self.assertIsNotNone(stale_lease)
        assert stale_lease is not None
        self.assertEqual(stale_lease.operation_id, RESILIENCE_OPERATION)
        await self._expire_active_erasure_lease(RESILIENCE_OPERATION)
        with self.assertRaises(asyncpg.PostgresError) as stale_read_failure:
            await self.conversation_worker.fetch(
                "SELECT * FROM memory_ingest_private."
                "read_source_erasure_targets("
                "$1::uuid,$2::uuid,NULL::timestamptz,NULL::uuid,500)",
                RESILIENCE_OPERATION,
                stale_lease.lease_token,
            )
        self.assertEqual(stale_read_failure.exception.sqlstate, "55000")
        self.assertEqual(
            stale_read_failure.exception.message,
            "source erasure lease is stale",
        )

        lease = await conversation_repository.lease_erasure(
            worker_id="governed-memory-deletion-coordinator-1",
            lease_seconds=120,
        )
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.operation_id, RESILIENCE_OPERATION)
        self.assertNotEqual(lease.lease_token, stale_lease.lease_token)
        with self.assertRaises(asyncpg.PostgresError) as stale_release_failure:
            await self.conversation_worker.fetchval(
                "SELECT memory_ingest_private.release_source_erasure_lease("
                "$1::uuid,$2::uuid)",
                RESILIENCE_OPERATION,
                stale_lease.lease_token,
            )
        self.assertEqual(stale_release_failure.exception.sqlstate, "55000")
        self.assertEqual(
            stale_release_failure.exception.message,
            "source erasure lease is stale",
        )
        lease_inventory = await self.conversation_admin.fetchrow(
            "SELECT attempt_count,lease_token,state FROM "
            "memory_ingest_private.source_erasure_operation "
            "WHERE operation_id=$1::uuid",
            RESILIENCE_OPERATION,
        )
        self.assertEqual(lease_inventory["attempt_count"], 2)
        self.assertEqual(lease_inventory["lease_token"], lease.lease_token)
        self.assertEqual(lease_inventory["state"], "governed_deletion_pending")

        first_source_page = await self.conversation_worker.fetch(
            "SELECT * FROM memory_ingest_private."
            "read_source_erasure_targets("
            "$1::uuid,$2::uuid,NULL::timestamptz,NULL::uuid,500)",
            RESILIENCE_OPERATION,
            lease.lease_token,
        )
        second_source_page = await self.conversation_worker.fetch(
            "SELECT * FROM memory_ingest_private."
            "read_source_erasure_targets("
            "$1::uuid,$2::uuid,$3::timestamptz,$4::uuid,500)",
            RESILIENCE_OPERATION,
            lease.lease_token,
            first_source_page[-1]["source_created_at"],
            first_source_page[-1]["message_id"],
        )
        source_page_sizes = (
            len(first_source_page),
            len(second_source_page),
        )
        self.assertEqual(source_page_sizes, (500, 1))
        targets = await conversation_repository.read_erasure_targets(lease)
        self.assertEqual(len(targets), 501)
        self.assertEqual(
            tuple(target.message_id for target in targets),
            RESILIENCE_MESSAGE_IDS,
        )
        await conversation_repository.release_erasure_lease(lease)

        await self._crash_after_repository_boundary(
            method_name="stage_source_erasure",
            repository="successor",
        )
        self.assertEqual(
            await self.successor_admin.fetchval(
                "SELECT state FROM memory.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                RESILIENCE_OPERATION,
            ),
            "receiving",
        )
        await self._expire_active_erasure_lease(RESILIENCE_OPERATION)

        first_append_page = targets[:500]
        second_append_page = targets[500:]

        async def append_page(page: tuple[Any, ...]) -> Mapping[str, Any]:
            row = await self.successor_worker.fetchrow(
                "SELECT * FROM memory_private.append_source_erasure_targets("
                "$1::uuid,$2::uuid[],$3::uuid[],$4::timestamptz[],"
                "$5::text[])",
                RESILIENCE_OPERATION,
                [target.message_id for target in page],
                [target.thread_id for target in page],
                [target.source_created_at for target in page],
                [target.target_sha256 for target in page],
            )
            return dict(row)

        first_append = await append_page(first_append_page)
        self.assertEqual(
            first_append,
            {
                "outcome": "appended",
                "inserted_count": 500,
                "received_target_count": 500,
            },
        )
        first_append_replay = await append_page(first_append_page)
        self.assertEqual(
            first_append_replay,
            {
                "outcome": "replayed",
                "inserted_count": 0,
                "received_target_count": 500,
            },
        )
        conflict_created_at = await self.conversation_admin.fetchval(
            "SELECT $1::timestamptz+interval '2 days'",
            targets[0].source_created_at,
        )
        conflict_target_sha256 = await self.conversation_admin.fetchval(
            "SELECT memory_ingest_private.source_erasure_target_sha256("
            "$1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::timestamptz)",
            RESILIENCE_OWNER,
            RESILIENCE_OPERATION,
            targets[0].message_id,
            targets[0].thread_id,
            conflict_created_at,
        )
        with self.assertRaises(asyncpg.PostgresError) as replay_conflict:
            await self.successor_worker.fetchrow(
                "SELECT * FROM memory_private.append_source_erasure_targets("
                "$1::uuid,$2::uuid[],$3::uuid[],$4::timestamptz[],"
                "$5::text[])",
                RESILIENCE_OPERATION,
                [targets[0].message_id],
                [targets[0].thread_id],
                [conflict_created_at],
                [conflict_target_sha256],
            )
        self.assertEqual(replay_conflict.exception.sqlstate, "23514")
        self.assertEqual(
            replay_conflict.exception.message,
            "source erasure target replay drifted",
        )
        second_append = await append_page(second_append_page)
        self.assertEqual(
            second_append,
            {
                "outcome": "appended",
                "inserted_count": 1,
                "received_target_count": 501,
            },
        )
        successor_page_sizes = (
            len(first_append_page),
            len(second_append_page),
        )
        self.assertEqual(successor_page_sizes, (500, 1))

        durable_boundaries = (
            ("stage_source_erasure_targets", "successor", True),
            ("seal_source_erasure_targets", "successor", True),
            (
                "request_next_source_erasure_claim_deletion",
                "successor",
                True,
            ),
            ("finalize_source_erasure_memory", "successor", True),
            ("mark_successor_memory_deleted", "conversation", False),
            ("finalize_conversation_erasure", "conversation", False),
        )
        for method_name, repository, expire_after in durable_boundaries:
            with self.subTest(crash_boundary=method_name):
                await self._crash_after_repository_boundary(
                    method_name=method_name,
                    repository=repository,
                )
                if method_name == "stage_source_erasure_targets":
                    self.assertEqual(
                        await self.successor_admin.fetchval(
                            "SELECT received_target_count FROM "
                            "memory.source_erasure_operation "
                            "WHERE operation_id=$1::uuid",
                            RESILIENCE_OPERATION,
                        ),
                        501,
                    )
                elif method_name == "seal_source_erasure_targets":
                    self.assertEqual(
                        await self.successor_admin.fetchval(
                            "SELECT state FROM memory.source_erasure_operation "
                            "WHERE operation_id=$1::uuid",
                            RESILIENCE_OPERATION,
                        ),
                        "fenced",
                    )
                elif method_name == "finalize_source_erasure_memory":
                    self.assertEqual(
                        await self.successor_admin.fetchval(
                            "SELECT state FROM memory.source_erasure_operation "
                            "WHERE operation_id=$1::uuid",
                            RESILIENCE_OPERATION,
                        ),
                        "memory_deleted",
                    )
                elif method_name == "mark_successor_memory_deleted":
                    state_and_lease = await self.conversation_admin.fetchrow(
                        "SELECT state,lease_token FROM "
                        "memory_ingest_private.source_erasure_operation "
                        "WHERE operation_id=$1::uuid",
                        RESILIENCE_OPERATION,
                    )
                    self.assertEqual(state_and_lease["state"], "governed_deleted")
                    self.assertIsNone(state_and_lease["lease_token"])
                elif method_name == "finalize_conversation_erasure":
                    self.assertEqual(
                        await self.conversation_admin.fetchval(
                            "SELECT state FROM "
                            "memory_ingest_private.source_erasure_operation "
                            "WHERE operation_id=$1::uuid",
                            RESILIENCE_OPERATION,
                        ),
                        "conversation_deleted_pending_ack",
                    )
                if expire_after:
                    await self._expire_active_erasure_lease(
                        RESILIENCE_OPERATION
                    )

        pending_ack_expired = await self._expire_active_erasure_lease(
            RESILIENCE_OPERATION,
            attempt_count=1000,
        )
        self.assertEqual(
            pending_ack_expired["state"],
            "conversation_deleted_pending_ack",
        )
        self.assertEqual(pending_ack_expired["attempt_count"], 1000)
        await self._crash_after_repository_boundary(
            method_name="acknowledge_conversation_deleted",
            repository="successor",
        )
        self.assertEqual(
            await self.successor_admin.fetchval(
                "SELECT state FROM memory.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                RESILIENCE_OPERATION,
            ),
            "completed",
        )
        await self._expire_active_erasure_lease(
            RESILIENCE_OPERATION,
            attempt_count=1000,
        )
        await self._crash_after_repository_boundary(
            method_name="acknowledge_erasure_completion",
            repository="conversation",
        )
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT state FROM "
                "memory_ingest_private.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                RESILIENCE_OPERATION,
            ),
            "completed",
        )
        self.assertIsNone(
            await InactiveDeletionCoordinator(
                conversation=PostgresConversationDeletionRepository(
                    self.conversation_worker
                ),
                successor=PostgresSuccessorDeletionRepository(
                    self.successor_worker
                ),
            ).advance_one()
        )

        with self.assertRaises(asyncpg.PostgresError) as reuse_failure:
            async with self._owner_context(self.brains, RESILIENCE_OWNER):
                await self.brains.execute(
                    "INSERT INTO public.chat_attachments("
                    "id,owner_user_id,thread_id,message_id,filename,"
                    "media_type,content,content_sha256,byte_size,status) "
                    "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,"
                    "'reuse.txt','text/plain','x',$5::text,1,'ready')",
                    RESILIENCE_ATTACHMENT,
                    RESILIENCE_OWNER,
                    RESILIENCE_THREAD,
                    RESILIENCE_MESSAGE_IDS[0],
                    sha256(b"x").hexdigest(),
                )
        self.assertEqual(reuse_failure.exception.sqlstate, "55000")
        self.assertEqual(
            reuse_failure.exception.message,
            "erased chat attachment identity may not be reused",
        )

        await self._seed_single_chat(
            owner_user_id=EXHAUSTED_OWNER,
            thread_id=EXHAUSTED_THREAD,
            message_id=EXHAUSTED_MESSAGE,
            future_dated=False,
        )
        exhausted_initial = await owner_repository.request_erasure(
            self._deletion_command(
                owner_user_id=EXHAUSTED_OWNER,
                operation_id=EXHAUSTED_OPERATION,
                thread_id=EXHAUSTED_THREAD,
            )
        )
        self.assertEqual(exhausted_initial.target_count, 1)
        await self.conversation_admin.execute(
            "UPDATE memory_ingest_private.source_erasure_operation "
            "SET attempt_count=1000 WHERE operation_id=$1::uuid "
            "AND lease_token IS NULL AND state='fenced'",
            EXHAUSTED_OPERATION,
        )
        self.assertIsNone(
            await conversation_repository.lease_erasure(
                worker_id="governed-memory-deletion-coordinator-1",
                lease_seconds=120,
            )
        )
        exhausted_inventory = await self.conversation_admin.fetchrow(
            "SELECT state,last_error_code,target_count FROM "
            "memory_ingest_private.source_erasure_operation "
            "WHERE operation_id=$1::uuid",
            EXHAUSTED_OPERATION,
        )
        self.assertEqual(
            dict(exhausted_inventory),
            {
                "state": "manual_review",
                "last_error_code": "coordinator_attempts_exhausted",
                "target_count": 1,
            },
        )
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT pg_catalog.count(*) FROM public.chat_log "
                "WHERE owner_user_id=$1::uuid",
                EXHAUSTED_OWNER,
            ),
            1,
        )

        conversation_receipt = await self.conversation_admin.fetchrow(
            "SELECT * FROM memory_ingest_private.source_erasure_receipt "
            "WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid",
            RESILIENCE_OWNER,
            RESILIENCE_OPERATION,
        )
        successor_receipt = await self.successor_admin.fetchrow(
            "SELECT * FROM memory.source_erasure_receipt "
            "WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid",
            RESILIENCE_OWNER,
            RESILIENCE_OPERATION,
        )
        self.assertEqual(conversation_receipt["target_count"], 501)
        self.assertEqual(conversation_receipt["deleted_message_count"], 501)
        self.assertEqual(conversation_receipt["deleted_thread_count"], 1)
        self.assertEqual(conversation_receipt["deleted_attachment_count"], 1)
        self.assertEqual(successor_receipt["target_count"], 501)
        self.assertEqual(successor_receipt["touched_claim_count"], 0)
        self.assertEqual(
            successor_receipt["conversation_receipt_sha256"],
            conversation_receipt["receipt_sha256"],
        )
        self.assertEqual(
            await self.conversation_admin.fetchval(
                "SELECT attempt_count FROM "
                "memory_ingest_private.source_erasure_operation "
                "WHERE operation_id=$1::uuid",
                RESILIENCE_OPERATION,
            ),
            1000,
        )
        final_conversation_absence = await self.conversation_admin.fetchrow(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM public.chat_log "
            " WHERE owner_user_id=$1::uuid) AS chat_rows,"
            "(SELECT pg_catalog.count(*) FROM public.chat_attachments "
            " WHERE owner_user_id=$1::uuid) AS attachment_rows,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_target "
            " WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid) "
            " AS source_target_rows,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_thread_target "
            " WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid) "
            " AS source_thread_target_rows,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_message_tombstone "
            " WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid) "
            " AS message_tombstone_rows,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_thread_tombstone "
            " WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid) "
            " AS thread_tombstone_rows",
            RESILIENCE_OWNER,
            RESILIENCE_OPERATION,
        )
        successor_target_rows = await self.successor_admin.fetchval(
            "SELECT pg_catalog.count(*) FROM memory.source_erasure_target "
            "WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid",
            RESILIENCE_OWNER,
            RESILIENCE_OPERATION,
        )
        final_absence_manifest = {
            **dict(final_conversation_absence),
            "successor_target_rows": successor_target_rows,
        }
        self.assertEqual(
            final_absence_manifest,
            {
                "attachment_rows": 0,
                "chat_rows": 0,
                "message_tombstone_rows": 501,
                "source_target_rows": 0,
                "source_thread_target_rows": 0,
                "successor_target_rows": 0,
                "thread_tombstone_rows": 1,
            },
        )

        crash_boundary_names = (
            "stage_source_erasure",
            "stage_source_erasure_targets",
            "seal_source_erasure_targets",
            "request_next_source_erasure_claim_deletion",
            "finalize_source_erasure_memory",
            "mark_successor_memory_deleted",
            "finalize_conversation_erasure",
            "acknowledge_conversation_deleted",
            "acknowledge_erasure_completion",
        )
        receipt = {
            "attachment_identity_reuse_refused": True,
            "attachment_movement_refused": True,
            "conversation_final_receipt_sha256": conversation_receipt[
                "receipt_sha256"
            ],
            "crash_after_conversation_completion_ack": True,
            "crash_after_conversation_finalize": True,
            "crash_after_governed_receipt_handoff": True,
            "crash_after_source_claim_step": True,
            "crash_after_source_memory_finalize": True,
            "crash_after_source_register": True,
            "crash_after_successor_completion_ack": True,
            "crash_after_target_append": True,
            "crash_after_target_seal": True,
            "crash_boundary_count": len(crash_boundary_names),
            "crash_boundary_manifest_sha256": canonical_sha256(
                "governed_memory.phase6e.crash_boundary_manifest.v1",
                crash_boundary_names,
            ),
            "deleted_attachment_count": conversation_receipt[
                "deleted_attachment_count"
            ],
            "deleted_message_count": conversation_receipt[
                "deleted_message_count"
            ],
            "deleted_thread_count": conversation_receipt[
                "deleted_thread_count"
            ],
            "exhausted_attempts_manual_review": True,
            "final_absence_manifest_sha256": canonical_sha256(
                "governed_memory.phase6e.final_absence_manifest.v1",
                final_absence_manifest,
            ),
            "final_absence_verified": True,
            "future_dated_chat_refused": True,
            "pending_ack_attempt_cap_recovered": True,
            "post_completion_no_work": True,
            "production_data_read": False,
            "production_endpoint_calls": 0,
            "provider_external_calls": 0,
            "schema": (
                "governed-memory-successor-deletion-resilience-"
                "receipt-v1"
            ),
            "source_page_count": len(source_page_sizes),
            "source_page_sizes_sha256": canonical_sha256(
                "governed_memory.phase6e.source_page_sizes.v1",
                source_page_sizes,
            ),
            "source_target_manifest_sha256": initial.target_manifest_sha256,
            "stale_lease_read_refused": True,
            "stale_lease_release_refused": True,
            "stale_lease_replaced": True,
            "successor_final_receipt_sha256": successor_receipt[
                "receipt_sha256"
            ],
            "successor_page_count": len(successor_page_sizes),
            "successor_page_replay_exact": True,
            "successor_page_replay_conflict_refused": True,
            "successor_page_sizes_sha256": canonical_sha256(
                "governed_memory.phase6e.successor_page_sizes.v1",
                successor_page_sizes,
            ),
            "target_count": initial.target_count,
        }
        print(
            "SUCCESSOR_PHASE6E_DELETION_RESILIENCE_RECEIPT="
            + json.dumps(receipt, sort_keys=True)
        )

    async def test_exact_chat_only_deletion_and_protected_store_retention(
        self,
    ) -> None:
        successor_marker = await self.successor_admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid,'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname=current_database()"
        )
        conversation_marker = await self.conversation_admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid,'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname=current_database()"
        )
        self.assertEqual(successor_marker, RUN_MARKER)
        self.assertEqual(conversation_marker, RUN_MARKER)

        auxiliary_fk_count = await self._prove_exact_auxiliary_foreign_keys()
        await self._prove_runtime_catalog_refusals()

        self.qdrant.create_collection(PHYSICAL_COLLECTION)
        self.qdrant.create_alias(ALIAS, PHYSICAL_COLLECTION)
        qdrant_transport = _DeleteOutcomeUnknownAfterCommitTransport(
            AsyncQdrantRestTransport(self.qdrant)
        )
        exact_qdrant = ExactQdrantAdapter(
            qdrant_transport
        )
        preflight = await exact_qdrant.preflight()
        self.assertEqual(preflight.physical_collection, PHYSICAL_COLLECTION)

        await self._seed_lifeswitch_sentinels()
        owner_a = await self._seed_owner_chain(
            owner_user_id=OWNER_A,
            thread_id=THREAD_A,
            user_message_id=USER_MESSAGE_A,
            assistant_message_id=ASSISTANT_MESSAGE_A,
            message_attachment_id=ATTACHMENT_A_MESSAGE,
            thread_attachment_id=ATTACHMENT_A_THREAD,
            response_id=RESPONSE_A,
            exact_qdrant=exact_qdrant,
        )
        owner_b = await self._seed_owner_chain(
            owner_user_id=OWNER_B,
            thread_id=THREAD_B,
            user_message_id=USER_MESSAGE_B,
            assistant_message_id=ASSISTANT_MESSAGE_B,
            message_attachment_id=ATTACHMENT_B_MESSAGE,
            thread_attachment_id=ATTACHMENT_B_THREAD,
            response_id=RESPONSE_B,
            exact_qdrant=exact_qdrant,
        )
        lifeswitch_before = await self._lifeswitch_snapshot()
        owner_b_before = await self._owner_b_snapshot()
        owner_b_qdrant_before = self._owner_b_qdrant_snapshot(
            owner_b["claim_id"]
        )

        command = self._deletion_command(
            owner_user_id=OWNER_A,
            operation_id=DELETE_OPERATION,
            thread_id=None,
        )
        owner_repository = PostgresConversationDeletionRepository(
            self.conversation_api
        )
        initial = await owner_repository.request_erasure(command)
        self.assertEqual(initial.state, ConversationErasureState.FENCED)
        self.assertEqual(initial.target_count, 2)

        coordinator = InactiveDeletionCoordinator(
            conversation=PostgresConversationDeletionRepository(
                self.conversation_worker
            ),
            successor=PostgresSuccessorDeletionRepository(
                self.successor_worker
            ),
        )
        prepared = await coordinator.advance_one()
        self.assertIsNotNone(prepared)
        self.assertEqual(
            prepared.outcome, DeletionCoordinatorOutcome.PROCESSING
        )
        self.assertEqual(prepared.target_count, 2)
        self.assertEqual(prepared.affected_claim_count, 1)
        self.assertEqual(prepared.pending_claim_deletions, 1)

        qdrant_deletion_receipt_sha256, claim_deletion_receipt_sha256 = (
            await self._finish_source_claim_qdrant_deletion(
                expected_claim_id=owner_a["claim_id"],
                exact_qdrant=exact_qdrant,
            )
        )
        self.assertTrue(qdrant_transport.injected)
        governed_deleted = await coordinator.advance_one()
        self.assertIsNotNone(governed_deleted)
        self.assertEqual(
            governed_deleted.outcome, DeletionCoordinatorOutcome.PROCESSING
        )
        self.assertEqual(
            governed_deleted.conversation_state,
            ConversationErasureState.GOVERNED_DELETED,
        )
        completed = await coordinator.advance_one()
        self.assertIsNotNone(completed)
        self.assertEqual(
            completed.outcome, DeletionCoordinatorOutcome.COMPLETED
        )
        self.assertEqual(
            completed.conversation_state, ConversationErasureState.COMPLETED
        )
        self.assertIsNone(await coordinator.advance_one())

        replay_one = await owner_repository.request_erasure(command)
        replay_two = await owner_repository.request_erasure(command)
        self.assertEqual(replay_one, replay_two)
        self.assertEqual(replay_one.state, ConversationErasureState.COMPLETED)
        self.assertEqual(replay_one.selector_sha256, initial.selector_sha256)
        self.assertEqual(
            replay_one.target_manifest_sha256,
            initial.target_manifest_sha256,
        )
        self.assertEqual(replay_one.target_count, 2)
        self.assertIsNone(await coordinator.advance_one())

        conversation_receipt = await self.conversation_admin.fetchrow(
            "SELECT * FROM memory_ingest_private.source_erasure_receipt "
            "WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid",
            OWNER_A,
            DELETE_OPERATION,
        )
        self.assertEqual(conversation_receipt["target_count"], 2)
        self.assertEqual(conversation_receipt["thread_target_count"], 1)
        self.assertEqual(conversation_receipt["deleted_message_count"], 2)
        self.assertEqual(conversation_receipt["deleted_thread_count"], 1)
        self.assertEqual(conversation_receipt["deleted_attachment_count"], 2)
        self.assertEqual(conversation_receipt["deleted_bridge_row_count"], 1)
        self.assertEqual(conversation_receipt["message_tombstone_count"], 2)
        self.assertEqual(conversation_receipt["thread_tombstone_count"], 1)

        successor_receipt = await self.successor_admin.fetchrow(
            "SELECT * FROM memory.source_erasure_receipt "
            "WHERE owner_user_id=$1::uuid AND operation_id=$2::uuid",
            OWNER_A,
            DELETE_OPERATION,
        )
        self.assertEqual(successor_receipt["target_count"], 2)
        self.assertEqual(successor_receipt["touched_claim_count"], 1)
        self.assertEqual(
            successor_receipt["claim_deletion_receipt_count"], 1
        )

        conversation_absence = await self.conversation_admin.fetchrow(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM public.threads "
            " WHERE owner_user_id=$1::uuid) AS threads,"
            "(SELECT pg_catalog.count(*) FROM public.chat_log "
            " WHERE owner_user_id=$1::uuid) AS messages,"
            "(SELECT pg_catalog.count(*) FROM public.chat_attachments "
            " WHERE owner_user_id=$1::uuid) AS attachments,"
            "(SELECT pg_catalog.count(*) FROM public.active_thread_selection "
            " WHERE owner_user_id=$1::uuid) AS active_selection,"
            "(SELECT pg_catalog.count(*) FROM "
            " trusted_web.response_transcript_v1 "
            " WHERE owner_user_id=$1::uuid) AS transcripts,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.memory_ingest_outbox "
            " WHERE owner_user_id=$1::uuid) AS bridge_rows,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_target "
            " WHERE owner_user_id=$1::uuid) AS transient_targets,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_thread_target "
            " WHERE owner_user_id=$1::uuid) AS transient_thread_targets,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_message_tombstone "
            " WHERE owner_user_id=$1::uuid) AS message_tombstones,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory_ingest_private.source_erasure_thread_tombstone "
            " WHERE owner_user_id=$1::uuid) AS thread_tombstones",
            OWNER_A,
        )
        self.assertEqual(
            dict(conversation_absence),
            {
                "threads": 0,
                "messages": 0,
                "attachments": 0,
                "active_selection": 0,
                "transcripts": 0,
                "bridge_rows": 0,
                "transient_targets": 0,
                "transient_thread_targets": 0,
                "message_tombstones": 2,
                "thread_tombstones": 1,
            },
        )
        successor_absence = await self.successor_admin.fetchrow(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM memory.claim "
            " WHERE owner_user_id=$1::uuid) AS claims,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_revision "
            " WHERE owner_user_id=$1::uuid) AS revisions,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_links,"
            "(SELECT pg_catalog.count(*) FROM memory.evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.projection_outbox "
            " WHERE owner_user_id=$1::uuid) AS projection_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.proposal "
            " WHERE owner_user_id=$1::uuid) AS proposals,"
            "(SELECT pg_catalog.count(*) FROM memory.provider_call "
            " WHERE owner_user_id=$1::uuid) AS provider_calls,"
            "(SELECT pg_catalog.count(*) FROM memory.extraction_job "
            " WHERE owner_user_id=$1::uuid) AS extraction_jobs,"
            "(SELECT pg_catalog.count(*) FROM memory.source_erasure_target "
            " WHERE owner_user_id=$1::uuid) AS transient_targets,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory.erased_chat_message_tombstone "
            " WHERE owner_user_id=$1::uuid) AS message_tombstones,"
            "(SELECT pg_catalog.count(*) FROM "
            " memory.claim_deletion_receipt "
            " WHERE owner_user_id=$1::uuid) AS claim_deletion_receipts",
            OWNER_A,
        )
        self.assertEqual(
            dict(successor_absence),
            {
                "claims": 0,
                "revisions": 0,
                "evidence_links": 0,
                "evidence_rows": 0,
                "projection_rows": 0,
                "proposals": 0,
                "provider_calls": 0,
                "extraction_jobs": 0,
                "transient_targets": 0,
                "message_tombstones": 2,
                "claim_deletion_receipts": 1,
            },
        )
        self.assertFalse(
            self.qdrant.retrieve(ALIAS, owner_a["claim_id"])
        )
        self.assertFalse(
            self.qdrant.retrieve(
                PHYSICAL_COLLECTION, owner_a["claim_id"]
            )
        )

        lifeswitch_after = await self._lifeswitch_snapshot()
        owner_b_after = await self._owner_b_snapshot()
        owner_b_qdrant_after = self._owner_b_qdrant_snapshot(
            owner_b["claim_id"]
        )
        self.assertEqual(lifeswitch_after, lifeswitch_before)
        self.assertEqual(owner_b_after, owner_b_before)
        self.assertEqual(owner_b_qdrant_after, owner_b_qdrant_before)

        receipt = {
            "attachment_thread_bridge_absence": True,
            "clean_composite_auxiliary_fk_count": auxiliary_fk_count,
            "claim_deletion_receipt_sha256": (
                claim_deletion_receipt_sha256
            ),
            "coordinator_final_receipt_sha256": completed.receipt_sha256,
            "coordinator_no_work_after_completion": True,
            "crash_injection_tested": False,
            "deleted_attachment_count": 2,
            "deleted_bridge_row_count": 1,
            "deleted_claim_count": 1,
            "deleted_message_count": 2,
            "deleted_thread_count": 1,
            "exact_completed_request_replay": True,
            "governed_final_receipt_sha256": successor_receipt[
                "receipt_sha256"
            ],
            "governed_message_tombstone_count": 2,
            "lifeswitch_snapshot_bytes": lifeswitch_before[0],
            "lifeswitch_snapshot_sha256": lifeswitch_before[1],
            "live_runtime_catalog_refusal_count": 4,
            "other_owner_qdrant_snapshot_bytes": owner_b_qdrant_before[0],
            "other_owner_qdrant_snapshot_sha256": owner_b_qdrant_before[1],
            "other_owner_snapshot_bytes": owner_b_before[0],
            "other_owner_snapshot_sha256": owner_b_before[1],
            "page_boundary_tested": False,
            "production_data_read": False,
            "production_endpoint_calls": 0,
            "production_service_invoked": False,
            "provider_external_calls": 0,
            "qdrant_deletion_receipt_sha256": (
                qdrant_deletion_receipt_sha256
            ),
            "qdrant_delete_outcome_unknown_resolved_by_readback": True,
            "qdrant_target_absent_alias_and_physical": True,
            "schema": (
                "governed-memory-successor-conversation-deletion-"
                "disposable-receipt-v1"
            ),
            "source_conversation_final_receipt_sha256": (
                conversation_receipt["receipt_sha256"]
            ),
            "source_message_tombstone_count": 2,
            "source_thread_tombstone_count": 1,
            "stale_lease_injection_tested": False,
            "synthetic_provider_only": True,
            "target_message_count": 2,
            "target_thread_count": 1,
            "transient_targets_purged": True,
            "typed_project_conflict_code": (
                "governed_project_thread_erasure_required"
            ),
            "typed_project_conflict_retryable": False,
            "typed_project_conflict_status": 409,
        }
        print(
            "SUCCESSOR_PHASE6E_DELETION_RECEIPT="
            + json.dumps(receipt, sort_keys=True)
        )
