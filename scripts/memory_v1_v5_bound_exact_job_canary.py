#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
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
)
from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    validate_and_normalize_observable,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    TrustedProjectBinding,
    TrustedProjectComponent,
    load_registry,
    load_schema,
)
from scripts.memory_v1_v5_bounded_extraction_worker import (
    EXPECTED_REGISTRY_SHA256,
    EXPECTED_SCHEMA_SHA256,
    PERSIST_NAMESPACE,
    ProcessingRejected,
    fail_job,
    persist_packet,
    rejection_code,
    set_actor,
    sha256_text,
    stable_json,
    worker_reference,
)


WORKER_VERSION = "memory_v1_v5_bound_exact_job_canary_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_bound_exact_job_canary_apply_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
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


class ExactProcessingRejected(ProcessingRejected):
    def __init__(
        self,
        code: str,
        external_model_calls: int,
        diagnostic: dict[str, Any] | None,
    ) -> None:
        super().__init__(code, external_model_calls)
        self.diagnostic = diagnostic


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute one owner-scoped, binding-pinned V5 extraction "
            "canary. The runner cannot scan the extraction queue."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--expected-content-sha256", required=True)
    parser.add_argument("--binding-event-id", required=True)
    parser.add_argument("--expected-project-key", required=True)
    parser.add_argument("--expected-component-key", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--worker-id")
    parser.add_argument("--expected-attempts", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-output-tokens", type=int, default=16000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable-external-call", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> dict[str, Any]:
    try:
        owner = uuid.UUID(str(args.owner_user_id))
        job_id = uuid.UUID(str(args.job_id))
        binding_event_id = uuid.UUID(str(args.binding_event_id))
        run_id = uuid.UUID(str(args.run_id))
    except ValueError as exc:
        raise RuntimeError("owner, job, binding, and run ids must be UUIDs") from exc
    if not SHA256_RE.fullmatch(str(args.expected_content_sha256)):
        raise RuntimeError("expected content hash must be lowercase SHA-256")
    if not KEY_RE.fullmatch(str(args.expected_project_key)):
        raise RuntimeError("expected project key is invalid")
    if not KEY_RE.fullmatch(str(args.expected_component_key)):
        raise RuntimeError("expected component key is invalid")
    if not 30 <= args.lease_seconds <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if not 0 <= args.expected_attempts <= 3:
        raise RuntimeError("expected-attempts must be between 0 and 3")
    if args.max_attempts != args.expected_attempts + 1:
        raise RuntimeError("max-attempts must equal expected-attempts plus one")
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise RuntimeError("timeout-seconds must be between 1 and 600")
    if not 1000 <= args.max_output_tokens <= 20000:
        raise RuntimeError("max-output-tokens must be between 1000 and 20000")
    if args.enable_external_call and not args.apply:
        raise RuntimeError("external-call enablement requires --apply")
    if args.apply and not args.enable_external_call:
        raise RuntimeError("--apply requires --enable-external-call")
    if args.apply and not args.model:
        raise RuntimeError("--apply requires an explicit model")
    if args.apply and os.getenv("MEMORY_V1_V5_EXACT_JOB_CANARY_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("exact-job canary apply capability is absent")
    if args.apply and os.getenv("MEMORY_V1_V5_EXTERNAL_CALLS") != (
        EXTERNAL_CALL_ENABLE_TOKEN
    ):
        raise RuntimeError("external model call capability is absent")
    return {
        "owner": owner,
        "job_id": job_id,
        "binding_event_id": binding_event_id,
        "run_id": run_id,
    }


async def plan_exact_target(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job_id: uuid.UUID,
    expected_content_sha256: str,
    binding_event_id: uuid.UUID,
    expected_project_key: str,
    expected_component_key: str,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT
              job.job_id,job.status::text AS status,job.attempts,
              job.route,job.evidence_content_sha256,
              binding.binding_event_id,binding.project_id,
              binding.project_key,binding.thread_id
            FROM memory.evidence_extraction_job AS job
            JOIN memory.evidence AS evidence
              ON evidence.owner_user_id=job.owner_user_id
             AND evidence.evidence_id=job.evidence_id
            JOIN memory.current_project_thread_binding_v5 AS binding
              ON binding.owner_user_id=job.owner_user_id
             AND binding.binding_event_id=$4
             AND binding.thread_id::text=evidence.metadata->>'thread_id'
            WHERE job.owner_user_id=$1
              AND job.job_id=$2
              AND job.evidence_content_sha256=$3
              AND evidence.content_sha256=$3
              AND job.route='relational_extraction'
            """,
            owner,
            job_id,
            expected_content_sha256,
            binding_event_id,
        )
        if row is None:
            raise RuntimeError("exact target or reviewed binding is absent")
        components = await conn.fetch(
            """
            SELECT component_key
            FROM memory.read_owner_project_components_v5($1)
            WHERE component_key=$2
            """,
            row["project_id"],
            expected_component_key,
        )
    if str(row["project_key"]) != expected_project_key:
        raise RuntimeError("exact target project key changed")
    if len(components) != 1:
        raise RuntimeError("expected active component is absent")
    return {
        "target_job_sha256": sha256_text(str(row["job_id"])),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "route": str(row["route"]),
        "evidence_content_sha256": str(row["evidence_content_sha256"]),
        "binding_event_sha256": sha256_text(str(row["binding_event_id"])),
        "project_key_sha256": sha256_text(str(row["project_key"])),
        "component_key_sha256": sha256_text(expected_component_key),
    }


async def claim_exact_target(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    job_id: uuid.UUID,
    expected_content_sha256: str,
    binding_event_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.claim_owner_bound_evidence_job_v5(
              $1,$2,$3,$4,'relational_extraction',$5,$6,$7
            )
            """,
            operation_id,
            job_id,
            expected_content_sha256,
            binding_event_id,
            worker_id,
            lease_seconds,
            max_attempts,
        )
    if row is None:
        raise RuntimeError("exact-job claim returned no row")
    return dict(row)


async def read_pinned_context(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    expected_binding_event_id: uuid.UUID,
    expected_project_key: str,
    expected_component_key: str,
) -> tuple[dict[str, Any], TrustedProjectBinding]:
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
        if row is None:
            raise RuntimeError("V5 context function returned no row")
        context = dict(row)
        if context["binding_event_id"] != expected_binding_event_id:
            raise RuntimeError("reviewed binding changed before extraction")
        if str(context["project_key"]) != expected_project_key:
            raise RuntimeError("reviewed project changed before extraction")
        component_rows = await conn.fetch(
            """
            SELECT *
            FROM memory.read_owner_project_components_v5($1)
            """,
            context["project_id"],
        )
    components = tuple(
        TrustedProjectComponent.create(
            component_id=item["component_id"],
            component_key=item["component_key"],
            display_name=item["display_name"],
            parent_component_id=item["parent_component_id"],
            aliases=item["aliases"],
        )
        for item in component_rows
    )
    if sum(item.component_key == expected_component_key for item in components) != 1:
        raise RuntimeError("expected component changed before extraction")
    binding = TrustedProjectBinding.create(
        thread_id=context["thread_id"],
        project_id=context["project_id"],
        project_key=context["project_key"],
        binding_event_id=context["binding_event_id"],
        components=components,
    )
    return context, binding


async def process_exact_target(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    model: str,
    registry: dict[str, Any],
    schema: dict[str, Any],
    expected_binding_event_id: uuid.UUID,
    expected_project_key: str,
    expected_component_key: str,
    timeout_seconds: float,
    max_output_tokens: int,
) -> tuple[dict[str, Any], int]:
    provider: OpenAIResponsesProvider | None = None
    observable = None
    try:
        context, trusted_binding = await read_pinned_context(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            expected_binding_event_id=expected_binding_event_id,
            expected_project_key=expected_project_key,
            expected_component_key=expected_component_key,
        )
        source = TrustedExtractionSource.create(
            job_id=job["job_id"],
            source_system=job["evidence_source_system"],
            source_external_id=job["evidence_external_id"],
            source_sha256=job["evidence_content_sha256"],
            source_recorded_at=job["evidence_recorded_at"],
            content=job["evidence_content"],
        )
        provider = OpenAIResponsesProvider(
            model=model,
            registry=registry,
            transport=OpenAIResponsesTransport(
                enable_token=os.getenv("MEMORY_V1_V5_EXTERNAL_CALLS")
            ),
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        observable = validate_and_normalize_observable(
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
        if not observable.passed:
            if observable.rejection is None:
                raise RuntimeError("observable rejection lost its code")
            raise ExactProcessingRejected(
                str(observable.rejection["code"]),
                observable.external_model_calls,
                observable.audit_record(),
            )
        validated = observable.validated_result
        if validated is None:
            raise RuntimeError("observable validation lost its result")
        if validated.external_model_calls != 1:
            raise RuntimeError("exact-job provider call count changed")
        packet_id = uuid.uuid5(PERSIST_NAMESPACE, f"packet:{job['job_id']}")
        persist_operation_id = uuid.uuid5(
            PERSIST_NAMESPACE,
            f"persist:{job['job_id']}",
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
            binding_event_id=expected_binding_event_id,
        )
    except ExactProcessingRejected:
        raise
    except Exception as exc:
        calls = provider.external_model_calls if provider is not None else 0
        raise ExactProcessingRejected(
            rejection_code(exc),
            calls,
            observable.audit_record() if observable is not None else None,
        ) from exc
    return (
        {
            "job_sha256": sha256_text(str(job["job_id"])),
            "route": "relational_extraction",
            "outcome": str(persisted["apply_outcome"]),
            "status": str(persisted["status"]),
            "provider_output_sha256": validated.provider_output_sha256,
            "validator_packet_sha256": validated.normalized_packet_sha256,
            "packet_storage_sha256": str(persisted["packet_storage_sha256"]),
            "reviewed_binding_pinned": (
                context["binding_event_id"] == expected_binding_event_id
            ),
            "counts": {
                "entity_mentions": len(validated.normalized_packet["entity_mentions"]),
                "observations": len(validated.normalized_packet["observations"]),
                "comparison_hints": len(
                    validated.normalized_packet["comparison_hints"]
                ),
                "deferrals": len(validated.normalized_packet["deferrals"]),
            },
            "external_model_calls": 1,
            "write_counts": {
                "extraction_packets": int(persisted["apply_outcome"] == "applied"),
                "candidates": 0,
                "claims": 0,
                "staging": 0,
                "qdrant": 0,
                "prompt_influence": 0,
            },
        },
        1,
    )


async def main() -> int:
    args = arguments()
    ids = validate_arguments(args)
    worker_id = worker_reference(args.worker_id)
    registry = load_registry(Path(args.registry), EXPECTED_REGISTRY_SHA256)
    schema = load_schema(Path(args.schema), EXPECTED_SCHEMA_SHA256)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=args.lease_seconds)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("exact-job canary requires brains_app session")
        plan = await plan_exact_target(
            conn,
            owner=ids["owner"],
            job_id=ids["job_id"],
            expected_content_sha256=args.expected_content_sha256,
            binding_event_id=ids["binding_event_id"],
            expected_project_key=args.expected_project_key,
            expected_component_key=args.expected_component_key,
        )
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "exact_target": plan,
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
        if (
            plan["status"] != "pending"
            or plan["attempts"] != args.expected_attempts
        ):
            raise RuntimeError("exact canary target state changed")
        claim_operation_id = uuid.uuid5(
            ids["run_id"],
            f"bound-exact-claim:{ids['owner']}:{ids['job_id']}",
        )
        job = await claim_exact_target(
            conn,
            owner=ids["owner"],
            operation_id=claim_operation_id,
            job_id=ids["job_id"],
            expected_content_sha256=args.expected_content_sha256,
            binding_event_id=ids["binding_event_id"],
            worker_id=worker_id,
            lease_seconds=args.lease_seconds,
            max_attempts=args.max_attempts,
        )
        if job["status"] != "processing" or job["apply_outcome"] != "applied":
            raise RuntimeError("exact canary claim did not enter processing")
        try:
            report, calls = await process_exact_target(
                conn,
                owner=ids["owner"],
                job=job,
                worker_id=worker_id,
                model=str(args.model),
                registry=registry,
                schema=schema,
                expected_binding_event_id=ids["binding_event_id"],
                expected_project_key=args.expected_project_key,
                expected_component_key=args.expected_component_key,
                timeout_seconds=args.timeout_seconds,
                max_output_tokens=args.max_output_tokens,
            )
        except ProcessingRejected as exc:
            failure = await fail_job(
                conn,
                owner=ids["owner"],
                operation_id=uuid.uuid5(
                    PERSIST_NAMESPACE,
                    f"failure:{job['job_id']}:{job['attempts']}",
                ),
                job=job,
                worker_id=worker_id,
                code=exc.code,
                max_attempts=args.max_attempts,
            )
            report = {
                "job_sha256": sha256_text(str(job["job_id"])),
                "route": "relational_extraction",
                "outcome": str(failure["apply_outcome"]),
                "status": str(failure["status"]),
                "rejection_code": exc.code,
                "external_model_calls": exc.external_model_calls,
            }
            diagnostic = getattr(exc, "diagnostic", None)
            if diagnostic is not None:
                report["sanitized_diagnostic"] = diagnostic
            calls = exc.external_model_calls
        if calls > 1:
            raise RuntimeError("exact-job canary exceeded one external call")
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "model_sha256": sha256_text(str(args.model)),
                    "processed": 1,
                    "external_model_calls": calls,
                    "result": report,
                }
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
