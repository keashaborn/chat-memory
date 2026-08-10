from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from typing import Any, AsyncIterator, Mapping
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

if os.environ.get("GM_PHASE2_RUN") == "1":
    import asyncpg
else:  # Keep offline unit discovery free of the integration dependency.
    asyncpg = None  # type: ignore[assignment]

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
    canonical_sha256,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy
from rag_engine.governed_memory.extraction import (
    CANONICAL_PREDICATE_CATALOG_SHA256,
    build_provider_request,
    validate_provider_result,
)
from rag_engine.governed_memory.projection import (
    PROJECTION_OUTBOX_FIELDS,
    QDRANT_PAYLOAD_FIELDS,
    build_projection_delete,
    build_projection_delete_receipt,
    build_projection_point,
    build_rebuild_projection_point,
    qdrant_absence_verification_sha256,
    vector_sha256,
)
from rag_engine.governed_memory.postgres_adapter import (
    extraction_lease_to_provider_inputs,
    normalize_postgres_record,
    projection_rebuild_row_to_inputs,
)
from rag_engine.governed_memory.retrieval import (
    ANSWER_RENDERER_SHA256,
    CANDIDATE_FIELDS,
    RetrievalPolicy,
    build_answer_binding,
    mark_answer_binding_dispatched,
    render_memory_context,
    revalidate_candidates,
    validate_vector_candidates,
)
from rag_engine.governed_memory.worker import process_ingest_item
from tests.memory._fixtures import (
    EXCHANGE_A,
    MESSAGE_A,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    SOURCE_TEXT,
    THREAD_A,
    WINDOW_A,
    deterministic_vector,
    make_claim_row,
    make_ingest_payload,
    make_projection_outbox,
    make_provider_output,
)


RUN_MARKER = "governed-memory-phase2-disposable:019fe927"
AUTH_CONTEXT_SHA256 = "a" * 64
MODEL = "synthetic-extraction-model"
SCHEMA = "governed-memory-extraction"
SCHEMA_SHA256 = sha256(SCHEMA.encode("utf-8")).hexdigest()
PRIVACY_MANIFEST_SHA256 = sha256(
    b"governed-memory-phase2-disposable-privacy"
).hexdigest()
PROMPT_SHA256 = "d" * 64
QUERY_SHA256 = "e" * 64
RESPONSE_ID = UUID("99999999-9999-4999-8999-999999999991")
ANSWER_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999992")
CORRECTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999993")
RETRACTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999994")
DELETION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999995")
THREAD_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
OWNER_B_CLAIM_ID = UUID("22222222-2222-4222-8222-222222222226")
OWNER_B_REVISION_ID = UUID("22222222-2222-4222-8222-222222222227")
ALIAS = "governed_memory_active"
PHYSICAL_A = "governed_memory_phase2_a_019fe927"
PHYSICAL_B = "governed_memory_phase2_b_019fe927"
INDEX_FIELDS = {
    "owner_user_id": "keyword",
    "lifecycle_state": "keyword",
    "is_current": "bool",
    "projectable": "bool",
    "predicate": "keyword",
    "requires_explicit": "bool",
    "sensitivity": "keyword",
    "domains": "keyword",
    "intents": "keyword",
}
OWNER_TABLES = (
    "answer_binding",
    "audit_event",
    "claim",
    "claim_deletion_receipt",
    "claim_evidence",
    "claim_revision",
    "entity",
    "evidence",
    "extraction_job",
    "projection_outbox",
    "proposal",
    "provider_call",
)


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


class QdrantRest:
    def __init__(self, base_url: str) -> None:
        if base_url != "http://127.0.0.1:6338":
            raise RuntimeError("Phase 2 Qdrant URL is not the isolated endpoint")
        self.base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        encoded = None if body is None else json.dumps(_jsonable(body)).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except HTTPError as exc:
            payload = exc.read()
            if allow_not_found and exc.code == 404:
                return exc.code, json.loads(payload) if payload else {}
            raise AssertionError(
                f"Qdrant {method} {path} failed with {exc.code}: "
                f"{payload.decode('utf-8', errors='replace')[:500]}"
            ) from exc

    def collection_exists(self, name: str) -> bool:
        status, _ = self.request(
            "GET", f"/collections/{name}", allow_not_found=True
        )
        return status == 200

    def create_collection(self, name: str) -> None:
        status, _ = self.request(
            "PUT",
            f"/collections/{name}",
            {
                "vectors": {"size": 3072, "distance": "Dot"},
                "shard_number": 1,
                "replication_factor": 1,
                "write_consistency_factor": 1,
                "on_disk_payload": False,
            },
        )
        if status != 200:
            raise AssertionError("Qdrant collection creation did not complete")
        for field_name, field_schema in INDEX_FIELDS.items():
            self.request(
                "PUT",
                f"/collections/{name}/index?wait=true",
                {"field_name": field_name, "field_schema": field_schema},
            )

    def aliases(self) -> dict[str, str]:
        _, payload = self.request("GET", "/aliases")
        return {
            item["alias_name"]: item["collection_name"]
            for item in payload["result"]["aliases"]
        }

    def create_alias(self, alias: str, physical: str) -> None:
        self.request(
            "POST",
            "/collections/aliases?timeout=10",
            {
                "actions": [
                    {
                        "create_alias": {
                            "alias_name": alias,
                            "collection_name": physical,
                        }
                    }
                ]
            },
        )

    def swap_alias(self, alias: str, old: str, new: str) -> None:
        if self.aliases().get(alias) != old:
            raise AssertionError("Qdrant alias did not point to the expected old build")
        self.request(
            "POST",
            "/collections/aliases?timeout=10",
            {
                "actions": [
                    {"delete_alias": {"alias_name": alias}},
                    {
                        "create_alias": {
                            "alias_name": alias,
                            "collection_name": new,
                        }
                    },
                ]
            },
        )
        if self.aliases().get(alias) != new:
            raise AssertionError("Qdrant alias swap did not complete atomically")

    def upsert(self, collection: str, point: Mapping[str, Any]) -> None:
        self.request(
            "PUT",
            f"/collections/{collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": point["point_id"],
                        "vector": point["vector"],
                        "payload": point["payload"],
                    }
                ]
            },
        )

    def delete_point(self, collection: str, point_id: UUID | str) -> None:
        self.request(
            "POST",
            f"/collections/{collection}/points/delete?wait=true",
            {"points": [str(point_id)]},
        )

    def retrieve(self, collection: str, point_id: UUID | str) -> list[dict[str, Any]]:
        _, payload = self.request(
            "POST",
            f"/collections/{collection}/points",
            {
                "ids": [str(point_id)],
                "with_payload": True,
                "with_vector": True,
            },
        )
        return payload["result"]

    def search(
        self,
        collection: str,
        owner_user_id: UUID,
        vector: list[float] | tuple[float, ...],
    ) -> list[dict[str, Any]]:
        payload_fields = [field for field in CANDIDATE_FIELDS if field != "score"]
        _, payload = self.request(
            "POST",
            f"/collections/{collection}/points/search",
            {
                "vector": vector,
                "limit": 8,
                "params": {"exact": True},
                "with_vector": False,
                "with_payload": {"include": payload_fields},
                "filter": {
                    "must": [
                        {
                            "key": "owner_user_id",
                            "match": {"value": str(owner_user_id)},
                        },
                        {
                            "key": "lifecycle_state",
                            "match": {"value": "active"},
                        },
                        {"key": "is_current", "match": {"value": True}},
                        {"key": "projectable", "match": {"value": True}},
                        {
                            "key": "predicate",
                            "match": {"any": ["preference.personal"]},
                        },
                        {
                            "key": "requires_explicit",
                            "match": {"value": False},
                        },
                    ]
                },
            },
        )
        return payload["result"]

    def remove_all(self) -> None:
        aliases = self.aliases()
        if aliases.get(ALIAS) in {PHYSICAL_A, PHYSICAL_B}:
            self.request(
                "POST",
                "/collections/aliases?timeout=10",
                {"actions": [{"delete_alias": {"alias_name": ALIAS}}]},
            )
        for name in (PHYSICAL_A, PHYSICAL_B):
            if self.collection_exists(name):
                self.request("DELETE", f"/collections/{name}?timeout=10")


