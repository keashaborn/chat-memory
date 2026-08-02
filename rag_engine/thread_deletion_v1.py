from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from qdrant_client.http import models as qmodels

from .memory_v1_qdrant_rebuild_contract_v1 import qdrant_mutation_lock


THREAD_DELETION_CONTRACT_VERSION = "thread_deletion_v1"
_EVIDENCE_DELETE_NAMESPACE = uuid.UUID("d67c7266-37f5-4f9b-9025-f00dca324173")
_QDRANT_BATCH_SIZE = 256


class ThreadDeletionV1Error(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ThreadDeletionCountsV1:
    governed_evidence_tombstoned: int
    unsupported_claim_projections_verified_absent: int
    retrieval_traces_deleted: int
    answer_bindings_deleted: int
    attestations_deleted: int
    telemetry_events_deleted: int
    transcript_rows_deleted: int
    threads_deleted: int


@dataclass(frozen=True)
class ThreadDeletionResultV1:
    contract_version: str
    owner_user_id: uuid.UUID
    thread_id: uuid.UUID
    status: str
    qdrant_verified: bool
    retained_audit_tombstones: bool
    counts: ThreadDeletionCountsV1

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "status": self.status,
            "owner_user_id": str(self.owner_user_id),
            "thread_id": str(self.thread_id),
            "qdrant_verified": self.qdrant_verified,
            "retained_audit_tombstones": self.retained_audit_tombstones,
            "counts": {
                "governed_evidence_tombstoned": (
                    self.counts.governed_evidence_tombstoned
                ),
                "unsupported_claim_projections_verified_absent": (
                    self.counts.unsupported_claim_projections_verified_absent
                ),
                "retrieval_traces_deleted": self.counts.retrieval_traces_deleted,
                "answer_bindings_deleted": self.counts.answer_bindings_deleted,
                "attestations_deleted": self.counts.attestations_deleted,
                "telemetry_events_deleted": self.counts.telemetry_events_deleted,
                "transcript_rows_deleted": self.counts.transcript_rows_deleted,
                "threads_deleted": self.counts.threads_deleted,
            },
        }


def _canonical_uuid(value: str | uuid.UUID, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ThreadDeletionV1Error(
            f"invalid_{field}",
            retryable=False,
        ) from exc


def _delete_count(command_tag: str) -> int:
    try:
        operation, count = str(command_tag).rsplit(" ", 1)
        if operation != "DELETE":
            raise ValueError("not a DELETE command tag")
        parsed = int(count)
    except (TypeError, ValueError) as exc:
        raise ThreadDeletionV1Error(
            "invalid_database_delete_result",
            retryable=True,
        ) from exc
    if parsed < 0:
        raise ThreadDeletionV1Error(
            "invalid_database_delete_result",
            retryable=True,
        )
    return parsed


def _batches(values: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for offset in range(0, len(values), _QDRANT_BATCH_SIZE):
        yield values[offset : offset + _QDRANT_BATCH_SIZE]


def _claim_collection() -> str:
    value = os.environ.get("MEMORY_V1_COLLECTION", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,255}", value):
        raise ThreadDeletionV1Error(
            "invalid_memory_claim_collection",
            retryable=False,
        )
    return value


def _raw_thread_filter(
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID,
) -> qmodels.Filter:
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="owner_user_id",
                match=qmodels.MatchValue(value=str(owner_user_id)),
            ),
            qmodels.FieldCondition(
                key="thread_id",
                match=qmodels.MatchValue(value=str(thread_id)),
            ),
        ]
    )


def _claim_filter(
    owner_user_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
) -> qmodels.Filter:
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="owner_user_id",
                match=qmodels.MatchValue(value=str(owner_user_id)),
            ),
            qmodels.HasIdCondition(has_id=[str(claim_id) for claim_id in claim_ids]),
        ]
    )


def _delete_and_verify_qdrant(
    qdrant: Any,
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID,
    unsupported_claim_ids: Sequence[uuid.UUID],
) -> None:
    with qdrant_mutation_lock(exclusive=False):
        _delete_and_verify_qdrant_locked(
            qdrant, owner_user_id, thread_id, unsupported_claim_ids
        )


