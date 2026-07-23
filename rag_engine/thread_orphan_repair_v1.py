from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from .thread_deletion_v1 import (
    ThreadDeletionV1Error,
    _canonical_uuid,
    _delete_and_verify_qdrant,
    _delete_count,
    _evidence_request_id,
    memory_source_lock_key_v1,
)


THREAD_ORPHAN_REPAIR_CONTRACT_VERSION = "thread_orphan_repair_v1"


@dataclass(frozen=True)
class OrphanThreadIdentityV1:
    owner_user_id: uuid.UUID
    thread_id: uuid.UUID


@dataclass(frozen=True)
class OrphanThreadRepairCountsV1:
    governed_evidence_tombstoned: int = 0
    unsupported_claim_projections_verified_absent: int = 0
    retrieval_traces_deleted: int = 0
    answer_bindings_deleted: int = 0
    attestations_deleted: int = 0
    telemetry_events_deleted: int = 0

    def plus(self, other: "OrphanThreadRepairCountsV1") -> "OrphanThreadRepairCountsV1":
        return OrphanThreadRepairCountsV1(
            governed_evidence_tombstoned=(
                self.governed_evidence_tombstoned
                + other.governed_evidence_tombstoned
            ),
            unsupported_claim_projections_verified_absent=(
                self.unsupported_claim_projections_verified_absent
                + other.unsupported_claim_projections_verified_absent
            ),
            retrieval_traces_deleted=(
                self.retrieval_traces_deleted + other.retrieval_traces_deleted
            ),
            answer_bindings_deleted=(
                self.answer_bindings_deleted + other.answer_bindings_deleted
            ),
            attestations_deleted=(
                self.attestations_deleted + other.attestations_deleted
            ),
            telemetry_events_deleted=(
                self.telemetry_events_deleted + other.telemetry_events_deleted
            ),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "governed_evidence_tombstoned": self.governed_evidence_tombstoned,
            "unsupported_claim_projections_verified_absent": (
                self.unsupported_claim_projections_verified_absent
            ),
            "retrieval_traces_deleted": self.retrieval_traces_deleted,
            "answer_bindings_deleted": self.answer_bindings_deleted,
            "attestations_deleted": self.attestations_deleted,
            "telemetry_events_deleted": self.telemetry_events_deleted,
        }


async def discover_orphan_thread_identities_v1(
    conn: Any,
) -> tuple[OrphanThreadIdentityV1, ...]:
    rows = await conn.fetch(
        """
        WITH candidate(owner_user_id,thread_id) AS (
          SELECT owner_user_id::text,thread_id::text
          FROM memory.assistant_transcript_attestation_v1
          UNION
          SELECT owner_user_id::text,thread_id::text
          FROM memory.final_answer_memory_binding_v1
          UNION
          SELECT owner_user_id::text,thread_id::text
          FROM memory.retrieval_trace
          WHERE thread_id IS NOT NULL
          UNION
          SELECT actor_user_id,thread_id
          FROM public.telemetry_event
          WHERE actor_user_id IS NOT NULL AND thread_id IS NOT NULL
          UNION
          SELECT owner_user_id::text,metadata->>'thread_id'
          FROM memory.evidence
          WHERE source_system='public.chat_log'
            AND metadata ? 'thread_id'
        )
        SELECT DISTINCT
          candidate.owner_user_id::uuid AS owner_user_id,
          candidate.thread_id::uuid AS thread_id
        FROM candidate
        WHERE candidate.owner_user_id ~*
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND candidate.thread_id ~*
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND NOT EXISTS (
            SELECT 1
            FROM public.threads AS thread
            WHERE thread.owner_user_id::text=candidate.owner_user_id
              AND thread.id::text=candidate.thread_id
          )
        ORDER BY owner_user_id,thread_id
        """
    )
    return tuple(
        OrphanThreadIdentityV1(
            owner_user_id=uuid.UUID(str(row["owner_user_id"])),
            thread_id=uuid.UUID(str(row["thread_id"])),
        )
        for row in rows
    )


