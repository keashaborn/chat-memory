#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
    OpenAIResponsesProvider,
    OpenAIResponsesTransport,
    ProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    TrustedProjectBinding,
    TrustedProjectComponent,
    load_registry,
    load_schema,
    validate_and_normalize,
)


WORKER_VERSION = "memory_v1_v5_bounded_extraction_worker_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_bounded_extraction_apply_v1"
PERSIST_NAMESPACE = uuid.UUID("3fc762b4-6e78-4cbd-b669-36427f647eb2")
EXPECTED_REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
EXPECTED_SCHEMA_SHA256 = (
    "744ce1d466dfe502fb78fd0ba0a996dd723d1593f34bc456c849c20811f99e70"
)
DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[1]
    / "specs"
    / "memory_v1_predicate_registry_v5.json"
)
DEFAULT_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "specs"
    / "memory_v1_relational_extraction_v5.schema.json"
)


class ProcessingRejected(RuntimeError):
    def __init__(self, code: str, external_model_calls: int) -> None:
        super().__init__(code)
        self.code = code
        self.external_model_calls = external_model_calls


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute a bounded, owner-scoped V5 relational "
            "extraction cycle. Reports contain hashes, counts, route "
            "outcomes, and rejection codes only."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--worker-id")
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-output-tokens", type=int, default=16000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable-external-call", action="store_true")
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def project_binding_for_packet(
    normalized_packet: dict[str, Any],
    binding_event_id: uuid.UUID | None,
) -> uuid.UUID | None:
    resolved_project_scope = any(
        observation.get("projection_class") == "project_knowledge"
        and observation.get("project_scope", {}).get("state") == "resolved"
        for observation in normalized_packet.get("observations", [])
    )
    if not resolved_project_scope:
        return None
    if binding_event_id is None:
        raise RuntimeError("resolved project scope lost its trusted binding")
    return binding_event_id


def canonical_owners(values: list[str]) -> list[uuid.UUID]:
    if not values:
        raise RuntimeError("at least one explicit owner UUID is required")
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if len(owners) > 100:
        raise RuntimeError("owner allowlist exceeds 100 entries")
    return owners


def worker_reference(value: str | None) -> str:
    result = value or (
        f"{WORKER_VERSION}:{socket.gethostname()}:{os.getpid()}:"
        f"{uuid.uuid4().hex[:12]}"
    )
    if not result.strip() or len(result) > 500:
        raise RuntimeError("worker id must contain 1 to 500 characters")
    return result


def validate_arguments(args: argparse.Namespace) -> uuid.UUID:
    try:
        run_id = uuid.UUID(str(args.run_id))
    except ValueError as exc:
        raise RuntimeError("run id must be a UUID") from exc
    if not 1 <= args.max_jobs <= 10:
        raise RuntimeError("max-jobs must be between 1 and 10")
    if not 1 <= args.max_attempts <= 3:
        raise RuntimeError("max-attempts must be between 1 and 3")
    if not 30 <= args.lease_seconds <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise RuntimeError("timeout-seconds must be between 1 and 600")
    if not 1000 <= args.max_output_tokens <= 20000:
        raise RuntimeError("max-output-tokens must be between 1000 and 20000")
    if args.enable_external_call and not args.apply:
        raise RuntimeError("external-call enablement requires --apply")
    if args.apply and not args.enable_external_call:
        raise RuntimeError("--apply requires --enable-external-call")
    if args.apply and not args.model:
        raise RuntimeError("--apply requires an explicit --model")
    if args.apply and os.getenv("MEMORY_V1_V5_BOUNDED_EXTRACTION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("bounded extraction apply capability is absent")
    if args.apply and os.getenv("MEMORY_V1_V5_EXTERNAL_CALLS") != (
        EXTERNAL_CALL_ENABLE_TOKEN
    ):
        raise RuntimeError("external model call capability is absent")
    return run_id


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT status::text AS status,count(*)::integer AS count
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1
              AND route='relational_extraction'
            GROUP BY status
            ORDER BY status
            """,
            owner,
        )
        next_job = await conn.fetchval(
            """
            SELECT job_id::text
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1
              AND route='relational_extraction'
              AND available_at<=clock_timestamp()
              AND status IN ('pending','error')
            ORDER BY priority,available_at,created_at,job_id
            LIMIT 1
            """,
            owner,
        )
    return {
        "owner_sha256": sha256_text(str(owner)),
        "route": "relational_extraction",
        "status_counts": {str(row["status"]): row["count"] for row in rows},
        "next_job_sha256": sha256_text(next_job) if next_job else None,
    }


async def claim_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> dict[str, Any] | None:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.claim_owner_evidence_extraction_job_v1(
              $1,'relational_extraction',$2,$3,$4
            )
            """,
            operation_id,
            worker_id,
            lease_seconds,
            max_attempts,
        )
    return dict(row) if row else None