def _delete_and_verify_qdrant_locked(
    qdrant: Any,
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID,
    unsupported_claim_ids: Sequence[uuid.UUID],
) -> None:
    claim_collection = _claim_collection()
    raw_filter = _raw_thread_filter(owner_user_id, thread_id)
    qdrant.delete(
        collection_name="memory_raw",
        points_selector=qmodels.FilterSelector(filter=raw_filter),
        wait=True,
    )
    remaining_raw, _ = qdrant.scroll(
        collection_name="memory_raw",
        scroll_filter=raw_filter,
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    if remaining_raw:
        raise ThreadDeletionV1Error(
            "qdrant_raw_cleanup_not_verified",
            retryable=True,
        )

    for batch in _batches(unsupported_claim_ids):
        claim_filter = _claim_filter(owner_user_id, batch)
        qdrant.delete(
            collection_name=claim_collection,
            points_selector=qmodels.FilterSelector(filter=claim_filter),
            wait=True,
        )
        remaining_claims, _ = qdrant.scroll(
            collection_name=claim_collection,
            scroll_filter=claim_filter,
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if remaining_claims:
            raise ThreadDeletionV1Error(
                "qdrant_claim_cleanup_not_verified",
                retryable=True,
            )


def _evidence_request_id(
    owner_user_id: uuid.UUID,
    thread_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> uuid.UUID:
    return uuid.uuid5(
        _EVIDENCE_DELETE_NAMESPACE,
        f"{owner_user_id}:{thread_id}:{evidence_id}:delete_tombstone",
    )


def memory_source_lock_key_v1(
    owner_user_id: str | uuid.UUID,
    source_system: str,
    source_external_id: str | uuid.UUID,
) -> str:
    owner = _canonical_uuid(owner_user_id, "owner_user_id")
    system = str(source_system or "").strip()
    external_id = str(source_external_id or "").strip()
    if not system or not external_id:
        raise ThreadDeletionV1Error(
            "invalid_memory_source_lock_identity",
            retryable=False,
        )
    return f"{owner}:{system}:{external_id}"


async def delete_thread_v1(
    conn: Any,
    qdrant: Any,
    *,
    owner_user_id: str | uuid.UUID,
    thread_id: str | uuid.UUID,
) -> ThreadDeletionResultV1:
    owner = _canonical_uuid(owner_user_id, "owner_user_id")
    thread = _canonical_uuid(thread_id, "thread_id")

    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)",
            str(owner),
        )
        locked_thread = await conn.fetchval(
            """
            SELECT id
            FROM public.threads
            WHERE owner_user_id=$1 AND id=$2
            FOR UPDATE
            """,
            owner,
            thread,
        )
        if locked_thread is None:
            raise ThreadDeletionV1Error(
                "thread_not_found",
                retryable=False,
            )

        governed_thread_binding = await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1
              FROM memory.project_thread_binding_event
              WHERE owner_user_id=$1 AND thread_id=$2
            )
            """,
            owner,
            thread,
        )
        if governed_thread_binding:
            raise ThreadDeletionV1Error(
                "governed_project_thread_erasure_required",
                retryable=False,
            )

        message_rows = await conn.fetch(
            """
            SELECT id
            FROM public.chat_log
            WHERE owner_user_id=$1 AND thread_id=$2
            ORDER BY id
            """,
            owner,
            thread,
        )
        message_ids = tuple(uuid.UUID(str(row["id"])) for row in message_rows)
        source_ids = tuple(str(message_id) for message_id in message_ids)
        prefixed_source_ids = tuple(
            f"chat_log:{message_id}" for message_id in message_ids
        )

        evidence_rows = []
        if source_ids:
            for source_id in source_ids:
                await conn.execute(
                    """
                    SELECT pg_advisory_xact_lock(hashtextextended($1,0))
                    """,
                    memory_source_lock_key_v1(
                        owner,
                        "public.chat_log",
                        source_id,
                    ),
                )
            await conn.fetch(
                """
                SELECT job_id
                FROM memory.consolidation_job
                WHERE owner_user_id=$1
                  AND source_system='public.chat_log'
                  AND source_external_id=ANY($2::text[])
                ORDER BY job_id
                FOR UPDATE
                """,
                owner,
                source_ids,
            )
            evidence_rows = await conn.fetch(
                """
                SELECT evidence_id
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND source_system='public.chat_log'
                  AND (
                    external_id=ANY($2::text[])
                    OR external_id=ANY($3::text[])
                  )
                ORDER BY evidence_id
                """,
                owner,
                source_ids,
                prefixed_source_ids,
            )
        evidence_ids = tuple(
            uuid.UUID(str(row["evidence_id"])) for row in evidence_rows
        )

        tombstoned = 0
        for evidence_id in evidence_ids:
            lifecycle_row = await conn.fetchrow(
                """
                SELECT outcome, resulting_status
                FROM memory.transition_evidence_lifecycle(
                  $1, $2, 'delete_tombstone', 'user_request',
                  'user', $3, $4::jsonb
                )
                """,
                evidence_id,
                _evidence_request_id(owner, thread, evidence_id),
                THREAD_DELETION_CONTRACT_VERSION,
                json.dumps(
                    {
                        "contract_version": THREAD_DELETION_CONTRACT_VERSION,
                        "thread_id": str(thread),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            if lifecycle_row is None or str(lifecycle_row["resulting_status"]) != "deleted":
                raise ThreadDeletionV1Error(
                    "governed_evidence_tombstone_failed",
                    retryable=True,
                )
            tombstoned += 1

        unsupported_claim_rows = []
        if evidence_ids:
            unsupported_claim_rows = await conn.fetch(
                """
                SELECT DISTINCT linked.claim_id
                FROM memory.claim_evidence AS linked
                WHERE linked.owner_user_id=$1
                  AND linked.evidence_id=ANY($2::uuid[])
                  AND NOT EXISTS (
                    SELECT 1
                    FROM memory.claim_evidence AS remaining_link
                    JOIN memory.evidence AS remaining_evidence
                      ON remaining_evidence.owner_user_id=remaining_link.owner_user_id
                     AND remaining_evidence.evidence_id=remaining_link.evidence_id
                    WHERE remaining_link.owner_user_id=linked.owner_user_id
                      AND remaining_link.claim_id=linked.claim_id
                      AND remaining_evidence.status='active'
                  )
                ORDER BY linked.claim_id
                """,
                owner,
                evidence_ids,
            )
        unsupported_claim_ids = tuple(
            uuid.UUID(str(row["claim_id"])) for row in unsupported_claim_rows
        )

        try:
            await asyncio.to_thread(
                _delete_and_verify_qdrant,
                qdrant,
                owner,
                thread,
                unsupported_claim_ids,
            )
        except ThreadDeletionV1Error:
            raise
        except Exception as exc:
            raise ThreadDeletionV1Error(
                "qdrant_cleanup_failed",
                retryable=True,
            ) from exc

        retrieval_traces = _delete_count(
            await conn.execute(
                """
                DELETE FROM memory.retrieval_trace
                WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread,
            )
        )
        answer_bindings = _delete_count(
            await conn.execute(
                """
                DELETE FROM memory.final_answer_memory_binding_v1
                WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread,
            )
        )
        attestations = _delete_count(
            await conn.execute(
                """
                DELETE FROM memory.assistant_transcript_attestation_v1
                WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread,
            )
        )
        telemetry_events = _delete_count(
            await conn.execute(
                """
                DELETE FROM public.telemetry_event
                WHERE actor_user_id=$1 AND thread_id=$2
                """,
                str(owner),
                str(thread),
            )
        )
        transcript_rows = _delete_count(
            await conn.execute(
                """
                DELETE FROM public.chat_log
                WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread,
            )
        )
        threads = _delete_count(
            await conn.execute(
                """
                DELETE FROM public.threads
                WHERE owner_user_id=$1 AND id=$2
                """,
                owner,
                thread,
            )
        )
        if threads != 1:
            raise ThreadDeletionV1Error(
                "thread_delete_not_verified",
                retryable=True,
            )

    return ThreadDeletionResultV1(
        contract_version=THREAD_DELETION_CONTRACT_VERSION,
        owner_user_id=owner,
        thread_id=thread,
        status="completed",
        qdrant_verified=True,
        retained_audit_tombstones=bool(evidence_ids),
        counts=ThreadDeletionCountsV1(
            governed_evidence_tombstoned=tombstoned,
            unsupported_claim_projections_verified_absent=len(
                unsupported_claim_ids
            ),
            retrieval_traces_deleted=retrieval_traces,
            answer_bindings_deleted=answer_bindings,
            attestations_deleted=attestations,
            telemetry_events_deleted=telemetry_events,
            transcript_rows_deleted=transcript_rows,
            threads_deleted=threads,
        ),
    )
