#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any, Sequence

import asyncpg

from scripts.memory_v1_authenticated_owners import (
    resolve_authenticated_owners,
)
from scripts.memory_v1_v5_local_inference_scheduler import loopback_dsn


WORKER_VERSION = "memory_v1_legacy_context_rebind_v1"
SELECTOR_VERSION = "20260728_v5_2_legacy_context_rebind_v1"
APPLY_TOKEN = "memory_v1_legacy_context_rebind_apply_v1"
OPERATION_NAMESPACE = uuid.UUID("df2981f5-104a-443a-bfc3-2d39f7e5fd59")
SHA256_RE = r"^[0-9a-f]{64}$"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or append exact same-owner full-turn context rebindings. "
            "Reports contain only hashes and counts."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 100:
        raise RuntimeError("legacy context rebind limit must be 1..100")
    if args.expected_plan_sha256 is not None and (
        len(args.expected_plan_sha256) != 64
        or any(char not in "0123456789abcdef" for char in args.expected_plan_sha256)
    ):
        raise RuntimeError("expected plan hash is invalid")
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires an expected plan hash")
    if args.apply and os.getenv(
        "MEMORY_V1_LEGACY_CONTEXT_REBIND_APPLY"
    ) != APPLY_TOKEN:
        raise RuntimeError("legacy context rebind apply capability is absent")


async def set_actor(
    connection: asyncpg.Connection,
    owner: uuid.UUID,
) -> None:
    await connection.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner),
    )


async def plan_owner(
    connection: asyncpg.Connection,
    owner: uuid.UUID,
    limit: int,
) -> list[dict[str, Any]]:
    async with connection.transaction(readonly=True):
        await set_actor(connection, owner)
        rows = await connection.fetch(
            """
            SELECT
              job.job_id AS source_job_id,
              job.evidence_id AS source_evidence_id,
              job.evidence_content_sha256,
              job.created_at AS job_created_at,
              evidence.kind::text AS evidence_kind,
              evidence.content,
              evidence.observed_at,
              evidence.directness,
              evidence.source_reliability,
              evidence.sensitivity::text AS sensitivity,
              evidence.metadata,
              source.id AS raw_source_id,
              source.thread_id,
              source.request_id,
              source.created_at AS source_created_at
            FROM memory.evidence_extraction_job AS job
            JOIN memory.evidence AS evidence
              ON evidence.owner_user_id=job.owner_user_id
             AND evidence.evidence_id=job.evidence_id
            JOIN public.chat_log AS source
              ON source.owner_user_id=job.owner_user_id
             AND evidence.external_id IN (
               source.id::text,
               'chat_log:' || source.id::text
             )
            WHERE job.owner_user_id=$1
              AND job.selector_version='20260717_v2'
              AND job.route='relational_extraction'
              AND job.status IN ('pending','error')
              AND job.attempts<2
              AND job.available_at<=clock_timestamp()
              AND job.lease_token IS NULL
              AND job.lease_expires_at IS NULL
              AND evidence.status='active'
              AND evidence.source_system='public.chat_log'
              AND evidence.content IS NOT NULL
              AND evidence.content<>''
              AND evidence.content=source.text
              AND evidence.content_sha256=job.evidence_content_sha256
              AND encode(
                public.digest(convert_to(source.text,'UTF8'),'sha256'),
                'hex'
              )=job.evidence_content_sha256
              AND source.thread_id IS NOT NULL
              AND source.request_id IS NOT NULL
              AND NOT (
                evidence.metadata ? 'source_id'
                AND evidence.metadata ? 'source_content_sha256'
                AND evidence.metadata ? 'source_char_start'
                AND evidence.metadata ? 'source_char_end'
              )
              AND NOT EXISTS (
                SELECT 1
                FROM memory.evidence AS rebound
                WHERE rebound.owner_user_id=job.owner_user_id
                  AND rebound.source_system='public.chat_log'
                  AND rebound.external_id=
                    'context_rebind_v1:' || evidence.evidence_id::text
              )
            ORDER BY job.created_at,job.job_id
            LIMIT $2
            """,
            owner,
            limit,
        )
    return [dict(row) for row in rows]


def plan_contract(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_user_id": str(row["owner_user_id"]),
        "source_job_id": str(row["source_job_id"]),
        "source_evidence_id": str(row["source_evidence_id"]),
        "evidence_content_sha256": row["evidence_content_sha256"],
        "raw_source_id": str(row["raw_source_id"]),
        "thread_id": str(row["thread_id"]),
        "request_id": str(row["request_id"]),
        "selector_version": SELECTOR_VERSION,
    }


