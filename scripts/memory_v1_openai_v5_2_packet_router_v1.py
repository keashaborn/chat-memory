#!/usr/bin/env python3
"""Route one immutable OpenAI packet using PostgreSQL-only review authority."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from scripts.memory_v1_openai_review_packet_v1 import build
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)
from scripts.memory_v1_v5_2_local_packet_router import (
    repository_commit_valid,
    rotate_owners,
)


WORKER_VERSION = "memory_v1_openai_v5_2_packet_router_v1"
APPLY_ENABLE_TOKEN = "memory_v1_openai_v5_2_packet_router_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("c57b6e67-9f19-5e91-b3f1-1f878e8a3700")
REVIEW_CONTRACT = "memory_v1_v5_2_openai_packet_review_v1"
BUNDLE_CONTRACT = "memory_v1_v5_2_stage_preflight_v1"
RESOLUTION_STATES = (
    "auto_link_eligible",
    "manual_review_required",
    "deferred",
    "rejected",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route at most one exact owner-scoped OpenAI V5.2 packet."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--packet-id")
    parser.add_argument(
        "--extraction-schema",
        default="specs/memory_v1_relational_extraction_v5_2.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5_2.schema.json",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_artifacts(
    *,
    report: dict[str, Any],
    bundle: dict[str, Any],
    owner: uuid.UUID,
    packet_id: uuid.UUID,
) -> dict[str, Any]:
    report_text = stable_json(report)
    report_sha = sha256_text(report_text)
    bundle["source_report"] = {
        "storage": "memory.v5_2_openai_packet_route_event.review_report",
        "sha256": report_sha,
    }
    bundle_text = stable_json(bundle)
    bundle_sha = sha256_text(bundle_text)
    counts = report.get("resolution_summary")
    provenance = report.get("provider_provenance")
    if (
        report.get("contract_version") != REVIEW_CONTRACT
        or report.get("mode") != "owner_scoped_openai_packet_review_zero_write"
        or report.get("owner_user_id") != str(owner)
        or report.get("packet_id") != str(packet_id)
        or report.get("review_disposition") != "manual_review_required"
        or not repository_commit_valid(report.get("repository_commit"))
        or not isinstance(provenance, dict)
        or provenance.get("provider_id") != "openai_responses"
        or provenance.get("provider_version") != "v1"
        or not isinstance(counts, dict)
        or set(counts) != set(RESOLUTION_STATES)
        or any(not isinstance(counts[state], int) or counts[state] < 0 for state in RESOLUTION_STATES)
        or not isinstance(report.get("blocking_codes"), list)
        or report.get("zero_write_proof", {}).get("database_writes") != 0
        or report.get("zero_write_proof", {}).get("qdrant_writes") != 0
        or report.get("zero_write_proof", {}).get("external_model_calls") != 0
    ):
        raise RuntimeError("OpenAI V5.2 review report contract is invalid")
    if (
        bundle.get("contract_version") != BUNDLE_CONTRACT
        or bundle.get("mode") != "preflight_only_zero_write"
        or bundle.get("owner_user_id") != str(owner)
        or bundle.get("case_id") != f"openai-packet-{packet_id}"
        or bundle.get("database_writes") != 0
        or bundle.get("qdrant_writes") != 0
        or bundle.get("external_model_calls") != 0
        or bundle.get("authorized_stage") is not False
        or bundle.get("source_report", {}).get("sha256") != report_sha
        or bundle.get("extraction_packet_sha256")
        != report.get("derived_packet_sha256")
        or bundle.get("resolution_packet_sha256")
        != report.get("resolution_packet_sha256")
        or bundle.get("resolution_summary") != counts
    ):
        raise RuntimeError("OpenAI V5.2 stage bundle contract is invalid")
    try:
        review_id = uuid.UUID(str(report["review_id"]))
        request_id = uuid.UUID(str(bundle["request_id"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("OpenAI V5.2 artifact identifiers are invalid") from exc
    if len(report_text.encode("utf-8")) > 196_608 or len(bundle_text.encode("utf-8")) > 262_144:
        raise RuntimeError("OpenAI V5.2 PostgreSQL review artifact exceeds its bound")
    return {
        "review": report,
        "bundle": bundle,
        "review_id": review_id,
        "request_id": request_id,
        "report_sha256": report_sha,
        "bundle_sha256": bundle_sha,
        "repository_commit": report["repository_commit"],
        "counts": {state: counts[state] for state in RESOLUTION_STATES},
        "blocking_code_count": len(set(report["blocking_codes"])),
    }


def stable_ids(
    *,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
    routing_basis_sha256: str,
    report_sha256: str,
    bundle_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = "|".join(
        (
            str(owner),
            str(packet_id),
            routing_basis_sha256,
            report_sha256,
            bundle_sha256,
        )
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"event|{identity}"),
    )


async def bind_postgres_artifact_hashes(
    conn: asyncpg.Connection, artifact: dict[str, Any]
) -> dict[str, Any]:
    hash_query = """
        SELECT encode(public.digest(
          convert_to($1::jsonb::text,'UTF8'),'sha256'
        ),'hex')
    """
    report_sha = await conn.fetchval(hash_query, stable_json(artifact["review"]))
    if not isinstance(report_sha, str) or len(report_sha) != 64:
        raise RuntimeError("PostgreSQL review artifact hash is invalid")
    artifact["bundle"]["source_report"]["sha256"] = report_sha
    bundle_sha = await conn.fetchval(hash_query, stable_json(artifact["bundle"]))
    if not isinstance(bundle_sha, str) or len(bundle_sha) != 64:
        raise RuntimeError("PostgreSQL stage bundle hash is invalid")
    return {
        **artifact,
        "report_sha256": report_sha,
        "bundle_sha256": bundle_sha,
    }


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    packet_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        if packet_id is None:
            candidates = await conn.fetch(
                """
                SELECT packet.packet_id
                FROM memory.evidence_extraction_packet_v5 AS packet
                JOIN memory.evidence_extraction_job AS job
                  ON job.owner_user_id=packet.owner_user_id
                 AND job.job_id=packet.job_id
                JOIN memory.evidence AS evidence
                  ON evidence.owner_user_id=packet.owner_user_id
                 AND evidence.evidence_id=packet.evidence_id
                WHERE packet.provider_id='openai_responses'
                  AND packet.provider_version='v1'
                  AND packet.external_model_calls=1
                  AND job.status='review_required'
                  AND job.lease_token IS NULL
                  AND job.last_error IS NULL
                  AND evidence.status='active'
                  AND NOT EXISTS (
                    SELECT 1 FROM memory.v5_2_openai_packet_route_event AS prior
                    WHERE prior.owner_user_id=packet.owner_user_id
                      AND prior.packet_id=packet.packet_id
                  )
                ORDER BY packet.created_at,packet.packet_id
                LIMIT 1
                """
            )
            candidate_ids = [uuid.UUID(str(row["packet_id"])) for row in candidates]
        else:
            candidate_ids = [packet_id]
        planned: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            rows = await conn.fetch(
                "SELECT * FROM memory.plan_owner_v5_2_exact_packet_route_v1($1)",
                candidate_id,
            )
            if len(rows) > 1:
                raise RuntimeError("OpenAI V5.2 route planner exceeded its bound")
            if rows:
                planned.append(dict(rows[0]))
    if not planned:
        return None
    if len(planned) != 1 or planned[0].get("route") != "manual_review_artifact_ready":
        raise RuntimeError("OpenAI V5.2 planner returned an invalid route")
    return planned[0]


async def build_artifacts(
    args: argparse.Namespace,
    *,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
) -> dict[str, Any]:
    build_args = argparse.Namespace(
        owner_user_id=str(owner),
        packet_id=str(packet_id),
        extraction_schema=args.extraction_schema,
        resolution_schema=args.resolution_schema,
    )
    report, bundle = await build(build_args)
    return validate_artifacts(
        report=report,
        bundle=bundle,
        owner=owner,
        packet_id=packet_id,
    )


async def record_review(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    artifact: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    operation_id, event_id = stable_ids(
        owner=owner,
        packet_id=packet_id,
        routing_basis_sha256=target["routing_basis_sha256"],
        report_sha256=artifact["report_sha256"],
        bundle_sha256=artifact["bundle_sha256"],
    )

    async def invoke() -> dict[str, Any]:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            await conn.execute(
                "SELECT set_config('memory.openai_review_report_v1',$1,true)",
                stable_json(artifact["review"]),
            )
            await conn.execute(
                "SELECT set_config('memory.openai_stage_bundle_v1',$1,true)",
                stable_json(artifact["bundle"]),
            )
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.record_owner_v5_2_review_route_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
                )
                """,
                operation_id,
                event_id,
                packet_id,
                target["packet_storage_sha256"],
                target["routing_basis_sha256"],
                artifact["review_id"],
                artifact["request_id"],
                artifact["report_sha256"],
                artifact["bundle_sha256"],
                artifact["repository_commit"],
                artifact["counts"]["auto_link_eligible"],
                artifact["counts"]["manual_review_required"],
                artifact["counts"]["deferred"],
                artifact["counts"]["rejected"],
                artifact["blocking_code_count"],
            )
        if row is None:
            raise RuntimeError("OpenAI V5.2 route recorder returned no row")
        return dict(row)

    return await invoke(), await invoke()