async def repair_orphan_thread_v1(
    conn: Any,
    qdrant: Any,
    *,
    owner_user_id: str | uuid.UUID,
    thread_id: str | uuid.UUID,
) -> OrphanThreadRepairCountsV1:
    owner = _canonical_uuid(owner_user_id, "owner_user_id")
    thread = _canonical_uuid(thread_id, "thread_id")

    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)",
            str(owner),
        )
        if await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1
              FROM public.threads
              WHERE owner_user_id=$1 AND id=$2
            )
            """,
            owner,
            thread,
        ):
            raise ThreadDeletionV1Error(
                "orphan_thread_reappeared",
                retryable=False,
            )
        if await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1
              FROM memory.project_thread_binding_event
              WHERE owner_user_id=$1 AND thread_id=$2
            )
            """,
            owner,
            thread,
        ):
            raise ThreadDeletionV1Error(
                "governed_project_thread_erasure_required",
                retryable=False,
            )

        evidence_rows = await conn.fetch(
            """
            SELECT evidence_id,external_id,status
            FROM memory.evidence
            WHERE owner_user_id=$1
              AND source_system='public.chat_log'
              AND metadata->>'thread_id'=$2
            ORDER BY evidence_id
            """,
            owner,
            str(thread),
        )
        source_ids = tuple(
            sorted(
                {
                    (
                        str(row["external_id"])[len("chat_log:") :]
                        if str(row["external_id"]).startswith("chat_log:")
                        else str(row["external_id"])
                    )
                    for row in evidence_rows
                }
            )
        )
        for source_id in source_ids:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                memory_source_lock_key_v1(
                    owner,
                    "public.chat_log",
                    source_id,
                ),
            )
        if source_ids:
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

        active_evidence_ids = tuple(
            uuid.UUID(str(row["evidence_id"]))
            for row in evidence_rows
            if str(row["status"]) == "active"
        )
        for evidence_id in active_evidence_ids:
            lifecycle_row = await conn.fetchrow(
                """
                SELECT outcome,resulting_status
                FROM memory.transition_evidence_lifecycle(
                  $1,$2,'delete_tombstone','user_request',
                  'user',$3,$4::jsonb
                )
                """,
                evidence_id,
                _evidence_request_id(owner, thread, evidence_id),
                THREAD_ORPHAN_REPAIR_CONTRACT_VERSION,
                json.dumps(
                    {
                        "contract_version": THREAD_ORPHAN_REPAIR_CONTRACT_VERSION,
                        "thread_id": str(thread),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            if (
                lifecycle_row is None
                or str(lifecycle_row["resulting_status"]) != "deleted"
            ):
                raise ThreadDeletionV1Error(
                    "governed_evidence_tombstone_failed",
                    retryable=True,
                )

        unsupported_claim_rows = []
        if active_evidence_ids:
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
                      ON remaining_evidence.owner_user_id=
                           remaining_link.owner_user_id
                     AND remaining_evidence.evidence_id=
                           remaining_link.evidence_id
                    WHERE remaining_link.owner_user_id=linked.owner_user_id
                      AND remaining_link.claim_id=linked.claim_id
                      AND remaining_evidence.status='active'
                  )
                ORDER BY linked.claim_id
                """,
                owner,
                active_evidence_ids,
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

        try:
            retrieval_trace_command = await conn.execute(
                """
                DELETE FROM memory.retrieval_trace
                WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23503":
                raise ThreadDeletionV1Error(
                    "governed_retrieval_trace_reference_requires_review",
                    retryable=False,
                ) from exc
            raise
        retrieval_traces = _delete_count(retrieval_trace_command)
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
        if await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1
              FROM memory.evidence
              WHERE owner_user_id=$1
                AND source_system='public.chat_log'
                AND metadata->>'thread_id'=$2
                AND status='active'
            )
            """,
            owner,
            str(thread),
        ):
            raise ThreadDeletionV1Error(
                "active_orphan_evidence_remains",
                retryable=True,
            )

    return OrphanThreadRepairCountsV1(
        governed_evidence_tombstoned=len(active_evidence_ids),
        unsupported_claim_projections_verified_absent=len(
            unsupported_claim_ids
        ),
        retrieval_traces_deleted=retrieval_traces,
        answer_bindings_deleted=answer_bindings,
        attestations_deleted=attestations,
        telemetry_events_deleted=telemetry_events,
    )