async def read_context(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.read_owner_evidence_extraction_context_v5(
              $1,$2,$3,$4
            )
            """,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
        )
        component_rows = []
        if row is not None and row["project_id"] is not None:
            component_rows = await conn.fetch(
                """
                SELECT *
                FROM memory.read_owner_project_components_v5($1)
                """,
                row["project_id"],
            )
    if row is None:
        raise RuntimeError("V5 context function returned no row")
    context = dict(row)
    context["project_components"] = [dict(item) for item in component_rows]
    return context


async def persist_packet(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    packet_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    model_sha256: str,
    validated: Any,
    binding_event_id: uuid.UUID | None,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.persist_owner_evidence_extraction_packet_v5(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15
            )
            """,
            operation_id,
            packet_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            validated.provider_id,
            validated.provider_version,
            model_sha256,
            validated.provider_output_sha256,
            validated.normalized_packet_sha256,
            stable_json(validated.normalized_packet),
            validated.manual_review_required,
            validated.external_model_calls,
            binding_event_id,
        )
    if row is None:
        raise RuntimeError("V5 persistence function returned no row")
    return dict(row)


def rejection_code(exc: BaseException) -> str:
    if isinstance(exc, ProviderAdapterError):
        return exc.code
    if isinstance(exc, (ValueError, TypeError)):
        return "validator_rejected"
    if isinstance(exc, asyncpg.PostgresError):
        return "database_contract_rejected"
    return "worker_rejected"


async def fail_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    code: str,
    max_attempts: int,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.fail_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            operation_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            code,
            "sanitized bounded extraction rejection",
            max_attempts,
        )
    if row is None:
        raise RuntimeError("V5 failure function returned no row")
    return dict(row)