async def run() -> int:
    args = arguments()
    try:
        target_packet_id = uuid.UUID(args.packet_id) if args.packet_id else None
    except ValueError as exc:
        raise RuntimeError("exact OpenAI packet ID is invalid") from exc
    if args.apply and os.getenv("MEMORY_V1_OPENAI_V5_2_PACKET_ROUTER_APPLY") != APPLY_ENABLE_TOKEN:
        raise RuntimeError("OpenAI V5.2 router apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    if not args.owner_user_id:
        owners = rotate_owners(owners, int(time.time() // 60))
    if target_packet_id is not None and len(owners) != 1:
        raise RuntimeError("exact OpenAI packet routing requires one owner")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("OpenAI V5.2 router requires brains_app session")
        plans = [(owner, await plan_owner(conn, owner, target_packet_id)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        sanitized = [
            {
                "owner_user_id_sha256": sha256_text(str(owner)),
                "packet_id_sha256": sha256_text(str(row["packet_id"])) if row else None,
                "route": row["route"] if row else "no_work",
            }
            for owner, row in plans
        ]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "plans": sanitized,
                        "database_writes": 0,
                        "filesystem_writes": 0,
                        "external_model_calls": 0,
                    }
                )
            )
            return 0
        if selected is None:
            outcome = "no_work"
            database_writes = 0
            review_counts = None
        else:
            owner, target = selected
            packet_id = uuid.UUID(str(target["packet_id"]))
            artifact = await build_artifacts(args, owner=owner, packet_id=packet_id)
            artifact = await bind_postgres_artifact_hashes(conn, artifact)
            applied, replayed = await record_review(
                conn, owner=owner, target=target, artifact=artifact
            )
            if (
                applied["apply_outcome"] not in {"applied", "replayed"}
                or replayed["apply_outcome"] != "replayed"
            ):
                raise RuntimeError("OpenAI V5.2 route replay failed")
            outcome = "manual_review_artifact_ready"
            database_writes = int(applied["apply_outcome"] == "applied")
            review_counts = artifact["counts"]
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": outcome,
                    "plans": sanitized,
                    "review_resolution_counts": review_counts,
                    "write_counts": {
                        "route_events": database_writes,
                        "restricted_review_artifacts": database_writes * 2,
                        "filesystem": 0,
                        "stage": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "external_model_calls": 0,
                }
            )
        )
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "router_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "filesystem_writes": 0,
                    "stage_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
