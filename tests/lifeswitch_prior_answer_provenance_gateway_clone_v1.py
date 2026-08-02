from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg

from rag_engine.lifeswitch_answer_binding_v1 import (
    FinalAnswerLifeSwitchBindingV1,
    LifeSwitchAnswerRecordRefV1,
    _sha256 as binding_sha256,
)
from rag_engine.lifeswitch_answer_provenance_receipt_v1 import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
    LifeSwitchProvenanceSourceRefV1,
    _sha256 as receipt_sha256,
)
from rag_engine.lifeswitch_prior_answer_provenance_runtime_v1 import (
    PostgresPriorLifeSwitchRestrictedReadSessionV1,
    PriorLifeSwitchProvenanceProviderV1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    ATTESTED_ASSISTANT_SOURCE,
    ConversationSnapshotOutcome,
    _snapshot,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
THREAD_A = UUID("33333333-3333-4333-8333-333333333333")
THREAD_B = UUID("44444444-4444-4444-8444-444444444444")
CURRENT_LOG = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
CUTOFF = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
QUESTION = "Where did you get those earlier protein numbers?"
RAW_SECRET = "RAW-PROTEIN-201.8-MUST-NOT-APPEAR"


def sha(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def expect_denied(awaitable, label: str) -> None:
    try:
        await awaitable
    except (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError):
        return
    raise AssertionError(f"unauthorized access unexpectedly succeeded: {label}")


@asynccontextmanager
async def reader_transaction(
    conn: asyncpg.Connection,
    *,
    user_owner: UUID | None,
    lifeswitch_owner: UUID | None,
):
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        if user_owner is not None:
            await conn.execute(
                "select set_config('app.user_id',$1,true)", str(user_owner)
            )
        if lifeswitch_owner is not None:
            await conn.execute(
                "select set_config('app.lifeswitch_owner_id',$1,true)",
                str(lifeswitch_owner),
            )
        await conn.execute("set local role lifeswitch_chat_reader_v1")
        yield


async def create_context(
    conn: asyncpg.Connection,
    *,
    owner: UUID,
    thread: UUID,
    authenticated_owner: UUID | None = None,
    lifeswitch_owner: UUID | None = None,
) -> UUID:
    async with conn.transaction():
        await conn.execute(
            "select set_config('app.user_id',$1,true)",
            str(authenticated_owner or owner),
        )
        await conn.execute(
            "select set_config('app.lifeswitch_owner_id',$1,true)",
            str(lifeswitch_owner or owner),
        )
        value = await conn.fetchval(
            "select lifeswitch_chat.begin_owner_read_context_v1($1,$2,$3,$4)",
            owner,
            thread,
            sha("request"),
            sha("snapshot"),
        )
    require(isinstance(value, UUID), "context binder did not return UUID")
    return value


async def end_context(conn: asyncpg.Connection, context_id: UUID) -> None:
    async with conn.transaction():
        removed = await conn.fetchval(
            "select lifeswitch_chat.end_owner_read_context_v1($1)", context_id
        )
    require(removed is True, "context cleanup failed")


def snapshot(owner: UUID, thread: UUID):
    return _snapshot(
        actor=owner,
        thread=thread,
        request_id="prior-provenance-clone-request",
        outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
        current_log_id=CURRENT_LOG,
        cutoff=CUTOFF,
        messages=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="What were my macros?",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="Your LifeSwitch nutrition log was used.",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=QUESTION,
            ),
        ),
        candidate_count=2,
        dropped_count=0,
        message_limit_truncated=False,
    )


