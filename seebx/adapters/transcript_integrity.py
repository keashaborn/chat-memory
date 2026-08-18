from __future__ import annotations

"""PostgreSQL writes for transcript-integrity attestations."""

from seebx.contracts.transcript_integrity import AssistantTranscriptAttestationV1


async def insert_assistant_transcript_attestation_v1(
    conn: object,
    attestation: AssistantTranscriptAttestationV1,
) -> None:
    """Insert one owner-bound neutral attestation inside the caller transaction."""

    value = AssistantTranscriptAttestationV1.model_validate_json(
        attestation.model_dump_json()
    )
    await conn.execute(
        """
        INSERT INTO chat_integrity.assistant_transcript_attestation_v1(
          answer_id,owner_user_id,thread_id,chat_log_id,
          request_id_sha256,conversation_snapshot_sha256,
          trusted_plan_sha256,provider_request_sha256,
          provider_response_sha256,provider_response_id,output_kind,
          assistant_text_sha256,attestation_sha256,created_at
        )
        VALUES($1,$2,$3,$1,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """,
        value.answer_id,
        value.authenticated_actor_user_id,
        value.thread_id,
        value.request_id_sha256,
        value.conversation_snapshot_sha256,
        value.trusted_plan_sha256,
        value.provider_request_sha256,
        value.provider_response_sha256,
        value.provider_response_id,
        value.output_kind.value,
        value.assistant_text_sha256,
        value.attestation_sha256,
        value.created_at,
    )




__all__ = [
    "insert_assistant_transcript_attestation_v1",
]