@unittest.skipUnless(
    os.environ.get("GM_PHASE2_RUN") == "1",
    "requires explicitly authorized disposable Phase 2 infrastructure",
)
class Phase2VerticalSliceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert asyncpg is not None
        host = os.environ.get("GM_PHASE2_POSTGRES_HOST")
        port_text = os.environ.get("GM_PHASE2_POSTGRES_PORT")
        if host != "127.0.0.1" or port_text != "55442":
            self.fail("Phase 2 PostgreSQL endpoint is not the isolated endpoint")
        self.host = host
        self.port = int(port_text)
        self.qdrant = QdrantRest(os.environ.get("GM_PHASE2_QDRANT_URL", ""))
        self.connections: list[Any] = []
        self.admin = await self._connect("postgres", "phase2_disposable_only", "governed_memory")
        self.bridge_admin = await self._connect(
            "postgres", "phase2_disposable_only", "phase2_conversation"
        )
        self.api = await self._connect(
            "governed_memory_api", "phase2_api_disposable_only", "governed_memory"
        )
        self.worker = await self._connect(
            "governed_memory_worker",
            "phase2_worker_disposable_only",
            "governed_memory",
        )
        self.worker_two = await self._connect(
            "governed_memory_worker",
            "phase2_worker_disposable_only",
            "governed_memory",
        )
        self.bridge_worker = await self._connect(
            "governed_memory_worker",
            "phase2_worker_disposable_only",
            "phase2_conversation",
        )
        self.ingest = await self._connect(
            "phase2_ingest_login",
            "phase2_ingest_disposable_only",
            "phase2_conversation",
        )
        self.qdrant.remove_all()
        self.addCleanup(self.qdrant.remove_all)

    async def asyncTearDown(self) -> None:
        for connection in reversed(self.connections):
            await connection.close()

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
    async def owner_context(
        self, connection: Any, owner_user_id: UUID
    ) -> AsyncIterator[None]:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.set_config('app.user_id', $1::text, true)",
                str(owner_user_id),
            )
            await connection.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256', $1::text, true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    @asynccontextmanager
    async def ingest_context(self, owner_user_id: UUID) -> AsyncIterator[None]:
        async with self.ingest.transaction():
            await self.ingest.execute("SET LOCAL ROLE memory_ingest_writer")
            await self.ingest.execute(
                "SELECT pg_catalog.set_config('app.user_id', $1::text, true)",
                str(owner_user_id),
            )
            await self.ingest.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256', $1::text, true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    async def read_claims(self, owner: UUID, claim_ids: list[UUID]) -> list[dict[str, Any]]:
        async with self.owner_context(self.api, owner):
            rows = await self.api.fetch(
                "SELECT * FROM memory_private.read_claim_candidates($1::uuid[])",
                claim_ids,
            )
        return [normalize_postgres_record(dict(row)) for row in rows]

    async def list_claims(self, owner: UUID) -> list[dict[str, Any]]:
        async with self.owner_context(self.api, owner):
            rows = await self.api.fetch(
                "SELECT * FROM memory_private.list_claims("
                "NULL::uuid, 100, NULL::timestamptz, NULL::uuid)"
            )
        return [normalize_postgres_record(dict(row)) for row in rows]

    @staticmethod
    def projection_outbox(row: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "owner_user_id": str(row["owner_user_id"]),
            "claim_id": str(row["claim_id"]),
            "revision_id": str(row["revision_id"]),
            "operation_id": str(row["operation_id"]),
            "operation": row["operation"],
            "sequence_number": row["sequence_number"],
            "revision_sha256": row["revision_sha256"],
            "selection_binding_sha256": row["selection_binding_sha256"],
            "retrieval_text_sha256": row["retrieval_text_sha256"],
            "embedding_input_sha256": row["embedding_input_sha256"],
            "projection_contract_sha256": row["projection_contract_sha256"],
            "projection_manifest_sha256": row["projection_manifest_sha256"],
            "state": "claimed",
        }
        if tuple(result) != PROJECTION_OUTBOX_FIELDS:
            raise AssertionError("projection lease does not map to the closed outbox")
        return result

    @staticmethod
    def qdrant_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(item["payload"])
        candidate["score"] = item["score"]
        if tuple(sorted(candidate)) != CANDIDATE_FIELDS:
            raise AssertionError("Qdrant returned a non-candidate payload field")
        return candidate

    async def finish_upsert(
        self, lease: Mapping[str, Any], point: Mapping[str, Any], physical: str
    ) -> None:
        result = await self.worker.fetchrow(
            "SELECT * FROM memory_private.finish_projection_job("
            "$1::uuid,$2::uuid,'applied'::text,$3::text,$4::text,"
            "NULL::text,NULL::text,NULL::text)",
            lease["outbox_id"],
            lease["lease_token"],
            physical,
            point["payload"]["vector_sha256"],
        )
        self.assertEqual(dict(result), {"outcome": "applied", "deletion_ready": False})
        replay = await self.worker.fetchrow(
            "SELECT * FROM memory_private.finish_projection_job("
            "$1::uuid,$2::uuid,'applied'::text,$3::text,$4::text,"
            "NULL::text,NULL::text,NULL::text)",
            lease["outbox_id"],
            lease["lease_token"],
            physical,
            point["payload"]["vector_sha256"],
        )
        self.assertEqual(replay["outcome"], "replayed")

    async def finish_delete(
        self,
        lease: Mapping[str, Any],
        *,
        physical: str,
        finalize: bool,
        deletion_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = build_projection_delete(
            self.projection_outbox(lease),
            collection_alias=ALIAS,
            physical_collection=physical,
        )
        self.assertEqual(self.qdrant.aliases().get(ALIAS), physical)
        self.qdrant.delete_point(physical, command["point_id"])
        alias_absent = not self.qdrant.retrieve(ALIAS, command["point_id"])
        physical_absent = not self.qdrant.retrieve(physical, command["point_id"])
        absence_hash = qdrant_absence_verification_sha256(
            collection_alias=ALIAS,
            physical_collection=physical,
            point_id=UUID(str(command["point_id"])),
            projection_sequence=int(command["projection_sequence"]),
            alias_target_verified=True,
            alias_absent=alias_absent,
            physical_absent=physical_absent,
        )
        async with self.worker.transaction():
            receipt_time = await self.worker.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            receipt = build_projection_delete_receipt(
                command,
                applied_at=receipt_time,
                absence_verified_at=receipt_time,
                absence_verification_sha256=absence_hash,
            )
            result = await self.worker.fetchrow(
                "SELECT * FROM memory_private.finish_projection_job("
                "$1::uuid,$2::uuid,'applied'::text,$3::text,NULL::text,"
                "$4::text,$5::text,NULL::text)",
                lease["outbox_id"],
                lease["lease_token"],
                physical,
                receipt["absence_verification_sha256"],
                receipt["receipt_sha256"],
            )
            self.assertTrue(result["deletion_ready"])
            if finalize:
                if deletion_state is None:
                    raise AssertionError("deletion finalization state is required")
                finalized = await self.worker.fetchrow(
                    "SELECT * FROM memory_private.finalize_claim_deletion("
                    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::uuid,$6::uuid,"
                    "$7::text,$8::integer,$9::text,$10::text,$11::timestamptz,"
                    "$12::timestamptz,$13::text,$14::text)",
                    DELETION_OPERATION_ID,
                    OWNER_A,
                    lease["claim_id"],
                    deletion_state["current_state_sha256"],
                    lease["outbox_id"],
                    lease["revision_id"],
                    lease["revision_sha256"],
                    lease["sequence_number"],
                    lease["projection_manifest_sha256"],
                    physical,
                    receipt_time,
                    receipt_time,
                    receipt["absence_verification_sha256"],
                    receipt["receipt_sha256"],
                )
                self.assertEqual(finalized["outcome"], "deleted")
        return receipt

    async def test_real_chat_a_to_chat_b_rebuild_and_deletion(self) -> None:
        marker = await self.admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname = current_database()"
        )
        bridge_marker = await self.bridge_admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname = current_database()"
        )
        self.assertEqual(marker, RUN_MARKER)
        self.assertEqual(bridge_marker, RUN_MARKER)
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname='memory' AND c.relkind='r'"
            ),
            13,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname='memory' AND c.relkind='r' "
                "AND c.relrowsecurity AND c.relforcerowsecurity"
            ),
            12,
        )
        lease_result = await self.admin.fetchval(
            "SELECT pg_catalog.pg_get_function_result(p.oid) "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n "
            "ON n.oid=p.pronamespace WHERE n.nspname='memory_private' "
            "AND p.proname='lease_extraction_jobs'"
        )
        self.assertIn("provider_operation_id uuid", lease_result)
        read_result = await self.admin.fetchval(
            "SELECT pg_catalog.pg_get_function_result(p.oid) "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n "
            "ON n.oid=p.pronamespace WHERE n.nspname='memory_private' "
            "AND p.proname='read_claim_candidates'"
        )
        self.assertIn("projection_sequence integer", read_result)
        self.assertIn("state_sha256 text", read_result)
        self.assertNotIn("revision_fact_policy_sha256", read_result)
        policies = await self.admin.fetch(
            "SELECT tablename,policyname,roles,qual,with_check "
            "FROM pg_catalog.pg_policies WHERE schemaname='memory' "
            "ORDER BY tablename,policyname"
        )
        self.assertEqual(
            {(row["tablename"], row["policyname"]) for row in policies},
            {
                (table, policy)
                for table in OWNER_TABLES
                for policy in ("owner_internal", "owner_isolation")
            },
        )
        for row in policies:
            if row["policyname"] == "owner_isolation":
                self.assertEqual(row["roles"], ["governed_memory_api"])
                self.assertIn("current_owner_id", row["qual"])
                self.assertEqual(row["qual"], row["with_check"])
            else:
                self.assertEqual(row["roles"], ["governed_memory_owner"])
                self.assertEqual(row["qual"], "true")
                self.assertEqual(row["with_check"], "true")
        forbidden_dml = (
            "SELECT pg_catalog.count(*) FROM memory.claim",
            "INSERT INTO memory.claim(claim_id) "
            "VALUES ('12345678-1234-4234-8234-123456789012'::uuid)",
            "UPDATE memory.claim SET updated_at=pg_catalog.clock_timestamp()",
            "DELETE FROM memory.claim",
        )
        for connection in (self.api, self.worker):
            for statement in forbidden_dml:
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(statement)

        async with self.ingest_context(OWNER_A):
            transaction_time = await self.ingest.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            source_created_at = await self.ingest.fetchval(
                "SELECT pg_catalog.clock_timestamp()"
            )
            policy = EligibilityPolicy(ingest_after=transaction_time)
            payload = make_ingest_payload(created_at=source_created_at)
            await self.bridge_admin.execute(
                "INSERT INTO public.phase2_conversation_message_fixture("
                "owner_user_id,message_id,thread_id,role,content,"
                "content_sha256,created_at) VALUES("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,"
                "$7::timestamptz)",
                OWNER_A,
                MESSAGE_A,
                THREAD_A,
                payload["role"],
                payload["content"],
                payload["content_sha256"],
                source_created_at,
            )
            enqueued = await self.ingest.fetchrow(
                "SELECT * FROM memory_ingest_private.enqueue_memory_ingest("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::timestamptz,"
                "$6::uuid,$7::uuid,$8::integer,$9::text,$10::text)",
                OWNER_A,
                MESSAGE_A,
                THREAD_A,
                payload["content_sha256"],
                source_created_at,
                EXCHANGE_A,
                WINDOW_A,
                payload["window_ordinal"],
                payload["window_sha256"],
                policy.policy_sha256,
            )
        self.assertEqual(enqueued["outcome"], "enqueued")

        bridge_lease = await self.bridge_worker.fetchrow(
            "SELECT * FROM memory_ingest_private.lease_memory_ingest("
            "'phase2_bridge_worker',1,120)"
        )
        transaction_time = await self.bridge_worker.fetchval(
            "SELECT pg_catalog.clock_timestamp()"
        )
        actor = VerifiedActor(
            owner_user_id=OWNER_A,
            actor_id=UUID("33333333-3333-4333-8333-333333333333"),
            role=ActorRole.WORKER,
            scopes=(ActorScope.PROCESS_MEMORY_INGEST,),
            authentication_manifest_sha256=AUTH_CONTEXT_SHA256,
            authenticated_at=transaction_time,
        )
        plan = process_ingest_item(
            payload,
            lease_envelope=dict(bridge_lease),
            actor=actor,
            expected_owner_user_id=OWNER_A,
            transaction_time=transaction_time,
        )
        self.assertEqual(plan["decision"], "send_external")
        selected = dict(plan["selected_evidence"])

        evidence_sql = (
            "SELECT * FROM memory_private.record_selected_evidence("
            "$1::uuid,$2::uuid,$3::text,$4::uuid,$5::uuid,$6::uuid,"
            "$7::text,$8::text,$9::text,$10::integer,$11::integer,"
            "$12::uuid,$13::text,$14::timestamptz,$15::text,$16::text,"
            "$17::text)"
        )
        evidence_args = (
            OWNER_A,
            MESSAGE_A,
            bridge_lease["source_binding_sha256"],
            MESSAGE_A,
            THREAD_A,
            WINDOW_A,
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
        concurrent = await asyncio.gather(
            self.worker.fetchrow(evidence_sql, *evidence_args),
            self.worker_two.fetchrow(evidence_sql, *evidence_args),
        )
        self.assertEqual(
            {row["outcome"] for row in concurrent}, {"accepted", "replayed"}
        )
        evidence_result = next(row for row in concurrent if row["outcome"] == "accepted")
        self.assertEqual(concurrent[0]["evidence_id"], concurrent[1]["evidence_id"])
        acknowledged = await self.bridge_worker.fetchval(
            "SELECT memory_ingest_private.ack_memory_ingest("
            "$1::uuid,$2::uuid,'send_external'::text,$3::uuid,$4::uuid)",
            bridge_lease["outbox_id"],
            bridge_lease["lease_token"],
            evidence_result["evidence_id"],
            evidence_result["extraction_job_id"],
        )
        self.assertEqual(acknowledged, "completed")
        async with self.ingest_context(OWNER_A):
            terminal_replay = await self.ingest.fetchrow(
                "SELECT * FROM memory_ingest_private.enqueue_memory_ingest("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::timestamptz,"
                "$6::uuid,$7::uuid,$8::integer,$9::text,$10::text)",
                OWNER_A,
                MESSAGE_A,
                THREAD_A,
                payload["content_sha256"],
                source_created_at,
                EXCHANGE_A,
                WINDOW_A,
                payload["window_ordinal"],
                payload["window_sha256"],
                policy.policy_sha256,
            )
        self.assertEqual(terminal_replay["outcome"], "terminal_replayed")
        await self.bridge_admin.execute(
            "UPDATE public.memory_ingest_outbox "
            "SET created_at=pg_catalog.transaction_timestamp()-interval '31 days', "
            "content_hash_expires_at="
            "pg_catalog.transaction_timestamp()-interval '30 days 1 hour', "
            "purge_after=pg_catalog.transaction_timestamp()-interval '1 day' "
            "WHERE outbox_id=$1::uuid",
            bridge_lease["outbox_id"],
        )
        self.assertEqual(
            await self.bridge_worker.fetchval(
                "SELECT memory_ingest_private.purge_terminal_memory_ingest(10)"
            ),
            1,
        )
        self.assertEqual(
            await self.bridge_admin.fetchval(
                "SELECT pg_catalog.count(*) FROM public.memory_ingest_outbox"
            ),
            0,
        )
        del plan
        del selected

        catalog_sha256 = CANONICAL_PREDICATE_CATALOG_SHA256
        first_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_extraction_jobs("
            "'phase2_extractor',1,120,$1::text,$2::text,$3::text,$4::text,"
            "'responses.create'::text,$5::text,4096,30000)",
            "synthetic",
            MODEL,
            SCHEMA_SHA256,
            catalog_sha256,
            PRIVACY_MANIFEST_SHA256,
        )
        failed_before_send = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'retryable_failure'::text,"
            "NULL::text,NULL::text,NULL::integer,NULL::integer,"
            "'connection_failed_before_send'::text,'[]'::jsonb)",
            first_lease["job_id"],
            first_lease["lease_token"],
            first_lease["provider_call_id"],
        )
        self.assertEqual(failed_before_send["outcome"], "retryable_failure")
        replay_before_send = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'retryable_failure'::text,"
            "NULL::text,NULL::text,NULL::integer,NULL::integer,"
            "'connection_failed_before_send'::text,'[]'::jsonb)",
            first_lease["job_id"],
            first_lease["lease_token"],
            first_lease["provider_call_id"],
        )
        self.assertEqual(replay_before_send["outcome"], "replayed")
        await self.admin.execute(
            "UPDATE memory.extraction_job "
            "SET available_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=$1::uuid",
            first_lease["job_id"],
        )
        extraction_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_extraction_jobs("
            "'phase2_extractor',1,120,$1::text,$2::text,$3::text,$4::text,"
            "'responses.create'::text,$5::text,4096,30000)",
            "synthetic",
            MODEL,
            SCHEMA_SHA256,
            catalog_sha256,
            PRIVACY_MANIFEST_SHA256,
        )
        self.assertEqual(extraction_lease["attempt_number"], 2)
        lease_inputs = extraction_lease_to_provider_inputs(dict(extraction_lease))
        source_lookup = lease_inputs["source_lookup"]
        source_record = await self.bridge_worker.fetchrow(
            "SELECT role,content,content_sha256 FROM "
            "public.phase2_conversation_message_fixture "
            "WHERE owner_user_id=$1::uuid AND message_id=$2::uuid "
            "AND thread_id=$3::uuid AND content_sha256=$4::text",
            UUID(source_lookup["owner_user_id"]),
            UUID(source_lookup["message_id"]),
            UUID(source_lookup["thread_id"]),
            source_lookup["content_sha256"],
        )
        self.assertIsNotNone(source_record)
        self.assertEqual(
            sha256(source_record["content"].encode("utf-8")).hexdigest(),
            source_record["content_sha256"],
        )
        bounded_context = None
        context_lookup = lease_inputs["context_lookup"]
        if context_lookup is not None:
            context_record = await self.bridge_worker.fetchrow(
                "SELECT role,content,content_sha256 FROM "
                "public.phase2_conversation_message_fixture "
                "WHERE owner_user_id=$1::uuid AND message_id=$2::uuid "
                "AND role='assistant' AND content_sha256=$3::text",
                UUID(context_lookup["owner_user_id"]),
                UUID(context_lookup["message_id"]),
                context_lookup["content_sha256"],
            )
            self.assertIsNotNone(context_record)
            bounded_context = dict(context_record)
        cold_evidence = lease_inputs["evidence"]
        request = build_provider_request(
            lease_inputs["job"],
            cold_evidence,
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
            bounded_context=bounded_context,
        )
        dispatched = await self.worker.fetchrow(
            "SELECT * FROM memory_private.mark_provider_call_dispatched("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            extraction_lease["selected_sha256"],
            extraction_lease["selection_binding_sha256"],
            extraction_lease["predicate_catalog_sha256"],
        )
        self.assertEqual(dispatched["outcome"], "dispatched")
        with self.assertRaises(asyncpg.PostgresError) as duplicate_dispatch:
            await self.worker.fetchrow(
                "SELECT * FROM memory_private.mark_provider_call_dispatched("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)",
                extraction_lease["job_id"],
                extraction_lease["lease_token"],
                extraction_lease["provider_call_id"],
                request["request_sha256"],
                extraction_lease["selected_sha256"],
                extraction_lease["selection_binding_sha256"],
                extraction_lease["predicate_catalog_sha256"],
            )
        self.assertEqual(duplicate_dispatch.exception.sqlstate, "55000")
        batch = validate_provider_result(request, make_provider_output())
        completed = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'completed'::text,$4::text,$5::text,"
            "128,16,NULL::text,$6::jsonb)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            batch["response_sha256"],
            batch["proposals"],
        )
        self.assertEqual(dict(completed), {"outcome": "completed", "proposal_count": 1})
        completed_replay = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'completed'::text,$4::text,$5::text,"
            "128,16,NULL::text,$6::jsonb)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            batch["response_sha256"],
            batch["proposals"],
        )
        self.assertEqual(completed_replay["outcome"], "replayed")
        del batch
        async with self.owner_context(self.api, OWNER_B):
            self.assertEqual(
                await self.api.fetch(
                    "SELECT * FROM memory_private.list_proposals("
                    "100,NULL::timestamptz,NULL::uuid)"
                ),
                [],
            )
        async with self.owner_context(self.api, OWNER_A):
            review_rows = await self.api.fetch(
                "SELECT * FROM memory_private.list_proposals("
                "100,NULL::timestamptz,NULL::uuid)"
            )
        self.assertEqual(len(review_rows), 1)
        review_surface = dict(review_rows[0])
        self.assertEqual(review_surface["source_excerpt"], SOURCE_TEXT)
        self.assertEqual(review_surface["subject_entity_type"], "self")
        self.assertEqual(review_surface["predicate"], "preference.personal")
        self.assertEqual(review_surface["object_kind"], "literal")
        self.assertEqual(review_surface["object_literal"], "cobalt")

        async with self.owner_context(self.api, OWNER_B):
            with self.assertRaises(asyncpg.PostgresError):
                await self.api.fetchrow(
                    "SELECT * FROM memory_private.review_proposal("
                    "$1::uuid,$2::uuid,'admitted'::text,$3::text,$4::text,"
                    "$5::text,$6::text,$7::text,$8::text[])",
                    review_surface["operation_id"],
                    review_surface["proposal_id"],
                    review_surface["proposal_sha256"],
                    review_surface["source_sha256"],
                    review_surface["selected_sha256"],
                    review_surface["selection_binding_sha256"],
                    review_surface["predicate_catalog_sha256"],
                    ["explicit_owner_review"],
                )
        async with self.owner_context(self.api, OWNER_A):
            review = await self.api.fetchrow(
                "SELECT * FROM memory_private.review_proposal("
                "$1::uuid,$2::uuid,'admitted'::text,$3::text,$4::text,"
                "$5::text,$6::text,$7::text,$8::text[])",
                review_surface["operation_id"],
                review_surface["proposal_id"],
                review_surface["proposal_sha256"],
                review_surface["source_sha256"],
                review_surface["selected_sha256"],
                review_surface["selection_binding_sha256"],
                review_surface["predicate_catalog_sha256"],
                ["explicit_owner_review"],
            )
        self.assertEqual(review["outcome"], "admitted")
        claim_id = review["claim_id"]
        claims = await self.read_claims(OWNER_A, [claim_id])
        self.assertEqual(len(claims), 1)
        self.assertEqual(await self.read_claims(OWNER_B, [claim_id]), [])

        await self.admin.execute("GRANT SELECT ON memory.claim TO governed_memory_api")
        try:
            async with self.owner_context(self.api, OWNER_A):
                self.assertEqual(
                    await self.api.fetchval("SELECT count(*) FROM memory.claim"), 1
                )
            async with self.owner_context(self.api, OWNER_B):
                self.assertEqual(
                    await self.api.fetchval("SELECT count(*) FROM memory.claim"), 0
                )
        finally:
            await self.admin.execute("REVOKE SELECT ON memory.claim FROM governed_memory_api")

        self.qdrant.create_collection(PHYSICAL_A)
        self.qdrant.create_alias(ALIAS, PHYSICAL_A)
        projection_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase2_projector',1,120)"
        )
        point = build_projection_point(
            claims[0], self.projection_outbox(projection_lease), deterministic_vector()
        )
        self.qdrant.upsert(PHYSICAL_A, point)
        stored = self.qdrant.retrieve(PHYSICAL_A, claim_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(tuple(sorted(stored[0]["payload"])), QDRANT_PAYLOAD_FIELDS)
        self.assertEqual(len(stored[0]["vector"]), 3072)
        self.assertEqual(
            vector_sha256(stored[0]["vector"]), point["payload"]["vector_sha256"]
        )
        await self.finish_upsert(projection_lease, point, PHYSICAL_A)

        owner_b_claim = make_claim_row(
            owner_user_id=OWNER_B,
            claim_id=OWNER_B_CLAIM_ID,
            revision_id=OWNER_B_REVISION_ID,
        )
        owner_b_point = build_projection_point(
            owner_b_claim,
            make_projection_outbox(owner_b_claim, state="claimed"),
            deterministic_vector(),
        )
        self.qdrant.upsert(PHYSICAL_A, owner_b_point)
        owner_a_results = self.qdrant.search(ALIAS, OWNER_A, list(point["vector"]))
        self.assertEqual([item["id"] for item in owner_a_results], [str(claim_id)])
        owner_b_result = self.qdrant.search(ALIAS, OWNER_B, list(point["vector"]))[0]
        with self.assertRaisesRegex(ContractViolation, "cross_owner_vector_candidate"):
            validate_vector_candidates(OWNER_A, [self.qdrant_candidate(owner_b_result)])
        self.qdrant.delete_point(PHYSICAL_A, OWNER_B_CLAIM_ID)

        candidates = [self.qdrant_candidate(item) for item in owner_a_results]
        validated = validate_vector_candidates(OWNER_A, candidates)
        authoritative = await self.read_claims(
            OWNER_A, [UUID(item["claim_id"]) for item in validated]
        )
        answer_policy = RetrievalPolicy(
            explicit_recall=False,
            allowed_predicates=("preference.personal",),
            max_records=8,
        )
        authorization_at = await self.admin.fetchval(
            "SELECT pg_catalog.clock_timestamp()"
        )
        selected_claims = revalidate_candidates(
            OWNER_A,
            validated,
            authoritative,
            policy=answer_policy,
            predicate_catalog=PREDICATE_CATALOG,
            authorization_at=authorization_at,
        )
        self.assertEqual(len(selected_claims), 1)
        rendered = render_memory_context(
            OWNER_A, selected_claims, max_records=8, max_bytes=32768
        )
        prepared = build_answer_binding(
            owner_user_id=OWNER_A,
            response_id=RESPONSE_ID,
            rendered_context=rendered,
            selected_claims=selected_claims,
            query_sha256=QUERY_SHA256,
            policy=answer_policy,
            renderer_sha256=ANSWER_RENDERER_SHA256,
            prompt_sha256=PROMPT_SHA256,
        )
        escaped = canonical_json_bytes(rendered["content"])
        prefix = b'{"input":"synthetic question","memory":'
        outbound = prefix + escaped + b"}"
        binding = mark_answer_binding_dispatched(
            prepared,
            rendered,
            outbound_request_bytes=outbound,
            escaped_segment_start_utf8=len(prefix),
            escaped_segment_end_utf8=len(prefix) + len(escaped),
        )
        self.assertNotEqual(THREAD_A, THREAD_B)
        async with self.owner_context(self.api, OWNER_A):
            stored_binding = await self.api.fetchrow(
                "SELECT * FROM memory_private.record_answer_binding("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text[],"
                "$7::text[],$8::text[],$9::integer,$10::integer,$11::text,"
                "$12::text,$13::boolean,$14::uuid[],$15::uuid[],$16::text,"
                "$17::text,$18::text,$19::text,$20::text)",
                ANSWER_OPERATION_ID,
                RESPONSE_ID,
                THREAD_B,
                QUERY_SHA256,
                answer_policy.policy_sha256,
                list(answer_policy.allowed_predicates),
                list(answer_policy.domains),
                list(answer_policy.intents),
                answer_policy.max_records,
                answer_policy.policy_revision,
                ANSWER_RENDERER_SHA256,
                PROMPT_SHA256,
                answer_policy.explicit_recall,
                [UUID(value) for value in binding["selected_claim_ids"]],
                [UUID(value) for value in binding["injected_claim_ids"]],
                "exposed",
                binding["selection_manifest_sha256"],
                binding["injection_manifest_sha256"],
                rendered["content"],
                outbound.decode("utf-8"),
            )
        self.assertEqual(
            stored_binding["injection_manifest_sha256"],
            binding["injection_manifest_sha256"],
        )
        try:
            await self.admin.execute(
                "ALTER TABLE memory.answer_binding "
                "DISABLE TRIGGER answer_binding_immutable"
            )
            await self.admin.execute(
                "UPDATE memory.answer_binding "
                "SET created_at="
                "pg_catalog.transaction_timestamp()-interval '91 days', "
                "expires_at="
                "pg_catalog.transaction_timestamp()-interval '1 day' "
                "WHERE binding_id=$1::uuid",
                stored_binding["binding_id"],
            )
        finally:
            await self.admin.execute(
                "ALTER TABLE memory.answer_binding "
                "ENABLE TRIGGER answer_binding_immutable"
            )
        self.assertEqual(
            await self.worker.fetchval(
                "SELECT memory_private.purge_expired_answer_bindings(10)"
            ),
            1,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM memory.answer_binding "
                "WHERE binding_id=$1::uuid",
                stored_binding["binding_id"],
            ),
            0,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM memory.audit_event "
                "WHERE owner_user_id=$1::uuid "
                "AND object_type='answer_binding' AND object_id=$2::uuid "
                "AND transition_code='answer_binding_retention_purged' "
                "AND reason_code='retention_expired' "
                "AND new_state_sha256=event_sha256",
                OWNER_A,
                stored_binding["binding_id"],
            ),
            1,
        )

        replacement = {
            "epistemic_state": "supported",
            "object_display_name": None,
            "object_entity_type": None,
            "object_kind": "literal",
            "object_literal": "amber",
            "sensitivity": "ordinary",
        }
        async with self.owner_context(self.api, OWNER_A):
            correction = await self.api.fetchrow(
                "SELECT * FROM memory_private.correct_claim("
                "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::jsonb)",
                CORRECTION_OPERATION_ID,
                claim_id,
                claims[0]["revision_sha256"],
                claims[0]["state_sha256"],
                catalog_sha256,
                replacement,
            )
        self.assertEqual(correction["outcome"], "correction_pending")
        stale_results = self.qdrant.search(ALIAS, OWNER_A, list(point["vector"]))
        self.assertEqual(len(stale_results), 1)
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        self.assertEqual(
            revalidate_candidates(
                OWNER_A,
                [self.qdrant_candidate(stale_results[0])],
                [],
                policy=answer_policy,
                predicate_catalog=PREDICATE_CATALOG,
                authorization_at=authorization_at,
            ),
            (),
        )
        correction_delete_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase2_projector',1,120)"
        )
        await self.finish_delete(
            correction_delete_lease, physical=PHYSICAL_A, finalize=False
        )
        async with self.owner_context(self.api, OWNER_A):
            proposals = await self.api.fetch(
                "SELECT * FROM memory_private.list_proposals("
                "100,NULL::timestamptz,NULL::uuid)"
            )
        correction_proposal = next(
            row for row in proposals if row["proposal_id"] == correction["proposal_id"]
        )
        async with self.owner_context(self.api, OWNER_A):
            corrected_review = await self.api.fetchrow(
                "SELECT * FROM memory_private.review_proposal("
                "$1::uuid,$2::uuid,'admitted'::text,$3::text,$4::text,"
                "$5::text,$6::text,$7::text,$8::text[])",
                correction["review_operation_id"],
                correction["proposal_id"],
                correction_proposal["proposal_sha256"],
                correction_proposal["source_sha256"],
                correction_proposal["selected_sha256"],
                correction_proposal["selection_binding_sha256"],
                correction_proposal["predicate_catalog_sha256"],
                ["explicit_owner_review"],
            )
        self.assertEqual(corrected_review["outcome"], "admitted")
        corrected_claim = (await self.read_claims(OWNER_A, [claim_id]))[0]
        self.assertEqual(corrected_claim["revision_number"], 2)
        corrected_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase2_projector',1,120)"
        )
        corrected_point = build_projection_point(
            corrected_claim,
            self.projection_outbox(corrected_lease),
            deterministic_vector(),
        )
        self.qdrant.upsert(PHYSICAL_A, corrected_point)
        await self.finish_upsert(corrected_lease, corrected_point, PHYSICAL_A)
        corrected_candidate = self.qdrant_candidate(
            self.qdrant.search(ALIAS, OWNER_A, list(corrected_point["vector"]))[0]
        )
        self.assertEqual(corrected_candidate["revision_number"], 2)

        self.qdrant.create_collection(PHYSICAL_B)
        del corrected_claim
        del corrected_lease
        del corrected_point
        rebuild_rows = await self.worker.fetch(
            "SELECT * FROM memory_private.read_projection_rebuild_batch("
            "NULL::uuid,NULL::uuid,100)"
        )
        self.assertEqual(len(rebuild_rows), 1)
        rebuild_inputs = projection_rebuild_row_to_inputs(dict(rebuild_rows[0]))
        self.assertEqual(rebuild_inputs["applied_physical_collection"], PHYSICAL_A)
        rebuilt_point = build_rebuild_projection_point(
            rebuild_inputs["claim"],
            rebuild_inputs["outbox"],
            deterministic_vector(),
        )
        self.assertEqual(
            rebuilt_point["payload"]["vector_sha256"],
            rebuild_inputs["expected_vector_sha256"],
        )
        self.qdrant.upsert(PHYSICAL_B, rebuilt_point)
        point_a = self.qdrant.retrieve(PHYSICAL_A, claim_id)[0]
        point_b = self.qdrant.retrieve(PHYSICAL_B, claim_id)[0]
        rebuild_manifest_a = canonical_sha256(
            "governed_memory.phase2_rebuild_point",
            {
                "point_id": str(point_a["id"]),
                "vector_sha256": vector_sha256(point_a["vector"]),
                "payload": point_a["payload"],
            },
        )
        rebuild_manifest_b = canonical_sha256(
            "governed_memory.phase2_rebuild_point",
            {
                "point_id": str(point_b["id"]),
                "vector_sha256": vector_sha256(point_b["vector"]),
                "payload": point_b["payload"],
            },
        )
        self.assertEqual(rebuild_manifest_a, rebuild_manifest_b)
        direct_a = self.qdrant.search(PHYSICAL_A, OWNER_A, list(rebuilt_point["vector"]))
        direct_b = self.qdrant.search(PHYSICAL_B, OWNER_A, list(rebuilt_point["vector"]))
        candidate_manifest_a = [
            {
                "id": str(item["id"]),
                "payload": item["payload"],
                "score_hex": float(item["score"]).hex(),
            }
            for item in direct_a
        ]
        candidate_manifest_b = [
            {
                "id": str(item["id"]),
                "payload": item["payload"],
                "score_hex": float(item["score"]).hex(),
            }
            for item in direct_b
        ]
        self.assertEqual(
            canonical_sha256(
                "governed_memory.phase2_candidates", candidate_manifest_a
            ),
            canonical_sha256(
                "governed_memory.phase2_candidates", candidate_manifest_b
            ),
        )
        self.qdrant.swap_alias(ALIAS, PHYSICAL_A, PHYSICAL_B)
        self.assertEqual(self.qdrant.aliases().get(ALIAS), PHYSICAL_B)
        corrected_claim = (await self.read_claims(OWNER_A, [claim_id]))[0]

        async with self.owner_context(self.api, OWNER_A):
            retracted = await self.api.fetchrow(
                "SELECT * FROM memory_private.retract_claim("
                "$1::uuid,$2::uuid,$3::text,$4::text)",
                RETRACTION_OPERATION_ID,
                claim_id,
                corrected_claim["revision_sha256"],
                corrected_claim["state_sha256"],
            )
        self.assertEqual(retracted["outcome"], "retracted")
        stale_after_retraction = self.qdrant.search(
            ALIAS, OWNER_A, list(rebuilt_point["vector"])
        )
        self.assertEqual(len(stale_after_retraction), 1)
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        retraction_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase2_projector',1,120)"
        )
        await self.finish_delete(retraction_lease, physical=PHYSICAL_B, finalize=False)

        retracted_state = (await self.list_claims(OWNER_A))[0]
        async with self.owner_context(self.api, OWNER_A):
            deletion = await self.api.fetchrow(
                "SELECT * FROM memory_private.request_claim_deletion("
                "$1::uuid,$2::uuid,$3::text,$4::text)",
                DELETION_OPERATION_ID,
                claim_id,
                retracted_state["revision_sha256"],
                retracted_state["current_state_sha256"],
            )
        self.assertEqual(deletion["outcome"], "deletion_pending")
        deletion_state = (await self.list_claims(OWNER_A))[0]
        deletion_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'phase2_projector',1,120)"
        )
        deletion_receipt = await self.finish_delete(
            deletion_lease,
            physical=PHYSICAL_B,
            finalize=True,
            deletion_state=deletion_state,
        )
        deleted_counts = await self.admin.fetchrow(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM memory.claim "
            " WHERE owner_user_id=$1::uuid) AS claims,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_revision "
            " WHERE owner_user_id=$1::uuid) AS revisions,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_links,"
            "(SELECT pg_catalog.count(*) FROM memory.projection_outbox "
            " WHERE owner_user_id=$1::uuid) AS projection_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.proposal "
            " WHERE owner_user_id=$1::uuid) AS proposals,"
            "(SELECT pg_catalog.count(*) FROM memory.provider_call "
            " WHERE owner_user_id=$1::uuid) AS provider_calls,"
            "(SELECT pg_catalog.count(*) FROM memory.extraction_job "
            " WHERE owner_user_id=$1::uuid) AS extraction_jobs,"
            "(SELECT pg_catalog.count(*) FROM memory.evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.entity "
            " WHERE owner_user_id=$1::uuid) AS entities",
            OWNER_A,
        )
        self.assertEqual(set(dict(deleted_counts).values()), {0})
        retained_deletion = await self.admin.fetchrow(
            "SELECT receipt.receipt_sha256,event.event_sha256 "
            "FROM memory.claim_deletion_receipt AS receipt "
            "JOIN memory.audit_event AS event "
            "ON event.event_id=receipt.audit_event_id "
            "AND event.owner_user_id=receipt.owner_user_id "
            "WHERE receipt.owner_user_id=$1::uuid "
            "AND receipt.claim_id=$2::uuid "
            "AND receipt.operation_id=$3::uuid "
            "AND event.transition_code='claim_deleted' "
            "AND event.reason_code='qdrant_delete_verified'",
            OWNER_A,
            claim_id,
            DELETION_OPERATION_ID,
        )
        self.assertIsNotNone(retained_deletion)
        self.assertEqual(
            retained_deletion["receipt_sha256"],
            retained_deletion["event_sha256"],
        )
        self.assertEqual(await self.list_claims(OWNER_A), [])
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        async with self.owner_context(self.api, OWNER_A):
            deletion_replay = await self.api.fetchrow(
                "SELECT * FROM memory_private.request_claim_deletion("
                "$1::uuid,$2::uuid,$3::text,$4::text)",
                DELETION_OPERATION_ID,
                claim_id,
                retracted_state["revision_sha256"],
                retracted_state["current_state_sha256"],
            )
        self.assertEqual(deletion_replay["outcome"], "replayed")
        self.assertFalse(self.qdrant.retrieve(ALIAS, claim_id))

        receipt = {
            "schema": "governed-memory-phase2-integration-receipt-v1",
            "claim_id": str(claim_id),
            "initial_revision_id": str(claims[0]["revision_id"]),
            "corrected_revision_id": str(corrected_claim["revision_id"]),
            "chat_a_thread_id": str(THREAD_A),
            "chat_b_thread_id": str(THREAD_B),
            "cold_extraction_lease": True,
            "cold_projection_rebuild": True,
            "rebuild_manifest_sha256": rebuild_manifest_b,
            "review_surface_sha256": canonical_sha256(
                "governed_memory.phase2_review_surface",
                _jsonable(review_surface),
            ),
            "answer_binding_sha256": binding["binding_sha256"],
            "deletion_receipt_sha256": deletion_receipt["receipt_sha256"],
            "provider_external_calls": 0,
            "production_data_read": False,
        }
        print("PHASE2_VERTICAL_SLICE_RECEIPT=" + json.dumps(receipt, sort_keys=True))