def binding(
    *,
    owner: UUID,
    thread: UUID,
    answer: UUID,
    created_at: datetime,
    request_hash: str,
    snapshot_hash: str,
) -> FinalAnswerLifeSwitchBindingV1:
    ref = LifeSwitchAnswerRecordRefV1(
        ordinal=0,
        projection="nutrition_day",
        status="AVAILABLE",
        record_count=1,
        payload_sha256=sha(f"payload|{answer}"),
    )
    payload = {
        "contract_version": "final_answer_lifeswitch_binding_v1",
        "authenticated_actor_user_id": owner,
        "owner_user_id": owner,
        "thread_id": thread,
        "answer_id": answer,
        "request_id_sha256": request_hash,
        "conversation_snapshot_sha256": snapshot_hash,
        "source_assembly_sha256": sha(f"assembly|{answer}"),
        "envelope_sha256": sha(f"envelope|{answer}"),
        "rendered_content_sha256": sha(f"rendered|{answer}"),
        "answer_model_exposed": True,
        "record_count": 1,
        "rendered_tokens": 12,
        "record_refs": (ref,),
        "created_at": created_at,
    }
    return FinalAnswerLifeSwitchBindingV1(
        **payload,
        binding_manifest_sha256=binding_sha256(payload),
    )


def receipt(
    source_binding: FinalAnswerLifeSwitchBindingV1,
    *,
    assistant_text_sha256: str,
    attestation_sha256: str,
    request_hash: str | None = None,
) -> FinalAnswerLifeSwitchProvenanceReceiptV1:
    ref = source_binding.record_refs[0]
    source_ref = LifeSwitchProvenanceSourceRefV1.create(
        ordinal=0,
        projection=ref.projection,
        status=ref.status,
        record_count=ref.record_count,
        window_start_date=date(2026, 7, 18),
        window_end_date=date(2026, 7, 31),
        payload_sha256=ref.payload_sha256,
    )
    payload = {
        "contract_version": "final_answer_lifeswitch_provenance_receipt_v1",
        "authenticated_actor_user_id": source_binding.owner_user_id,
        "owner_user_id": source_binding.owner_user_id,
        "thread_id": source_binding.thread_id,
        "answer_id": source_binding.answer_id,
        "request_id_sha256": request_hash or source_binding.request_id_sha256,
        "conversation_snapshot_sha256": (
            source_binding.conversation_snapshot_sha256
        ),
        "lifeswitch_prepared_context_manifest_sha256": sha(
            f"prepared|{source_binding.answer_id}"
        ),
        "data_plan_sha256": sha(f"plan|{source_binding.answer_id}"),
        "lifeswitch_binding_manifest_sha256": (
            source_binding.binding_manifest_sha256
        ),
        "source_assembly_sha256": source_binding.source_assembly_sha256,
        "envelope_sha256": source_binding.envelope_sha256,
        "assistant_text_sha256": assistant_text_sha256,
        "attestation_sha256": attestation_sha256,
        "answer_model_exposed": True,
        "source_refs": (source_ref,),
        "created_at": source_binding.created_at,
    }
    return FinalAnswerLifeSwitchProvenanceReceiptV1(
        **payload,
        receipt_manifest_sha256=receipt_sha256(payload),
    )


