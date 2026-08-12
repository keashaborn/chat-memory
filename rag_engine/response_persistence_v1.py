from __future__ import annotations

"""Atomic append-only persistence for one finalized RESSE response."""

from uuid import UUID

from rag_engine.active_thread_selection_v1 import promote_resume_thread_v1
from rag_engine.chat_integrity import (
    ATTESTED_ASSISTANT_SOURCE,
    insert_assistant_transcript_attestation_v1,
    text_sha256,
)
from rag_engine.response_finalization_v1 import FinalizedTrustedResponseV1


class ResponsePersistenceError(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("finalized response persistence failed")


async def persist_finalized_response_v1(
    conn: object,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    finalized: FinalizedTrustedResponseV1,
) -> None:
    stage = "validation"
    try:
        value = FinalizedTrustedResponseV1.model_validate_json(
            finalized.model_dump_json()
        )
        if value.attestation.authenticated_actor_user_id != owner_user_id:
            raise ValueError("attestation owner mismatch")
        if value.attestation.thread_id != thread_id:
            raise ValueError("attestation thread mismatch")
        if value.attestation.request_id_sha256 != text_sha256(request_id):
            raise ValueError("request differs from attestation")
        stage = "transaction"
        async with conn.transaction():
            stage = "actor_scope"
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner_user_id))
            stage = "thread_owner_check"
            owns_thread = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM public.threads
                  WHERE owner_user_id=$1 AND id=$2
                )
                """,
                owner_user_id,
                thread_id,
            )
            if not owns_thread:
                raise ValueError("owner thread is absent")

            attestation = value.attestation
            stage = "chat_log_insert"
            await conn.execute(
                """
                INSERT INTO public.chat_log(
                  id,owner_user_id,user_id,user_id_alias,source,text,tags,
                  thread_id,vantage_id,request_id,created_at
                )
                VALUES($1,$2,$3,NULL,$4,$5,$6,$7,'RESSE',$8,$9)
                """,
                value.answer_id,
                owner_user_id,
                str(owner_user_id),
                ATTESTED_ASSISTANT_SOURCE,
                value.assistant_text,
                ["assistant", "chat", "server_attested", "resse_v0_2"],
                thread_id,
                request_id,
                attestation.created_at,
            )
            stage = "attestation_insert"
            await insert_assistant_transcript_attestation_v1(conn, attestation)
            stage = "thread_touch"
            await conn.execute(
                "UPDATE public.threads SET updated_at=now() WHERE owner_user_id=$1 AND id=$2",
                owner_user_id,
                thread_id,
            )
            stage = "resume_target_promotion"
            await promote_resume_thread_v1(
                conn,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
    except Exception:
        raise ResponsePersistenceError(stage) from None


__all__ = ["ResponsePersistenceError", "persist_finalized_response_v1"]
