from __future__ import annotations

from datetime import datetime
import uuid
from typing import Any

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    EvidenceContextConnectionV1,
    load_memory_evidence_context_v1,
)
from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    MemoryEvidenceContextEnvelopeV2,
    build_memory_evidence_context_envelope_v2,
)


async def load_memory_evidence_context_v2(
    conn: EvidenceContextConnectionV1,
    *,
    expected_owner_user_id: str | uuid.UUID,
    target_evidence_id: str | uuid.UUID,
    expected_target_content_sha256: str,
    max_spans: int = 12,
    max_prior_turns: int = 6,
    max_context_chars: int = 6000,
    max_turn_chars: int = 2500,
) -> MemoryEvidenceContextEnvelopeV2:
    """Load sibling spans plus bounded prior same-owner/thread turns.

    Prior turns are disambiguating context only. They cannot originate an
    entity, observation, source span, instruction, or durable assertion.
    """

    sibling_context = await load_memory_evidence_context_v1(
        conn,
        expected_owner_user_id=expected_owner_user_id,
        target_evidence_id=target_evidence_id,
        expected_target_content_sha256=expected_target_content_sha256,
        max_spans=max_spans,
    )
    try:
        owner = uuid.UUID(sibling_context.owner_user_id)
        source_id = uuid.UUID(sibling_context.source.source_id)
        thread_id = uuid.UUID(sibling_context.source.thread_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceContextContractError(
            "v1 context identifiers are invalid"
        ) from exc
    source_recorded_at = datetime.fromisoformat(
        sibling_context.source.source_recorded_at.replace("Z", "+00:00")
    )

    async with conn.transaction(readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(owner),
        )
        actor = await conn.fetchval(
            "SELECT memory.current_actor_user_id()::text"
        )
        if str(actor) != str(owner):
            raise EvidenceContextContractError(
                "database actor differs from authenticated owner"
            )
        source_hash = await conn.fetchval(
            """
            SELECT encode(digest(text,'sha256'),'hex')
            FROM public.chat_log
            WHERE owner_user_id=$1
              AND id=$2
              AND thread_id=$3
              AND created_at=$4
            """,
            owner,
            source_id,
            thread_id,
            source_recorded_at,
        )
        if source_hash != sibling_context.source.source_content_sha256:
            raise EvidenceContextContractError(
                "target source changed between context reads"
            )
        prior_rows = await conn.fetch(
            """
            SELECT id,owner_user_id,thread_id,request_id,
                   created_at,source,text
            FROM public.chat_log
            WHERE owner_user_id=$1
              AND thread_id=$2
              AND (created_at,id) < ($3,$4)
              AND text IS NOT NULL
              AND btrim(text) <> ''
              AND (
                lower(source) LIKE '%:user'
                OR lower(source) LIKE '%:assistant'
              )
            ORDER BY created_at DESC,id DESC
            LIMIT $5
            """,
            owner,
            thread_id,
            source_recorded_at,
            source_id,
            max(max_prior_turns * 3, max_prior_turns),
        )
        return build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context,
            prior_turn_rows=[dict(row) for row in prior_rows],
            max_prior_turns=max_prior_turns,
            max_context_chars=max_context_chars,
            max_turn_chars=max_turn_chars,
        )