async def build_plan(
    connection: asyncpg.Connection,
    owners: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for owner in owners:
        for row in await plan_owner(connection, owner, limit):
            row["owner_user_id"] = owner
            candidates.append(row)
    candidates.sort(
        key=lambda row: (
            row["job_created_at"],
            str(row["owner_user_id"]),
            str(row["source_job_id"]),
        )
    )
    return candidates[:limit]


def contextual_metadata(row: dict[str, Any]) -> dict[str, Any]:
    prior = row["metadata"]
    if isinstance(prior, str):
        prior = json.loads(prior)
    if not isinstance(prior, dict):
        prior = {}
    return {
        **prior,
        "capture_version": WORKER_VERSION,
        "source_type": "brains_chat_user",
        "source_external_id": str(row["raw_source_id"]),
        "source_id": str(row["raw_source_id"]),
        "source_content_sha256": row["evidence_content_sha256"],
        "thread_id": str(row["thread_id"]),
        "request_id": str(row["request_id"]),
        "source_char_start": 0,
        "source_char_end": len(row["content"]),
        "primary_lane": "unclassified_user_statement",
        "epistemic_role": "user_report_unclassified",
        "span_origin": "legacy_full_turn_rebind_v1",
        "rebind_from_evidence_id": str(row["source_evidence_id"]),
        "rebind_from_job_id": str(row["source_job_id"]),
        "semantic_processing": "pending",
    }


async def apply_record(
    connection: asyncpg.Connection,
    row: dict[str, Any],
) -> dict[str, Any]:
    owner = row["owner_user_id"]
    await set_actor(connection, owner)
    external_id = f"context_rebind_v1:{row['source_evidence_id']}"
    evidence = await connection.fetchrow(
        """
        SELECT * FROM memory.record_owner_evidence_v1(
          $1::memory.evidence_kind,
          'public.chat_log',
          $2,
          $3,
          $4,
          $5,
          $6,
          $7,
          $8::memory.sensitivity_level,
          $9::jsonb
        )
        """,
        row["evidence_kind"],
        external_id,
        row["content"],
        row["observed_at"] or row["source_created_at"],
        row["directness"],
        row["source_reliability"],
        f"public.chat_log:source:{row['raw_source_id']}",
        row["sensitivity"],
        stable_json(contextual_metadata(row)),
    )
    if evidence is None or evidence["outcome"] not in {"applied", "replayed"}:
        raise RuntimeError("context rebind evidence write failed")
    if evidence["content_sha256"] != row["evidence_content_sha256"]:
        raise RuntimeError("context rebind evidence hash changed")

    queued = await connection.fetchrow(
        """
        SELECT * FROM memory.enqueue_owner_evidence_extraction_v1(
          $1,$2,$3,'relational_extraction','eligible_unprocessed'
        )
        """,
        evidence["evidence_id"],
        SELECTOR_VERSION,
        row["evidence_content_sha256"],
    )
    if queued is None or queued["apply_outcome"] not in {"applied", "replayed"}:
        raise RuntimeError("context rebind queue write failed")

    operation_id = uuid.uuid5(
        OPERATION_NAMESPACE,
        f"{owner}|{row['source_job_id']}|{SELECTOR_VERSION}",
    )
    values = (
        operation_id,
        row["source_job_id"],
        evidence["evidence_id"],
        queued["job_id"],
        row["raw_source_id"],
        row["evidence_content_sha256"],
        SELECTOR_VERSION,
    )
    applied = await connection.fetchrow(
        """
        SELECT * FROM memory.finalize_owner_legacy_context_rebind_v1(
          $1,$2,$3,$4,$5,$6,$7
        )
        """,
        *values,
    )
    replayed = await connection.fetchrow(
        """
        SELECT * FROM memory.finalize_owner_legacy_context_rebind_v1(
          $1,$2,$3,$4,$5,$6,$7
        )
        """,
        *values,
    )
    if (
        applied is None
        or applied["apply_outcome"] not in {"applied", "replayed"}
        or replayed is None
        or replayed["apply_outcome"] != "replayed"
    ):
        raise RuntimeError("context rebind finalizer replay failed")
    return {
        "evidence_outcome": evidence["outcome"],
        "queue_outcome": queued["apply_outcome"],
        "finalize_outcome": applied["apply_outcome"],
    }


async def run() -> int:
    args = arguments()
    validate_arguments(args)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(
        dsn,
        args.owner_user_id,
    )
    connection = await asyncpg.connect(
        loopback_dsn(dsn),
        command_timeout=120,
        ssl=False,
    )
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("legacy context rebind requires brains_app")
        plan = await build_plan(connection, owners, args.limit)
        contracts = [plan_contract(row) for row in plan]
        plan_sha256 = sha256_text(stable_json(contracts))
        if (
            args.expected_plan_sha256 is not None
            and args.expected_plan_sha256 != plan_sha256
        ):
            raise RuntimeError("legacy context rebind plan changed")
        report: dict[str, Any] = {
            "worker_version": WORKER_VERSION,
            "apply": args.apply,
            "owner_count": len(owners),
            "candidate_count": len(plan),
            "plan_sha256": plan_sha256,
            "selector_version_sha256": sha256_text(SELECTOR_VERSION),
            "external_model_calls": 0,
            "local_model_calls": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
        }
        if not args.apply:
            report.update(
                {
                    "outcome": "planned",
                    "write_counts": {
                        "evidence": 0,
                        "jobs": 0,
                        "events": 0,
                        "terminals": 0,
                        "rebind_links": 0,
                    },
                    "zero_write_replay_proved": True,
                }
            )
            print(stable_json(report))
            return 0

        outcomes: list[dict[str, Any]] = []
        async with connection.transaction():
            for row in plan:
                outcomes.append(await apply_record(connection, row))
        if len(outcomes) != len(plan):
            raise RuntimeError("legacy context rebind write count changed")
        applied_count = sum(
            item["finalize_outcome"] == "applied"
            for item in outcomes
        )
        report.update(
            {
                "outcome": "applied",
                "write_counts": {
                    "evidence": applied_count,
                    "jobs": applied_count,
                    "events": applied_count * 2,
                    "terminals": applied_count,
                    "rebind_links": applied_count,
                },
                "zero_write_replay_proved": True,
            }
        )
        print(stable_json(report))
        return 0
    finally:
        await connection.close()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
