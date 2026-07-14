#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import socket
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from rag_engine.memory_v1_store import (
    _queue_claim_projection,
    _write_revision,
    actor_uuid,
    apply_candidate,
    canonical_claim_key,
    normalize_proposal,
)


WORKER_VERSION = "memory_v1_governance_20260714_v1"


class GovernanceBlocked(RuntimeError):
    def __init__(self, reason: str, details: Optional[dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--enqueue-salience", action="store_true")
    parser.add_argument("--process", action="store_true")
    return parser.parse_args()


def _owners(args: argparse.Namespace) -> list[uuid.UUID]:
    raw = list(args.owner_user_id)
    raw.extend(
        value.strip()
        for value in os.getenv("MEMORY_V1_GOVERNANCE_OWNER_IDS", "").split(",")
        if value.strip()
    )
    owners = sorted({actor_uuid(value) for value in raw}, key=str)
    if not owners:
        raise RuntimeError("no governance owner allowlist is configured")
    return owners


async def _set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


async def _queue_summary(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, int]:
    async with conn.transaction(readonly=True):
        await _set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT status::text, count(*)::integer AS count
            FROM memory.governance_job
            WHERE owner_user_id=$1
            GROUP BY status
            ORDER BY status
            """,
            owner,
        )
    return {row["status"]: int(row["count"]) for row in rows}


async def _claim_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> Optional[dict[str, Any]]:
    lease_token = uuid.uuid4()
    async with conn.transaction():
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            WITH selected AS (
              SELECT job_id, status
              FROM memory.governance_job
              WHERE owner_user_id=$1
                AND attempts < $5
                AND available_at <= clock_timestamp()
                AND (
                  status IN ('pending', 'error')
                  OR (status='processing' AND lease_expires_at < clock_timestamp())
                )
              ORDER BY priority, created_at, job_id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE memory.governance_job AS job
            SET status='processing', attempts=job.attempts + 1,
                lease_token=$2,
                lease_expires_at=clock_timestamp() + make_interval(secs => $3),
                worker_id=$4, last_error=NULL
            FROM selected
            WHERE job.owner_user_id=$1 AND job.job_id=selected.job_id
            RETURNING job.job_id, job.lane, job.action, job.target_id,
                      job.target_hash, job.attempts, job.lease_token,
                      selected.status::text AS prior_status
            """,
            owner,
            lease_token,
            lease_seconds,
            worker_id,
            max_attempts,
        )
        if row is None:
            return None
        await conn.execute(
            """
            INSERT INTO memory.governance_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,'claimed',$3::memory.governance_job_status,
                     'processing','worker',$4,$5::jsonb)
            """,
            owner,
            row["job_id"],
            row["prior_status"],
            worker_id,
            _stable_json({"attempt": int(row["attempts"]), "worker_version": WORKER_VERSION}),
        )
        return dict(row)


async def _finish_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    status: str,
    result: dict[str, Any],
    error: Optional[str] = None,
) -> None:
    async with conn.transaction():
        await _set_actor(conn, owner)
        updated = await conn.fetchval(
            """
            UPDATE memory.governance_job
            SET status=$4::memory.governance_job_status,
                lease_token=NULL, lease_expires_at=NULL, worker_id=$5,
                result=$6::jsonb, last_error=$7,
                available_at=CASE WHEN $4='error'
                  THEN clock_timestamp() + make_interval(secs => LEAST(3600, attempts * attempts * 30))
                  ELSE available_at END
            WHERE owner_user_id=$1 AND job_id=$2 AND lease_token=$3
              AND status='processing'
            RETURNING job_id
            """,
            owner,
            job["job_id"],
            job["lease_token"],
            status,
            worker_id,
            _stable_json(result),
            error[:2000] if error else None,
        )
        if updated is None:
            raise RuntimeError("governance lease was lost before finish")
        await conn.execute(
            """
            INSERT INTO memory.governance_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,$3,'processing',$4::memory.governance_job_status,
                     'worker',$5,$6::jsonb)
            """,
            owner,
            job["job_id"],
            "blocked" if status == "blocked" else "finished",
            status,
            worker_id,
            _stable_json(result),
        )