async def seed_answer(
    admin: asyncpg.Connection,
    *,
    owner: UUID,
    thread: UUID,
    answer: UUID,
    created_at: datetime,
    answer_text: str,
    chat_source: str = ATTESTED_ASSISTANT_SOURCE,
    attested_text: str | None = None,
    attestation_request_hash: str | None = None,
    with_receipt: bool = True,
    receipt_request_hash: str | None = None,
) -> None:
    request_hash = sha(f"request|{answer}")
    snapshot_hash = sha(f"snapshot|{answer}")
    attestation_request = attestation_request_hash or request_hash
    source_binding = binding(
        owner=owner,
        thread=thread,
        answer=answer,
        created_at=created_at,
        request_hash=request_hash,
        snapshot_hash=snapshot_hash,
    )
    text_hash = sha(attested_text if attested_text is not None else answer_text)
    attestation_hash = sha(f"attestation|{answer}")
    source_receipt = receipt(
        source_binding,
        assistant_text_sha256=text_hash,
        attestation_sha256=attestation_hash,
        request_hash=receipt_request_hash,
    )
    async with admin.transaction():
        await admin.execute(
            "select set_config('app.user_id',$1,true)", str(owner)
        )
        await admin.execute(
            "select set_config('app.lifeswitch_owner_id',$1,true)", str(owner)
        )
        await admin.execute(
            """
            insert into public.chat_log(
              id,owner_user_id,user_id,source,text,tags,thread_id,
              vantage_id,request_id,created_at
            ) values($1,$2,$3,$4,$5,$6,$7,'RESSE',$8,$9)
            """,
            answer,
            owner,
            str(owner),
            chat_source,
            answer_text,
            ["assistant", "chat", "server_attested", "clone_fixture"],
            thread,
            f"clone-{answer}",
            created_at,
        )
        await admin.execute(
            """
            insert into memory.assistant_transcript_attestation_v1(
              answer_id,owner_user_id,thread_id,chat_log_id,
              request_id_sha256,conversation_snapshot_sha256,
              trusted_plan_sha256,provider_request_sha256,
              provider_response_sha256,provider_response_id,output_kind,
              assistant_text_sha256,attestation_sha256,created_at
            ) values($1,$2,$3,$1,$4,$5,$6,$7,$8,$9,'content',$10,$11,$12)
            """,
            answer,
            owner,
            thread,
            attestation_request,
            snapshot_hash,
            sha(f"trusted-plan|{answer}"),
            sha(f"provider-request|{answer}"),
            sha(f"provider-response|{answer}"),
            f"clone-provider-{answer}",
            text_hash,
            attestation_hash,
            created_at,
        )
        await admin.execute(
            """
            insert into lifeswitch_chat.final_answer_lifeswitch_binding_v1(
              answer_id,authenticated_actor_user_id,owner_user_id,thread_id,
              request_id_sha256,conversation_snapshot_sha256,
              source_assembly_sha256,envelope_sha256,rendered_content_sha256,
              answer_model_exposed,record_count,rendered_tokens,record_refs,
              created_at,binding_manifest_sha256
            ) values(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15
            )
            """,
            source_binding.answer_id,
            source_binding.authenticated_actor_user_id,
            source_binding.owner_user_id,
            source_binding.thread_id,
            source_binding.request_id_sha256,
            source_binding.conversation_snapshot_sha256,
            source_binding.source_assembly_sha256,
            source_binding.envelope_sha256,
            source_binding.rendered_content_sha256,
            source_binding.answer_model_exposed,
            source_binding.record_count,
            source_binding.rendered_tokens,
            json.dumps(
                [item.model_dump(mode="json") for item in source_binding.record_refs],
                separators=(",", ":"),
                sort_keys=True,
            ),
            source_binding.created_at,
            source_binding.binding_manifest_sha256,
        )
        if with_receipt:
            await admin.execute(
                """
                insert into lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1(
                  answer_id,authenticated_actor_user_id,owner_user_id,thread_id,
                  request_id_sha256,conversation_snapshot_sha256,
                  lifeswitch_prepared_context_manifest_sha256,data_plan_sha256,
                  lifeswitch_binding_manifest_sha256,source_assembly_sha256,
                  envelope_sha256,assistant_text_sha256,attestation_sha256,
                  answer_model_exposed,source_refs,created_at,receipt_manifest_sha256
                ) values(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16,$17
                )
                """,
                source_receipt.answer_id,
                source_receipt.authenticated_actor_user_id,
                source_receipt.owner_user_id,
                source_receipt.thread_id,
                source_receipt.request_id_sha256,
                source_receipt.conversation_snapshot_sha256,
                source_receipt.lifeswitch_prepared_context_manifest_sha256,
                source_receipt.data_plan_sha256,
                source_receipt.lifeswitch_binding_manifest_sha256,
                source_receipt.source_assembly_sha256,
                source_receipt.envelope_sha256,
                source_receipt.assistant_text_sha256,
                source_receipt.attestation_sha256,
                source_receipt.answer_model_exposed,
                json.dumps(
                    [item.model_dump(mode="json") for item in source_receipt.source_refs],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                source_receipt.created_at,
                source_receipt.receipt_manifest_sha256,
            )


class ForbiddenSession:
    calls = 0

    async def select(self, **_kwargs):
        self.calls += 1
        raise AssertionError("OFF provenance path touched the database session")


async def main() -> None:
    dsn = os.environ["LIFESWITCH_PROVENANCE_TEST_DSN"]
    admin_dsn = os.environ["LIFESWITCH_PROVENANCE_ADMIN_DSN"]
    expected_database = os.environ["LIFESWITCH_PROVENANCE_EXPECTED_DATABASE"]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=20)
    conn = await asyncpg.connect(dsn, command_timeout=20)
    second = await asyncpg.connect(dsn, command_timeout=20)
    admin = await asyncpg.connect(admin_dsn, command_timeout=20)
    contexts: set[UUID] = set()
    checks: list[str] = []
    try:
        require(await conn.fetchval("select session_user") == "brains_app", "wrong reader")
        require(await admin.fetchval("select session_user") == "sage", "wrong admin")
        require(
            await admin.fetchval("select current_database()") == expected_database,
            "admin connection is not the disposable clone",
        )
        require(
            await conn.fetchval("select current_database()") == expected_database,
            "reader connection is not the disposable clone",
        )
        checks.append("disposable_clone_bound")

        relation = await conn.fetchrow(
            """
            select relrowsecurity,relforcerowsecurity,
                   coalesce(array_to_string(reloptions,','),'') as options
            from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid=c.relnamespace
            where n.nspname='lifeswitch_chat'
              and c.relname='final_answer_lifeswitch_provenance_receipt_v1'
            """
        )
        require(
            relation is not None
            and relation["relrowsecurity"]
            and relation["relforcerowsecurity"],
            "receipt table RLS is not forced",
        )
        barrier = await conn.fetchval(
            """
            select coalesce(array_to_string(reloptions,','),'')
                   like '%security_barrier=true%'
            from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid=c.relnamespace
            where n.nspname='lifeswitch_chat'
              and c.relname='read_prior_answer_lifeswitch_provenance_v1'
            """
        )
        require(barrier is True, "reader view lacks security barrier")
        checks.extend(["forced_rls", "security_barrier"])

        for owner, thread in ((OWNER_A, THREAD_A), (OWNER_B, THREAD_B)):
            async with admin.transaction():
                await admin.execute(
                    "select set_config('app.user_id',$1,true)", str(owner)
                )
                await admin.execute(
                    """
                    insert into public.threads(id,user_id,title,owner_user_id)
                    values($1,$2,'Clone provenance fixture',$3)
                    """,
                    thread,
                    str(owner),
                    owner,
                )

        valid_exact = UUID("50000000-0000-4000-8000-000000000001")
        valid_historical = UUID("50000000-0000-4000-8000-000000000002")
        cross_linked_receipt = UUID("50000000-0000-4000-8000-000000000003")
        wrong_source = UUID("50000000-0000-4000-8000-000000000004")
        wrong_digest = UUID("50000000-0000-4000-8000-000000000005")
        wrong_attestation = UUID("50000000-0000-4000-8000-000000000006")
        future_answer = UUID("50000000-0000-4000-8000-000000000007")
        owner_b_answer = UUID("60000000-0000-4000-8000-000000000001")

        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=valid_exact,
            created_at=CUTOFF - timedelta(minutes=7),
            answer_text=f"Valid exact {RAW_SECRET}",
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=valid_historical,
            created_at=CUTOFF - timedelta(minutes=6),
            answer_text=f"Valid historical {RAW_SECRET}",
            with_receipt=False,
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=cross_linked_receipt,
            created_at=CUTOFF - timedelta(minutes=5),
            answer_text=f"Cross-linked {RAW_SECRET}",
            receipt_request_hash=sha("different-receipt-request"),
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=wrong_source,
            created_at=CUTOFF - timedelta(minutes=4),
            answer_text=f"Wrong source {RAW_SECRET}",
            chat_source="backend/other:assistant:v1",
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=wrong_digest,
            created_at=CUTOFF - timedelta(minutes=3),
            answer_text=f"Wrong digest {RAW_SECRET}",
            attested_text="different transcript",
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=wrong_attestation,
            created_at=CUTOFF - timedelta(minutes=2),
            answer_text=f"Wrong attestation {RAW_SECRET}",
            attestation_request_hash=sha("different-attestation-request"),
        )
        await seed_answer(
            admin,
            owner=OWNER_A,
            thread=THREAD_A,
            answer=future_answer,
            created_at=CUTOFF + timedelta(seconds=1),
            answer_text=f"Future {RAW_SECRET}",
        )
        await seed_answer(
            admin,
            owner=OWNER_B,
            thread=THREAD_B,
            answer=owner_b_answer,
            created_at=CUTOFF - timedelta(minutes=1),
            answer_text=f"Owner B {RAW_SECRET}",
        )
        checks.append("synthetic_two_owner_fixture")

        selected_a = await PostgresPriorLifeSwitchRestrictedReadSessionV1(pool).select(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=snapshot(OWNER_A, THREAD_A),
        )
        require(selected_a.status == "SELECTED", "owner A path was not selected")
        assert selected_a.envelope is not None
        selected_ids = {item.answer_id for item in selected_a.envelope.responses}
        require(
            selected_ids == {valid_exact, valid_historical},
            "tampered, future, or cross-owner response escaped selection",
        )
        statuses = {item.answer_id: item.provenance_status for item in selected_a.envelope.responses}
        require(statuses[valid_exact] == "exact_receipt", "exact receipt was lost")
        require(
            statuses[valid_historical] == "historical_binding_only",
            "historical fallback was not bounded",
        )
        require(RAW_SECRET not in selected_a.envelope.content, "raw answer leaked")
        require(str(OWNER_A) not in selected_a.envelope.content, "owner UUID leaked")
        checks.extend(
            [
                "transaction_bound_selected_path",
                "exact_and_historical_paths",
                "tamper_and_cutoff_rejection",
                "content_free_output",
            ]
        )

        selected_b = await PostgresPriorLifeSwitchRestrictedReadSessionV1(pool).select(
            authenticated_actor_user_id=OWNER_B,
            conversation_snapshot=snapshot(OWNER_B, THREAD_B),
        )
        require(selected_b.status == "SELECTED", "owner B path was not selected")
        assert selected_b.envelope is not None
        require(
            {item.answer_id for item in selected_b.envelope.responses}
            == {owner_b_answer},
            "cross-owner response leaked",
        )
        checks.append("two_owner_isolation")

        await expect_denied(
            create_context(
                conn,
                owner=OWNER_B,
                thread=THREAD_B,
                authenticated_owner=OWNER_A,
                lifeswitch_owner=OWNER_A,
            ),
            "forged owner context",
        )
        checks.append("forged_owner_context_denied")

        context_a = await create_context(conn, owner=OWNER_A, thread=THREAD_A)
        contexts.add(context_a)
        async with reader_transaction(
            conn,
            user_owner=OWNER_B,
            lifeswitch_owner=OWNER_B,
        ):
            forged_rows = await conn.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                context_a,
            )
        require(not forged_rows, "GUC forgery exposed owner A")
        async with reader_transaction(
            conn,
            user_owner=OWNER_A,
            lifeswitch_owner=None,
        ):
            incomplete_rows = await conn.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                context_a,
            )
        require(not incomplete_rows, "single owner GUC bypassed the view")
        async with reader_transaction(
            second,
            user_owner=OWNER_A,
            lifeswitch_owner=OWNER_A,
        ):
            other_backend_rows = await second.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                context_a,
            )
        require(not other_backend_rows, "context crossed backend PID")
        checks.extend(["guc_forgery_denied", "both_gucs_required", "backend_pid_bound"])

        forged_thread = await create_context(conn, owner=OWNER_A, thread=THREAD_B)
        contexts.add(forged_thread)
        async with reader_transaction(
            conn,
            user_owner=OWNER_A,
            lifeswitch_owner=OWNER_A,
        ):
            forged_thread_rows = await conn.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                forged_thread,
            )
        require(not forged_thread_rows, "forged thread exposed another owner")
        checks.append("forged_thread_context_empty")

        ended = await create_context(conn, owner=OWNER_A, thread=THREAD_A)
        await end_context(conn, ended)
        async with reader_transaction(
            conn,
            user_owner=OWNER_A,
            lifeswitch_owner=OWNER_A,
        ):
            ended_rows = await conn.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                ended,
            )
        require(not ended_rows, "ended context remained usable")
        checks.append("ended_context_denied")

        expired = await create_context(conn, owner=OWNER_A, thread=THREAD_A)
        contexts.add(expired)
        await admin.execute(
            "update lifeswitch_chat.owner_read_context_v1 "
            "set created_at=clock_timestamp()-interval '10 minutes', "
            "expires_at=clock_timestamp()-interval '1 second' "
            "where context_id=$1",
            expired,
        )
        async with reader_transaction(
            conn,
            user_owner=OWNER_A,
            lifeswitch_owner=OWNER_A,
        ):
            expired_rows = await conn.fetch(
                "select answer_id from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 where context_id=$1",
                expired,
            )
        require(not expired_rows, "expired context remained usable")
        checks.append("expired_context_denied")

        protected_relations = (
            "lifeswitch_chat.final_answer_lifeswitch_binding_v1",
            "lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1",
            "lifeswitch_chat.owner_read_context_v1",
            "memory.assistant_transcript_attestation_v1",
            "public.chat_log",
            "lifeswitch_agentic.plan_versions",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_training.training_session_current_v",
            "public.lifeswitch_measurement_entries",
        )
        for relation_name in protected_relations:
            async with conn.transaction(readonly=True):
                await conn.execute("set local role lifeswitch_chat_reader_v1")
                await expect_denied(
                    conn.fetchval(f"select count(*) from {relation_name}"),
                    relation_name,
                )
        checks.append("direct_tables_and_sources_denied")

        can_insert = await conn.fetchval(
            "select has_table_privilege('lifeswitch_chat_binding_writer_v1','lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1','INSERT')"
        )
        can_delete = await conn.fetchval(
            "select has_table_privilege('lifeswitch_chat_binding_writer_v1','lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1','DELETE')"
        )
        require(can_insert is True and can_delete is False, "writer is not append-only")
        checks.append("append_only_writer_grant")

        forbidden = ForbiddenSession()
        off = await PriorLifeSwitchProvenanceProviderV1(forbidden).prepare(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=_snapshot(
                actor=OWNER_A,
                thread=THREAD_A,
                request_id="off-clone-request",
                outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
                current_log_id=CURRENT_LOG,
                cutoff=CUTOFF,
                messages=(
                    ResponsePolicyConversationMessageV0_2(
                        role=ConversationRole.USER,
                        content="What are my macros?",
                    ),
                ),
                candidate_count=0,
                dropped_count=0,
                message_limit_truncated=False,
            ),
        )
        require(off.status == "OFF" and forbidden.calls == 0, "OFF touched sources")
        checks.append("off_zero_source_reads")

        print(json.dumps({"checks": checks, "status": "passed"}, sort_keys=True))
    finally:
        for context_id in contexts:
            try:
                await end_context(conn, context_id)
            except Exception:
                pass
        await admin.close()
        await second.close()
        await conn.close()
        await pool.close()


asyncio.run(main())
