from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Protocol, Sequence

from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
    MemoryEvidenceContextEnvelopeV1,
    build_memory_evidence_context_envelope_v1,
)


class ReadOnlyTransactionV1(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, *args: Any) -> None: ...


class EvidenceContextConnectionV1(Protocol):
    def transaction(self, *, readonly: bool) -> ReadOnlyTransactionV1: ...

    async def execute(self, query: str, *args: Any) -> Any: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]: ...


def _mapping(row: Any, field: str) -> dict[str, Any]:
    if row is None:
        raise EvidenceContextContractError(f"{field} is absent")
    try:
        value = dict(row)
    except (TypeError, ValueError) as exc:
        raise EvidenceContextContractError(
            f"{field} must be a database record"
        ) from exc
    return value


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvidenceContextContractError(
                "target evidence metadata is invalid"
            ) from exc
    if not isinstance(value, Mapping):
        raise EvidenceContextContractError(
            "target evidence metadata is absent"
        )
    return value


def _target_centered_window(
    rows: Sequence[Any],
    *,
    target_evidence_id: uuid.UUID,
    max_spans: int,
) -> list[dict[str, Any]]:
    """Return one deterministic, contiguous context window.

    Nearby preceding spans receive two thirds of the available context-only
    positions because definite descriptions usually resolve backward. The
    window shifts at either source boundary so it still uses the full budget.
    """

    canonical = [_mapping(row, "sibling evidence") for row in rows]

    def order_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
        metadata = _metadata(row.get("metadata"))
        try:
            start = int(metadata.get("source_char_start"))
            end = int(metadata.get("source_char_end"))
        except (TypeError, ValueError) as exc:
            raise EvidenceContextContractError(
                "sibling evidence offsets are invalid"
            ) from exc
        return start, end, str(row.get("evidence_id"))

    canonical.sort(key=order_key)
    target_positions = [
        index
        for index, row in enumerate(canonical)
        if str(row.get("evidence_id")) == str(target_evidence_id)
    ]
    if len(target_positions) != 1:
        raise EvidenceContextContractError(
            "target evidence is not visible among its siblings"
        )
    if len(canonical) <= max_spans:
        return canonical

    preceding_budget = ((max_spans - 1) * 2) // 3
    maximum_start = len(canonical) - max_spans
    window_start = max(
        0,
        min(target_positions[0] - preceding_budget, maximum_start),
    )
    return canonical[window_start : window_start + max_spans]


async def load_memory_evidence_context_v1(
    conn: EvidenceContextConnectionV1,
    *,
    expected_owner_user_id: str | uuid.UUID,
    target_evidence_id: str | uuid.UUID,
    expected_target_content_sha256: str,
    max_spans: int = 12,
) -> MemoryEvidenceContextEnvelopeV1:
    """Load one bounded context envelope through forced owner RLS.

    The caller must supply the authenticated owner. This function performs only
    SELECT statements inside a read-only transaction and fails closed if the
    actor, source, target, or sibling bindings differ.
    """

    try:
        owner = uuid.UUID(str(expected_owner_user_id))
        target = uuid.UUID(str(target_evidence_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceContextContractError(
            "loader owner and target must be UUIDs"
        ) from exc
    if not 1 <= max_spans <= 32:
        raise EvidenceContextContractError(
            "max_spans must be between 1 and 32"
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
        target_row = _mapping(
            await conn.fetchrow(
                """
                SELECT evidence_id,owner_user_id,source_system,external_id,
                       content,content_sha256,recorded_at,metadata
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND evidence_id=$2
                  AND content_sha256=$3
                  AND source_system='public.chat_log'
                  AND status='active'
                """,
                owner,
                target,
                expected_target_content_sha256,
            ),
            "target evidence",
        )
        metadata = _metadata(target_row.get("metadata"))
        try:
            source_id = uuid.UUID(str(metadata.get("source_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise EvidenceContextContractError(
                "target source_id is invalid"
            ) from exc
        source_hash = metadata.get("source_content_sha256")
        if not isinstance(source_hash, str):
            raise EvidenceContextContractError(
                "target full-source hash is absent"
            )
        source_row = _mapping(
            await conn.fetchrow(
                """
                SELECT id,owner_user_id,thread_id,request_id,created_at,text
                FROM public.chat_log
                WHERE owner_user_id=$1
                  AND id=$2
                """,
                owner,
                source_id,
            ),
            "raw source",
        )
        preceding_budget = ((max_spans - 1) * 2) // 3
        siblings = _target_centered_window(
            await conn.fetch(
                """
                WITH ranked AS (
                  SELECT evidence_id,owner_user_id,source_system,external_id,
                         content,content_sha256,recorded_at,metadata,
                         row_number() OVER (
                           ORDER BY
                             (metadata->>'source_char_start')::integer,
                             (metadata->>'source_char_end')::integer,
                             evidence_id
                         ) AS source_ordinal,
                         count(*) OVER () AS total_span_count
                  FROM memory.evidence
                  WHERE owner_user_id=$1
                    AND source_system='public.chat_log'
                    AND status='active'
                    AND metadata->>'source_id'=$2
                    AND metadata->>'source_content_sha256'=$3
                ),
                target_position AS (
                  SELECT source_ordinal,total_span_count
                  FROM ranked
                  WHERE evidence_id=$4
                ),
                bounds AS (
                  SELECT greatest(
                           1,
                           least(
                             source_ordinal-$6,
                             greatest(total_span_count-$5+1,1)
                           )
                         ) AS window_start
                  FROM target_position
                )
                SELECT evidence_id,owner_user_id,source_system,external_id,
                       content,content_sha256,recorded_at,metadata
                FROM ranked CROSS JOIN bounds
                WHERE source_ordinal BETWEEN window_start AND window_start+$5-1
                ORDER BY source_ordinal
                """,
                owner,
                str(source_id),
                source_hash,
                target,
                max_spans,
                preceding_budget,
            ),
            target_evidence_id=target,
            max_spans=max_spans,
        )
        return build_memory_evidence_context_envelope_v1(
            expected_owner_user_id=owner,
            target_evidence_id=target,
            expected_target_content_sha256=(
                expected_target_content_sha256
            ),
            source_row=source_row,
            evidence_rows=siblings,
            max_spans=max_spans,
        )