async def _consolidate_claim(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    job: dict[str, Any],
) -> dict[str, Any]:
    async with conn.transaction():
        await _set_actor(conn, owner)
        candidate = await conn.fetchrow(
            """
            SELECT candidate_id, status::text, proposal, proposal_hash,
                   applied_claim_id
            FROM memory.candidate
            WHERE owner_user_id=$1 AND candidate_id=$2
            FOR UPDATE
            """,
            owner,
            job["target_id"],
        )
        if candidate is None or candidate["proposal_hash"] != job["target_hash"]:
            raise GovernanceBlocked("candidate_missing_or_hash_changed")
        if candidate["status"] == "applied":
            return {
                "outcome": "already_applied",
                "claim_id": str(candidate["applied_claim_id"]),
                "truth_changed_by_salience": False,
            }
        if candidate["status"] != "approved":
            raise GovernanceBlocked(
                "candidate_not_approved", {"status": candidate["status"]}
            )

        proposal = normalize_proposal(_json_object(candidate["proposal"]))
        key = canonical_claim_key(proposal)
        predicate = await conn.fetchrow(
            """
            SELECT object_kind, cardinality
            FROM memory.predicate
            WHERE predicate=$1 AND active=true
            """,
            proposal["predicate"],
        )
        if predicate is None:
            raise GovernanceBlocked(
                "unregistered_predicate", {"predicate": proposal["predicate"]}
            )

        exact_claim_id = await conn.fetchval(
            """
            SELECT claim_id FROM memory.claim
            WHERE owner_user_id=$1 AND canonical_key=$2
            FOR UPDATE
            """,
            owner,
            key,
        )
        conflicts: list[uuid.UUID] = []
        if exact_claim_id is None and predicate["cardinality"] == "one":
            subject_id = await conn.fetchval(
                """
                SELECT entity_id FROM memory.entity
                WHERE owner_user_id=$1 AND entity_key=$2
                """,
                owner,
                proposal["subject"]["entity_key"],
            )
            if subject_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT claim_id FROM memory.claim
                    WHERE owner_user_id=$1 AND subject_entity_id=$2
                      AND predicate=$3 AND canonical_key<>$4
                      AND status IN ('supported','uncertain','disputed')
                    ORDER BY claim_id
                    FOR UPDATE
                    """,
                    owner,
                    subject_id,
                    proposal["predicate"],
                    key,
                )
                conflicts = [uuid.UUID(str(row["claim_id"])) for row in rows]
        supersedes = proposal.get("supersedes_claim_id")
        if conflicts and (supersedes is None or uuid.UUID(supersedes) not in conflicts):
            raise GovernanceBlocked(
                "contradiction_requires_review",
                {
                    "predicate": proposal["predicate"],
                    "conflicting_claim_ids": [str(value) for value in conflicts],
                },
            )

        applied = await apply_candidate(
            conn,
            owner,
            candidate_id=candidate["candidate_id"],
            expected_proposal_hash=candidate["proposal_hash"],
            actor_type="job",
            actor_ref=WORKER_VERSION,
        )
        salience_job_id = await conn.fetchval(
            "SELECT memory.enqueue_claim_salience_governance($1,$2,$3)",
            uuid.UUID(applied["claim_id"]),
            candidate["proposal_hash"],
            WORKER_VERSION,
        )
        return {
            "outcome": "deduplicated" if exact_claim_id else "promoted",
            **applied,
            "salience_job_id": str(salience_job_id) if salience_job_id else None,
            "truth_changed_by_salience": False,
        }


async def _consolidate_preference(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    job: dict[str, Any],
) -> dict[str, Any]:
    async with conn.transaction():
        await _set_actor(conn, owner)
        existing = await conn.fetchrow(
            """
            SELECT event_id, preference_id, resulting_revision_id
            FROM memory.preference_apply_event
            WHERE owner_user_id=$1 AND accepted_review_id=$2
            """,
            owner,
            job["target_id"],
        )
        if existing:
            return {"outcome": "already_applied", **{k: str(v) for k, v in dict(existing).items()}}
        row = await conn.fetchrow(
            """
            SELECT review.review_id, review.expected_candidate_hash,
                   candidate.preference_key, head.current_revision_id
            FROM memory.preference_candidate_review AS review
            JOIN memory.preference_candidate AS candidate
              ON candidate.owner_user_id=review.owner_user_id
             AND candidate.candidate_id=review.candidate_id
             AND candidate.candidate_hash=review.expected_candidate_hash
            LEFT JOIN memory.user_preference AS head
              ON head.owner_user_id=review.owner_user_id
             AND head.preference_key=candidate.preference_key
            WHERE review.owner_user_id=$1 AND review.review_id=$2
              AND review.decision='accept'
            """,
            owner,
            job["target_id"],
        )
        if row is None or row["expected_candidate_hash"] != job["target_hash"]:
            raise GovernanceBlocked("accepted_preference_review_missing_or_changed")
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{WORKER_VERSION}:{job['job_id']}")
        event = await conn.fetchrow(
            """
            SELECT * FROM memory.apply_preference_candidate($1,$2,$3,$4::jsonb)
            """,
            row["review_id"],
            request_id,
            row["current_revision_id"],
            _stable_json({"governance_job_id": str(job["job_id"]), "worker": WORKER_VERSION}),
        )
        return {"outcome": "promoted", "apply_event_id": str(event["event_id"])}


async def _consolidate_project(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    job: dict[str, Any],
) -> dict[str, Any]:
    async with conn.transaction():
        await _set_actor(conn, owner)
        existing = await conn.fetchrow(
            """
            SELECT event_id, knowledge_id, resulting_revision_id
            FROM memory.project_knowledge_apply_event
            WHERE owner_user_id=$1 AND accepted_review_id=$2
            """,
            owner,
            job["target_id"],
        )
        if existing:
            return {"outcome": "already_applied", **{k: str(v) for k, v in dict(existing).items()}}
        row = await conn.fetchrow(
            """
            SELECT review.review_id, review.expected_candidate_hash,
                   candidate.project_id, candidate.knowledge_key,
                   current.revision_id AS current_revision_id
            FROM memory.project_knowledge_candidate_review AS review
            JOIN memory.project_knowledge_candidate AS candidate
              ON candidate.owner_user_id=review.owner_user_id
             AND candidate.project_id=review.project_id
             AND candidate.candidate_id=review.candidate_id
             AND candidate.candidate_hash=review.expected_candidate_hash
            LEFT JOIN memory.project_knowledge_head AS head
              ON head.owner_user_id=candidate.owner_user_id
             AND head.project_id=candidate.project_id
             AND head.knowledge_key=candidate.knowledge_key
            LEFT JOIN LATERAL (
              SELECT revision_id
              FROM memory.project_knowledge_revision AS revision
              WHERE revision.owner_user_id=head.owner_user_id
                AND revision.project_id=head.project_id
                AND revision.knowledge_id=head.knowledge_id
              ORDER BY revision.revision_number DESC
              LIMIT 1
            ) AS current ON true
            WHERE review.owner_user_id=$1 AND review.review_id=$2
              AND review.decision='accept'
            """,
            owner,
            job["target_id"],
        )
        if row is None or row["expected_candidate_hash"] != job["target_hash"]:
            raise GovernanceBlocked("accepted_project_review_missing_or_changed")
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{WORKER_VERSION}:{job['job_id']}")
        event = await conn.fetchrow(
            """
            SELECT * FROM memory.apply_project_candidate($1,$2,$3,$4::jsonb)
            """,
            row["review_id"],
            request_id,
            row["current_revision_id"],
            _stable_json({"governance_job_id": str(job["job_id"]), "worker": WORKER_VERSION}),
        )
        return {"outcome": "promoted", "apply_event_id": str(event["event_id"])}


def _half_life_days(predicate: str) -> float:
    if predicate in {"has_name", "spouse_name", "has_children_names", "did_activity_in_past", "spent_time_in"}:
        return 3650.0
    if predicate == "goal":
        return 365.0
    return 730.0


async def _recalculate_salience(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    job: dict[str, Any],
) -> dict[str, Any]:
    async with conn.transaction():
        await _set_actor(conn, owner)
        claim = await conn.fetchrow(
            """
            SELECT claim_id, predicate, status::text, importance, salience
            FROM memory.claim
            WHERE owner_user_id=$1 AND claim_id=$2
            FOR UPDATE
            """,
            owner,
            job["target_id"],
        )
        if claim is None:
            raise GovernanceBlocked("claim_missing")
        if claim["status"] not in {"supported", "uncertain", "disputed"}:
            raise GovernanceBlocked("claim_not_retrievable", {"status": claim["status"]})
        evidence = await conn.fetchrow(
            """
            SELECT
              count(*) FILTER (
                WHERE link.stance='supports' AND evidence.status='active'
              )::integer AS support_count,
              count(DISTINCT COALESCE(evidence.independence_key, evidence.evidence_id::text))
                FILTER (WHERE link.stance='supports' AND evidence.status='active')::integer
                AS independent_support_count,
              max(COALESCE(evidence.observed_at, evidence.recorded_at))
                FILTER (WHERE link.stance='supports' AND evidence.status='active')
                AS last_support_at
            FROM memory.claim_evidence AS link
            JOIN memory.evidence AS evidence
              ON evidence.owner_user_id=link.owner_user_id
             AND evidence.evidence_id=link.evidence_id
            WHERE link.owner_user_id=$1 AND link.claim_id=$2
            """,
            owner,
            claim["claim_id"],
        )
        independent = int(evidence["independent_support_count"] or 0)
        support_count = int(evidence["support_count"] or 0)
        last_support = evidence["last_support_at"]
        if last_support is None:
            recency = 0.0
        else:
            now = datetime.now(timezone.utc)
            age_days = max(0.0, (now - last_support).total_seconds() / 86400.0)
            recency = math.exp(-math.log(2.0) * age_days / _half_life_days(claim["predicate"]))
        confirmation = min(1.0, math.log1p(independent) / math.log(4.0)) if independent else 0.0
        importance = float(claim["importance"])
        target = round(max(0.15, min(1.0, 0.50 * importance + 0.30 * confirmation + 0.20 * recency)), 3)
        prior = float(claim["salience"])
        changed = abs(target - prior) >= 0.01
        revision_number = None
        if changed:
            await conn.execute(
                """
                UPDATE memory.claim
                SET salience=$3, updated_at=clock_timestamp()
                WHERE owner_user_id=$1 AND claim_id=$2
                """,
                owner,
                claim["claim_id"],
                Decimal(f"{target:.3f}"),
            )
            revision_number = await _write_revision(
                conn,
                owner,
                claim["claim_id"],
                reason="salience_recalculated:v1",
                actor_type="job",
                actor_ref=WORKER_VERSION,
            )
            await _queue_claim_projection(conn, owner, claim["claim_id"], revision_number)
        return {
            "outcome": "updated" if changed else "no_change",
            "claim_id": str(claim["claim_id"]),
            "prior_salience": prior,
            "target_salience": target,
            "support_count": support_count,
            "independent_support_count": independent,
            "revision_number": revision_number,
            "truth_changed_by_salience": False,
            "algorithm": "salience_v1",
        }


async def _process_job(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    job: dict[str, Any],
) -> dict[str, Any]:
    if job["action"] == "recalculate_salience":
        return await _recalculate_salience(conn, owner, job)
    if job["lane"] == "claim":
        return await _consolidate_claim(conn, owner, job)
    if job["lane"] == "preference":
        return await _consolidate_preference(conn, owner, job)
    if job["lane"] == "project":
        return await _consolidate_project(conn, owner, job)
    raise GovernanceBlocked("unsupported_lane_or_action")


async def _enqueue_salience(
    conn: asyncpg.Connection, owner: uuid.UUID, bucket: Any
) -> int:
    async with conn.transaction():
        await _set_actor(conn, owner)
        return int(
            await conn.fetchval(
                "SELECT memory.enqueue_salience_governance($1::date,$2)",
                bucket,
                WORKER_VERSION,
            )
            or 0
        )


async def _main() -> None:
    args = _args()
    owners = _owners(args)
    if args.batch_size < 1 or args.batch_size > 500:
        raise RuntimeError("batch-size must be between 1 and 500")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{WORKER_VERSION}"
    conn = await asyncpg.connect(dsn)
    output: dict[str, Any] = {
        "worker_version": WORKER_VERSION,
        "mode": "process" if args.process else "dry_run",
        "owners": {},
    }
    try:
        for owner in owners:
            owner_result: dict[str, Any] = {
                "queue_before": await _queue_summary(conn, owner),
                "salience_enqueued": 0,
                "processed": 0,
                "completed": 0,
                "blocked": 0,
                "errors": 0,
            }
            if args.process and args.enqueue_salience:
                owner_result["salience_enqueued"] = await _enqueue_salience(
                    conn, owner, datetime.now(timezone.utc).date()
                )
            if args.process:
                for _ in range(args.batch_size):
                    job = await _claim_job(
                        conn,
                        owner=owner,
                        worker_id=worker_id,
                        lease_seconds=args.lease_seconds,
                        max_attempts=args.max_attempts,
                    )
                    if job is None:
                        break
                    owner_result["processed"] += 1
                    try:
                        result = await _process_job(conn, owner, job)
                    except GovernanceBlocked as exc:
                        result = {"reason": exc.reason, **exc.details, "worker_version": WORKER_VERSION}
                        await _finish_job(
                            conn,
                            owner=owner,
                            job=job,
                            worker_id=worker_id,
                            status="blocked",
                            result=result,
                        )
                        owner_result["blocked"] += 1
                    except Exception as exc:
                        result = {"error_type": type(exc).__name__, "worker_version": WORKER_VERSION}
                        await _finish_job(
                            conn,
                            owner=owner,
                            job=job,
                            worker_id=worker_id,
                            status="error",
                            result=result,
                            error=str(exc),
                        )
                        owner_result["errors"] += 1
                    else:
                        result["worker_version"] = WORKER_VERSION
                        await _finish_job(
                            conn,
                            owner=owner,
                            job=job,
                            worker_id=worker_id,
                            status="completed",
                            result=result,
                        )
                        owner_result["completed"] += 1
            owner_result["queue_after"] = await _queue_summary(conn, owner)
            output["owners"][str(owner)] = owner_result
    finally:
        await conn.close()
    print(_stable_json(output))


if __name__ == "__main__":
    asyncio.run(_main())