async def process_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    model: str,
    registry: dict[str, Any],
    schema: dict[str, Any],
    timeout_seconds: float,
    max_output_tokens: int,
) -> tuple[dict[str, Any], int]:
    provider: OpenAIResponsesProvider | None = None
    try:
        context = await read_context(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
        )
        source = TrustedExtractionSource.create(
            job_id=job["job_id"],
            source_system=job["evidence_source_system"],
            source_external_id=job["evidence_external_id"],
            source_sha256=job["evidence_content_sha256"],
            source_recorded_at=job["evidence_recorded_at"],
            content=job["evidence_content"],
        )
        trusted_binding = None
        if context["binding_event_id"] is not None:
            trusted_components = tuple(
                TrustedProjectComponent.create(
                    component_id=item["component_id"],
                    component_key=item["component_key"],
                    display_name=item["display_name"],
                    parent_component_id=item["parent_component_id"],
                    aliases=item["aliases"],
                )
                for item in context["project_components"]
            )
            trusted_binding = TrustedProjectBinding.create(
                thread_id=context["thread_id"],
                project_id=context["project_id"],
                project_key=context["project_key"],
                binding_event_id=context["binding_event_id"],
                components=trusted_components,
            )
        transport = OpenAIResponsesTransport(
            enable_token=os.getenv("MEMORY_V1_V5_EXTERNAL_CALLS")
        )
        provider = OpenAIResponsesProvider(
            model=model,
            registry=registry,
            transport=transport,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        validated = validate_and_normalize(
            provider,
            source=source,
            registry=registry,
            schema=schema,
            trusted_project_binding=trusted_binding,
            allowed_provider_versions={
                OPENAI_PROVIDER_ID: OPENAI_PROVIDER_VERSION,
            },
            max_external_model_calls=1,
        )
        if validated.external_model_calls != 1:
            raise RuntimeError("bounded provider call count changed")

        packet_id = uuid.uuid5(PERSIST_NAMESPACE, f"packet:{job['job_id']}")
        persist_operation_id = uuid.uuid5(
            PERSIST_NAMESPACE,
            f"persist:{job['job_id']}",
        )
        packet_binding_event_id = project_binding_for_packet(
            validated.normalized_packet,
            (
                uuid.UUID(str(context["binding_event_id"]))
                if context["binding_event_id"] is not None
                else None
            ),
        )
        persisted = await persist_packet(
            conn,
            owner=owner,
            operation_id=persist_operation_id,
            packet_id=packet_id,
            job=job,
            worker_id=worker_id,
            model_sha256=sha256_text(model),
            validated=validated,
            binding_event_id=packet_binding_event_id,
        )
    except Exception as exc:
        calls = provider.external_model_calls if provider is not None else 0
        raise ProcessingRejected(rejection_code(exc), calls) from exc
    result = {
        "job_sha256": sha256_text(str(job["job_id"])),
        "route": "relational_extraction",
        "outcome": str(persisted["apply_outcome"]),
        "status": str(persisted["status"]),
        "provider_output_sha256": validated.provider_output_sha256,
        "validator_packet_sha256": validated.normalized_packet_sha256,
        "packet_storage_sha256": str(persisted["packet_storage_sha256"]),
        "project_binding_used": packet_binding_event_id is not None,
        "counts": {
            "entity_mentions": len(validated.normalized_packet["entity_mentions"]),
            "observations": len(validated.normalized_packet["observations"]),
            "comparison_hints": len(
                validated.normalized_packet["comparison_hints"]
            ),
            "deferrals": len(validated.normalized_packet["deferrals"]),
        },
        "external_model_calls": validated.external_model_calls,
        "write_counts": {
            "extraction_packets": int(persisted["apply_outcome"] == "applied"),
            "candidates": 0,
            "claims": 0,
            "staging": 0,
            "qdrant": 0,
            "prompt_influence": 0,
        },
    }
    return result, validated.external_model_calls


async def main() -> int:
    args = arguments()
    run_id = validate_arguments(args)
    owners = canonical_owners(args.owner_user_id)
    worker_id = worker_reference(args.worker_id)
    registry = load_registry(Path(args.registry), EXPECTED_REGISTRY_SHA256)
    schema = load_schema(Path(args.schema), EXPECTED_SCHEMA_SHA256)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=args.lease_seconds)
    reports: list[dict[str, Any]] = []
    total_external_calls = 0
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("bounded extraction requires brains_app session")
        plans = [await plan_owner(conn, owner) for owner in owners]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "route": "relational_extraction",
                        "max_jobs": args.max_jobs,
                        "owner_count": len(owners),
                        "plans": plans,
                        "external_model_calls": 0,
                        "write_counts": {
                            "queue": 0,
                            "packets": 0,
                            "candidates": 0,
                            "claims": 0,
                            "staging": 0,
                            "qdrant": 0,
                            "prompt_influence": 0,
                        },
                    }
                )
            )
            return 0

        remaining = args.max_jobs
        for owner in owners:
            for sequence in range(args.max_jobs):
                if remaining == 0:
                    break
                claim_operation_id = uuid.uuid5(
                    run_id,
                    f"claim:{owner}:{sequence}",
                )
                job = await claim_job(
                    conn,
                    owner=owner,
                    operation_id=claim_operation_id,
                    worker_id=worker_id,
                    lease_seconds=args.lease_seconds,
                    max_attempts=args.max_attempts,
                )
                if job is None:
                    break
                remaining -= 1
                if job["status"] != "processing":
                    reports.append(
                        {
                            "job_sha256": sha256_text(str(job["job_id"])),
                            "route": "relational_extraction",
                            "outcome": "claim_replayed_terminal",
                            "status": str(job["status"]),
                            "external_model_calls": 0,
                        }
                    )
                    continue
                try:
                    report, calls = await process_job(
                        conn,
                        owner=owner,
                        job=job,
                        worker_id=worker_id,
                        model=str(args.model),
                        registry=registry,
                        schema=schema,
                        timeout_seconds=args.timeout_seconds,
                        max_output_tokens=args.max_output_tokens,
                    )
                    total_external_calls += calls
                    reports.append(report)
                except ProcessingRejected as exc:
                    code = exc.code
                    total_external_calls += exc.external_model_calls
                    failure_operation_id = uuid.uuid5(
                        PERSIST_NAMESPACE,
                        f"failure:{job['job_id']}:{job['attempts']}",
                    )
                    failure = await fail_job(
                        conn,
                        owner=owner,
                        operation_id=failure_operation_id,
                        job=job,
                        worker_id=worker_id,
                        code=code,
                        max_attempts=args.max_attempts,
                    )
                    reports.append(
                        {
                            "job_sha256": sha256_text(str(job["job_id"])),
                            "route": "relational_extraction",
                            "outcome": str(failure["apply_outcome"]),
                            "status": str(failure["status"]),
                            "rejection_code": code,
                            "external_model_calls": exc.external_model_calls,
                        }
                    )

        if total_external_calls>args.max_jobs:
            raise RuntimeError("worker exceeded global external call budget")
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "route": "relational_extraction",
                    "model_sha256": sha256_text(str(args.model)),
                    "owner_count": len(owners),
                    "processed": len(reports),
                    "external_model_calls": total_external_calls,
                    "results": reports,
                }
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
