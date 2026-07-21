from __future__ import annotations

"""Atomic append-only persistence for one finalized RESSE response."""

import json
from uuid import UUID

from rag_engine.response_conversation_snapshot_v1 import ATTESTED_ASSISTANT_SOURCE
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
            await conn.execute(
                """
                INSERT INTO memory.assistant_transcript_attestation_v1(
                  answer_id,owner_user_id,thread_id,chat_log_id,
                  request_id_sha256,conversation_snapshot_sha256,
                  trusted_plan_sha256,provider_request_sha256,
                  provider_response_sha256,provider_response_id,output_kind,
                  assistant_text_sha256,attestation_sha256,created_at
                )
                VALUES($1,$2,$3,$1,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                value.answer_id,
                owner_user_id,
                thread_id,
                attestation.request_id_sha256,
                attestation.conversation_snapshot_sha256,
                attestation.trusted_plan_sha256,
                attestation.provider_request_sha256,
                attestation.provider_response_sha256,
                attestation.provider_response_id,
                attestation.output_kind.value,
                attestation.assistant_text_sha256,
                attestation.attestation_sha256,
                attestation.created_at,
            )
            if value.memory_binding is not None:
                stage = "memory_binding_insert"
                binding = value.memory_binding
                await conn.execute(
                    """
                    INSERT INTO memory.final_answer_memory_binding_v1(
                      answer_id,owner_user_id,thread_id,
                      binding_manifest_sha256,binding,created_at
                    )
                    VALUES($1,$2,$3,$4,$5::jsonb,$6)
                    """,
                    value.answer_id,
                    owner_user_id,
                    thread_id,
                    binding.binding_manifest_sha256,
                    json.dumps(binding.model_dump(mode="json"), separators=(",", ":"), sort_keys=True),
                    binding.created_at,
                )
            stage = "thread_touch"
            await conn.execute(
                "UPDATE public.threads SET updated_at=now() WHERE owner_user_id=$1 AND id=$2",
                owner_user_id,
                thread_id,
            )
    except Exception:
        raise ResponsePersistenceError(stage) from None


__all__ = ["ResponsePersistenceError", "persist_finalized_response_v1"]
