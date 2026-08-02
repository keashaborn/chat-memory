from __future__ import annotations

"""Restricted append-only persistence for LifeSwitch provenance receipts."""

import json
from typing import Any

from rag_engine.lifeswitch_answer_provenance_receipt_v1 import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
)


LIFESWITCH_BINDING_WRITER_ROLE = "lifeswitch_chat_binding_writer_v1"


class LifeSwitchProvenancePersistenceError(RuntimeError):
    pass


async def persist_lifeswitch_provenance_receipt_on_connection_v1(
    conn: Any,
    receipt: FinalAnswerLifeSwitchProvenanceReceiptV1,
) -> None:
    value = FinalAnswerLifeSwitchProvenanceReceiptV1.model_validate_json(
        receipt.model_dump_json()
    )
    await conn.execute(
        "select set_config('app.lifeswitch_owner_id',$1,true)",
        str(value.owner_user_id),
    )
    await conn.execute(f"set local role {LIFESWITCH_BINDING_WRITER_ROLE}")
    await conn.execute(
        """
        insert into lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1(
          answer_id,authenticated_actor_user_id,owner_user_id,thread_id,
          request_id_sha256,conversation_snapshot_sha256,
          lifeswitch_prepared_context_manifest_sha256,data_plan_sha256,
          lifeswitch_binding_manifest_sha256,source_assembly_sha256,envelope_sha256,
          assistant_text_sha256,attestation_sha256,answer_model_exposed,
          source_refs,created_at,receipt_manifest_sha256
        ) values (
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16,$17
        )
        on conflict (answer_id) do nothing
        """,
        value.answer_id,
        value.authenticated_actor_user_id,
        value.owner_user_id,
        value.thread_id,
        value.request_id_sha256,
        value.conversation_snapshot_sha256,
        value.lifeswitch_prepared_context_manifest_sha256,
        value.data_plan_sha256,
        value.lifeswitch_binding_manifest_sha256,
        value.source_assembly_sha256,
        value.envelope_sha256,
        value.assistant_text_sha256,
        value.attestation_sha256,
        value.answer_model_exposed,
        json.dumps(
            [item.model_dump(mode="json") for item in value.source_refs],
            separators=(",", ":"),
            sort_keys=True,
        ),
        value.created_at,
        value.receipt_manifest_sha256,
    )
    stored = await conn.fetchval(
        """
        select receipt_manifest_sha256
        from lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1
        where answer_id=$1 and owner_user_id=$2
        """,
        value.answer_id,
        value.owner_user_id,
    )
    if stored != value.receipt_manifest_sha256:
        raise ValueError("existing LifeSwitch provenance receipt differs")
    await conn.execute("reset role")


__all__ = [
    "LifeSwitchProvenancePersistenceError",
    "persist_lifeswitch_provenance_receipt_on_connection_v1",
]
