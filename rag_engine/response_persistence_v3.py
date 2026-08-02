from __future__ import annotations

"""Atomic persistence for a versioned response with separate context bindings."""

import hashlib
import json
from typing import Any
from uuid import UUID

from rag_engine.active_thread_selection_v1 import promote_resume_thread_v1
from rag_engine.lifeswitch_answer_binding_store_v1 import (
    persist_lifeswitch_binding_on_connection_v1,
)
from rag_engine.lifeswitch_answer_provenance_store_v1 import (
    persist_lifeswitch_provenance_receipt_on_connection_v1,
)
from rag_engine.response_conversation_snapshot_v1 import ATTESTED_ASSISTANT_SOURCE
from rag_engine.response_finalization_v3 import FinalizedTrustedResponseV3


class ResponsePersistenceV3Error(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("versioned finalized response persistence failed")


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


async def persist_finalized_response_v3(
    conn: object,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    finalized: FinalizedTrustedResponseV3,
) -> None:
    """Persist transcript, attestation, Memory, and LifeSwitch atomically.

    LifeSwitch remains in its own table and RLS domain. The transaction assumes
    the application role may SET ROLE to the narrowly granted binding writer.
    """

    stage = "validation"
    try:
        value = FinalizedTrustedResponseV3.model_validate_json(
            finalized.model_dump_json()
        )
        attestation = value.attestation
        if attestation.authenticated_actor_user_id != owner_user_id:
            raise ValueError("attestation owner mismatch")
        if attestation.thread_id != thread_id:
            raise ValueError("attestation thread mismatch")
        if attestation.request_id_sha256 != _text_sha256(request_id):
            raise ValueError("request differs from attestation")
        if (
            value.lifeswitch_binding is not None
            and value.lifeswitch_binding.owner_user_id != owner_user_id
        ):
            raise ValueError("LifeSwitch binding owner mismatch")
        if (
            value.lifeswitch_provenance_receipt is not None
            and value.lifeswitch_provenance_receipt.owner_user_id != owner_user_id
        ):
            raise ValueError("LifeSwitch provenance receipt owner mismatch")

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
                ["assistant", "chat", "server_attested", "response_v3"],
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
                    json.dumps(
                        binding.model_dump(mode="json"),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    binding.created_at,
                )
            if value.lifeswitch_binding is not None:
                stage = "lifeswitch_binding_insert"
                await persist_lifeswitch_binding_on_connection_v1(
                    conn,
                    value.lifeswitch_binding,
                )
                stage = "lifeswitch_provenance_receipt_insert"
                await persist_lifeswitch_provenance_receipt_on_connection_v1(
                    conn,
                    value.lifeswitch_provenance_receipt,
                )

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
        raise ResponsePersistenceV3Error(stage) from None


__all__ = ["ResponsePersistenceV3Error", "persist_finalized_response_v3"]
