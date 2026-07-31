#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    PROFILE_NAMES,
    load_runtime_profile_v2,
)
from scripts.memory_v1_v5_local_outcome_policy import classify_rejection_code


WORKER_VERSION = "memory_v1_v5_local_inference_scheduler_v2"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_inference_scheduler_apply_v1"
CANARY_APPLY_TOKEN = "memory_v1_v5_local_inference_canary_apply_v1"
CANARY_CONTRACT = "memory_v1_v5_local_inference_canary_v1"
DEFAULT_CANARY = Path(__file__).with_name("memory_v1_v5_local_inference_canary.py")
SAFE_CANARY_FIELDS = {
    "contract_version",
    "apply",
    "outcome",
    "owner_user_id_sha256",
    "job_id_sha256",
    "provider_id",
    "provider_version",
    "provider_output_sha256",
    "validator_packet_sha256",
    "packet_storage_sha256",
    "manual_review_required",
    "counts",
    "rejection_code",
    "job_status",
    "external_model_calls",
    "local_model_calls",
    "zero_write_replay_proved",
    "invocation_zero_write",
    "completion_apply_outcome",
    "predicate_contract_profile",
    "extraction_contract_version",
    "predicate_registry_version",
    "write_counts",
}
SELECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,99}$")
SAFE_AUDIT_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
CONTEXT_READY_PREDICATE_SQL = """
evidence.status='active'
AND evidence.content_sha256=job.evidence_content_sha256
AND evidence.source_system='public.chat_log'
AND evidence.metadata->>'source_id'
    ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
AND evidence.metadata->>'source_content_sha256' ~ '^[0-9a-f]{64}$'
AND evidence.metadata->>'thread_id'
    ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
AND evidence.metadata->>'request_id'
    ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
AND evidence.metadata->>'source_char_start' ~ '^[0-9]+$'
AND evidence.metadata->>'source_char_end' ~ '^[0-9]+$'
AND nullif(evidence.metadata->>'primary_lane','') IS NOT NULL
AND nullif(evidence.metadata->>'epistemic_role','') IS NOT NULL
AND nullif(evidence.metadata->>'span_origin','') IS NOT NULL
AND source.text IS NOT NULL
AND source.thread_id::text=evidence.metadata->>'thread_id'
AND source.request_id::text=evidence.metadata->>'request_id'
AND encode(
      public.digest(convert_to(source.text,'UTF8'),'sha256'),
      'hex'
    )=evidence.metadata->>'source_content_sha256'
AND CASE
      WHEN evidence.metadata->>'source_char_start' ~ '^[0-9]+$'
       AND evidence.metadata->>'source_char_end' ~ '^[0-9]+$'
      THEN (evidence.metadata->>'source_char_start')::bigint>=0
       AND (evidence.metadata->>'source_char_end')::bigint
           >(evidence.metadata->>'source_char_start')::bigint
       AND (evidence.metadata->>'source_char_end')::bigint
           <=char_length(source.text)
      ELSE false
    END
AND substring(
      source.text
      FROM (evidence.metadata->>'source_char_start')::integer+1
      FOR (evidence.metadata->>'source_char_end')::integer
          -(evidence.metadata->>'source_char_start')::integer
    )=evidence.content
AND encode(
      public.digest(convert_to(evidence.content,'UTF8'),'sha256'),
      'hex'
    )=evidence.content_sha256
"""
CONTEXT_READY_CANDIDATES_CTE_SQL = f"""
WITH context_ready_jobs AS (
  SELECT job.*,
         evidence.metadata->>'source_id' AS source_id,
         evidence.metadata->>'source_content_sha256' AS source_content_sha256,
         evidence.metadata->>'source_char_start' AS source_char_start,
         evidence.metadata->>'source_char_end' AS source_char_end,
         EXISTS (
           SELECT 1
           FROM memory.evidence_extraction_job AS sibling
           JOIN memory.evidence AS sibling_evidence
             ON sibling_evidence.owner_user_id=sibling.owner_user_id
            AND sibling_evidence.evidence_id=sibling.evidence_id
           WHERE sibling.owner_user_id=job.owner_user_id
             AND sibling.route=job.route
             AND sibling.job_id<>job.job_id
             AND sibling.status IN ('review_required','completed','skipped')
             AND sibling.created_at>=job.created_at
             AND sibling_evidence.metadata->>'source_id'
                 =evidence.metadata->>'source_id'
             AND sibling_evidence.metadata->>'source_content_sha256'
                 =evidence.metadata->>'source_content_sha256'
             AND sibling_evidence.metadata->>'source_char_start'
                 =evidence.metadata->>'source_char_start'
             AND sibling_evidence.metadata->>'source_char_end'
                 =evidence.metadata->>'source_char_end'
         ) AS has_same_or_newer_terminal_sibling
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=job.owner_user_id
   AND evidence.evidence_id=job.evidence_id
  JOIN public.chat_log AS source
    ON source.owner_user_id=job.owner_user_id
   AND source.id=CASE
         WHEN evidence.metadata->>'source_id'
           ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
         THEN (evidence.metadata->>'source_id')::uuid
         ELSE NULL
       END
  WHERE job.owner_user_id=$1
    AND job.route='relational_extraction'
    AND job.status IN ('pending','error')
    AND job.attempts<$2
    AND ($3::text IS NULL OR job.selector_version=$3)
    AND job.available_at<=clock_timestamp()
    AND {CONTEXT_READY_PREDICATE_SQL}
),
ranked_context_ready_jobs AS (
  SELECT context_ready_jobs.*,
         row_number() OVER (
           PARTITION BY owner_user_id,source_id,source_content_sha256,
                        source_char_start,source_char_end
           ORDER BY created_at DESC,priority,available_at,job_id
         ) AS source_envelope_ordinal
  FROM context_ready_jobs
)
"""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run at most one owner-scoped local V5 extraction job. Output "
            "contains hashes, counts, routing outcomes, and rejection codes only."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--selector-version")
    parser.add_argument(
        "--contract-profile",
        choices=sorted(PROFILE_NAMES),
        default="v5",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--canary", default=str(DEFAULT_CANARY))
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--max-runtime-seconds", type=int, default=600)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--rolling-window-seconds", type=int, default=86400)
    parser.add_argument("--max-reserved-jobs", type=int, default=12)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def canonical_owners(values: Sequence[str]) -> list[uuid.UUID]:
    if not values:
        raise RuntimeError("at least one explicit owner UUID is required")
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if len(owners) > 6:
        raise RuntimeError("owner allowlist exceeds six entries")
    return owners


def validate_arguments(args: argparse.Namespace) -> uuid.UUID:
    try:
        run_id = uuid.UUID(str(args.run_id)) if args.run_id else uuid.uuid4()
    except ValueError as exc:
        raise RuntimeError("run id must be a UUID") from exc
    if not 1 <= args.max_jobs <= 100:
        raise RuntimeError("max-jobs must be between 1 and 100")
    if not 60 <= args.max_runtime_seconds <= 21600:
        raise RuntimeError("max-runtime-seconds must be between 60 and 21600")
    if args.selector_version is not None and not SELECTOR_RE.fullmatch(
        args.selector_version
    ):
        raise RuntimeError("selector-version is invalid")
    if not 1 <= args.max_attempts <= 3:
        raise RuntimeError("max-attempts must be between 1 and 3")
    if not 30 <= args.lease_seconds <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise RuntimeError("timeout-seconds must be between 1 and 600")
    if not 1000 <= args.max_output_tokens <= 20000:
        raise RuntimeError("max-output-tokens must be between 1000 and 20000")
    if not 3600 <= args.rolling_window_seconds <= 604800:
        raise RuntimeError("rolling window must be between 3600 and 604800")
    if not 1 <= args.max_reserved_jobs <= 100:
        raise RuntimeError("max-reserved-jobs must be between 1 and 100")
    if not 1 <= args.failure_threshold <= 10:
        raise RuntimeError("failure-threshold must be between 1 and 10")
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("local scheduler apply capability is absent")
    return run_id


def worker_reference() -> str:
    return f"{WORKER_VERSION}:{socket.gethostname()}"


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("scheduler database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("scheduler database DSN must be loopback-only")
    return value


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    *,
    max_attempts: int,
    selector_version: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT status::text AS status,count(*)::integer AS count
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1 AND route='relational_extraction'
              AND ($2::text IS NULL OR selector_version=$2)
            GROUP BY status ORDER BY status
            """,
            owner,
            selector_version,
        )
        eligible_count = int(
            await conn.fetchval(
                """
                SELECT count(*)
                FROM memory.evidence_extraction_job AS job
                WHERE job.owner_user_id=$1
                  AND job.route='relational_extraction'
                  AND job.status IN ('pending','error')
                  AND job.attempts<$2
                  AND ($3::text IS NULL OR job.selector_version=$3)
                  AND job.available_at<=clock_timestamp()
                """,
                owner,
                max_attempts,
                selector_version,
            )
        )
        context_stats = await conn.fetchrow(
            f"""
            {CONTEXT_READY_CANDIDATES_CTE_SQL}
            SELECT count(*)::integer AS raw_context_ready_count,
                   count(*) FILTER (
                     WHERE source_envelope_ordinal=1
                       AND NOT has_same_or_newer_terminal_sibling
                   )::integer AS context_ready_count,
                   (
                     count(*)-count(*) FILTER (
                       WHERE source_envelope_ordinal=1
                     )
                   )::integer AS context_duplicate_count,
                   count(*) FILTER (
                     WHERE has_same_or_newer_terminal_sibling
                   )::integer AS context_superseded_count
            FROM ranked_context_ready_jobs
            """,
            owner,
            max_attempts,
            selector_version,
        )
        raw_context_ready_count = int(
            context_stats["raw_context_ready_count"]
        )
        context_ready_count = int(context_stats["context_ready_count"])
        context_duplicate_count = int(
            context_stats["context_duplicate_count"]
        )
        context_superseded_count = int(
            context_stats["context_superseded_count"]
        )
        target_row = await conn.fetchrow(
            f"""
            {CONTEXT_READY_CANDIDATES_CTE_SQL}
            SELECT job_id,evidence_id,evidence_content_sha256,
                   selector_version,status::text AS status,attempts
            FROM ranked_context_ready_jobs
            WHERE source_envelope_ordinal=1
              AND NOT has_same_or_newer_terminal_sibling
            ORDER BY priority,available_at,created_at,job_id
            LIMIT 1
            """,
            owner,
            max_attempts,
            selector_version,
        )
        last_service_at = await conn.fetchval(
            """
            SELECT max(updated_at)
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1 AND route='relational_extraction'
              AND ($2::text IS NULL OR selector_version=$2)
              AND status IN ('review_required','completed','skipped','error')
            """,
            owner,
            selector_version,
        )
    target = dict(target_row) if target_row else None
    report = {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "route": "relational_extraction",
        "status_counts": {str(row["status"]): int(row["count"]) for row in rows},
        "next_job_id_sha256": (
            sha256_text(str(target["job_id"])) if target is not None else None
        ),
        "last_service_at": (
            last_service_at.isoformat() if last_service_at is not None else None
        ),
        "selector_version_sha256": (
            sha256_text(selector_version) if selector_version is not None else None
        ),
        "eligible_count": eligible_count,
        "raw_context_ready_count": raw_context_ready_count,
        "context_ready_count": context_ready_count,
        "context_duplicate_count": context_duplicate_count,
        "context_superseded_count": context_superseded_count,
        "context_rebind_required_count": max(
            eligible_count - raw_context_ready_count,
            0,
        ),
    }
    return report, target, last_service_at


def ordered_owner_targets(
    planned: Sequence[tuple[uuid.UUID, dict[str, Any], dict[str, Any] | None, Any]],
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    candidates = [item for item in planned if item[2] is not None]
    ordered = sorted(
        candidates,
        key=lambda item: (
            item[3] is not None,
            item[3] if item[3] is not None else "",
            str(item[0]),
        ),
    )
    return [(item[0], item[2]) for item in ordered]


def select_owner_target(
    planned: Sequence[tuple[uuid.UUID, dict[str, Any], dict[str, Any] | None, Any]],
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    ordered = ordered_owner_targets(planned)
    return ordered[0] if ordered else None


def sanitized_queue_summary(plans: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    count_fields = (
        "eligible_count",
        "raw_context_ready_count",
        "context_ready_count",
        "context_duplicate_count",
        "context_superseded_count",
        "context_rebind_required_count",
    )
    return {
        field: sum(int(plan.get(field, 0)) for plan in plans)
        for field in count_fields
    }


def no_target_outcome(plans: Sequence[Mapping[str, Any]]) -> str:
    queue = sanitized_queue_summary(plans)
    if queue["eligible_count"] <= 0:
        return "idle_no_eligible_work"
    if queue["context_rebind_required_count"] > 0:
        return "blocked_context_rebind"
    if queue["context_superseded_count"] > 0:
        return "blocked_context_superseded"
    if queue["context_duplicate_count"] > 0:
        return "blocked_context_duplicate"
    return "blocked_no_context_ready"


def batch_child_run_id(
    batch_run_id: uuid.UUID,
    *,
    ordinal: int,
    owner: uuid.UUID,
    job_id: uuid.UUID,
) -> uuid.UUID:
    if ordinal < 0:
        raise RuntimeError("batch ordinal must be non-negative")
    return uuid.uuid5(batch_run_id, f"{ordinal}:{owner}:{job_id}")


def sanitized_batch_summary(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    outcomes = Counter(str(item.get("outcome", "unknown")) for item in results)
    rejections = Counter(
        str(item["rejection_code"])
        for item in results
        if item.get("rejection_code") is not None
    )
    policies = [
        classify_rejection_code(
            str(item["rejection_code"])
            if item.get("rejection_code") is not None
            else None
        )
        for item in results
    ]
    outcome_classes = Counter(policy.outcome_class for policy in policies)
    operational_reasons = Counter(
        policy.normalized_reason_code
        for policy in policies
        if policy.normalized_reason_code is not None
    )
    dispositions = Counter(
        policy.disposition
        for policy in policies
        if policy.disposition is not None
    )
    return {
        "processed": len(results),
        "outcome_counts": dict(sorted(outcomes.items())),
        "rejection_code_counts": dict(sorted(rejections.items())),
        "outcome_class_counts": dict(sorted(outcome_classes.items())),
        "operational_reason_counts": dict(sorted(operational_reasons.items())),
        "disposition_counts": dict(sorted(dispositions.items())),
        "local_model_calls": sum(
            int(item.get("local_model_calls", 0)) for item in results
        ),
        "external_model_calls": sum(
            int(item.get("external_model_calls", 0)) for item in results
        ),
        "result_sha256s": [sha256_text(stable_json(item)) for item in results],
    }


def load_api_key(credential_name: str) -> str:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("systemd credential directory is unavailable")
    if not credential_name or "/" in credential_name or "\\" in credential_name:
        raise RuntimeError("credential name is invalid")
    path = Path(directory) / credential_name
    key = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(key) <= 500 or any(character.isspace() for character in key):
        raise RuntimeError("local inference credential is invalid")
    return key


def canary_command(
    args: argparse.Namespace,
    *,
    owner: uuid.UUID,
    run_id: uuid.UUID,
    target: Mapping[str, Any],
) -> list[str]:
    return [
        sys.executable,
        str(Path(args.canary).resolve()),
        "--owner-user-id",
        str(owner),
        "--evidence-id",
        str(target["evidence_id"]),
        "--expected-job-id",
        str(target["job_id"]),
        "--expected-content-sha256",
        str(target["evidence_content_sha256"]),
        "--selector-version",
        str(target["selector_version"]),
        "--contract-profile",
        getattr(args, "contract_profile", "v5"),
        "--run-id",
        str(run_id),
        "--endpoint",
        args.endpoint,
        "--worker-id",
        worker_reference(),
        "--lease-seconds",
        str(args.lease_seconds),
        "--max-attempts",
        str(args.max_attempts),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--max-output-tokens",
        str(args.max_output_tokens),
        "--rolling-window-seconds",
        str(args.rolling_window_seconds),
        "--max-reserved-jobs",
        str(args.max_reserved_jobs),
        "--failure-threshold",
        str(args.failure_threshold),
        "--apply",
    ]


def sanitized_canary_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("local canary returned an invalid output envelope")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError("local canary returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("contract_version") != CANARY_CONTRACT:
        raise RuntimeError("local canary contract changed")
    outcome = payload.get("outcome")
    handled = (
        completed.returncode == 0
        and outcome in {"accepted", "quota_exhausted", "circuit_open"}
    ) or (completed.returncode == 1 and outcome == "rejected")
    if not handled:
        raise RuntimeError("local canary exit/outcome contract changed")
    sanitized = {
        key: payload[key]
        for key in sorted(SAFE_CANARY_FIELDS)
        if key in payload
    }
    audit = payload.get("audit")
    if audit is not None:
        if not isinstance(audit, dict):
            raise RuntimeError("local canary audit is invalid")
        safe_audit: dict[str, Any] = {}
        for key in (
            "error_code",
            "validation_exception_class",
        ):
            value = audit.get(key)
            if value is not None:
                if not isinstance(value, str) or not SAFE_AUDIT_CODE_RE.fullmatch(value):
                    raise RuntimeError("local canary audit code is invalid")
                safe_audit[key] = value
        count = audit.get("validation_error_count")
        if count is not None:
            if not isinstance(count, int) or not 0 <= count <= 32:
                raise RuntimeError("local canary audit count is invalid")
            safe_audit["validation_error_count"] = count
        error_types = audit.get("validation_error_types")
        if error_types is not None:
            if (
                not isinstance(error_types, list)
                or len(error_types) > 32
                or any(
                    not isinstance(value, str)
                    or not SAFE_AUDIT_CODE_RE.fullmatch(value)
                    for value in error_types
                )
            ):
                raise RuntimeError("local canary audit types are invalid")
            safe_audit["validation_error_types"] = error_types
        locations = audit.get("validation_error_locations")
        if locations is not None:
            if not isinstance(locations, list) or len(locations) > 32:
                raise RuntimeError("local canary audit locations are invalid")
            safe_locations: list[list[str | int]] = []
            for location in locations:
                if not isinstance(location, list) or len(location) > 16:
                    raise RuntimeError("local canary audit location is invalid")
                if any(
                    not (
                        isinstance(token, int)
                        or (
                            isinstance(token, str)
                            and SAFE_AUDIT_CODE_RE.fullmatch(token)
                        )
                    )
                    for token in location
                ):
                    raise RuntimeError("local canary audit location token is invalid")
                safe_locations.append(location)
            safe_audit["validation_error_locations"] = safe_locations
        sanitized["audit"] = safe_audit
    return sanitized


def invoke_canary(
    args: argparse.Namespace,
    *,
    owner: uuid.UUID,
    run_id: uuid.UUID,
    target: Mapping[str, Any],
    api_key: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["MEMORY_V1_V5_LOCAL_INFERENCE_APPLY"] = CANARY_APPLY_TOKEN
    environment["MEMORY_V1_LOCAL_INFERENCE_API_KEY"] = api_key
    completed = subprocess.run(
        canary_command(args, owner=owner, run_id=run_id, target=target),
        env=environment,
        capture_output=True,
        text=True,
        timeout=args.timeout_seconds + 90,
        check=False,
    )
    try:
        return sanitized_canary_result(completed)
    except RuntimeError as exc:
        diagnostic = sha256_text(completed.stdout + "\0" + completed.stderr)
        raise RuntimeError(f"local canary failed closed:{diagnostic}") from exc


async def plan_all_owners(
    dsn: str,
    owners: Sequence[uuid.UUID],
    *,
    max_attempts: int,
    selector_version: str | None,
) -> list[tuple[uuid.UUID, dict[str, Any], dict[str, Any] | None, Any]]:
    conn = await asyncpg.connect(
        loopback_dsn(dsn), command_timeout=30, ssl=False
    )
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("local scheduler requires brains_app session")
        return [
            (
                owner,
                *(
                    await plan_owner(
                        conn,
                        owner,
                        max_attempts=max_attempts,
                        selector_version=selector_version,
                    )
                ),
            )
            for owner in owners
        ]
    finally:
        await conn.close()


BatchPlanLoaderV2 = Callable[..., Awaitable[
    list[tuple[uuid.UUID, dict[str, Any], dict[str, Any] | None, Any]]
]]
BatchCanaryInvokerV2 = Callable[..., dict[str, Any]]


async def execute_batch_v2(
    args: argparse.Namespace,
    *,
    batch_run_id: uuid.UUID,
    owners: Sequence[uuid.UUID],
    dsn: str,
    api_key: str,
    plan_loader: BatchPlanLoaderV2 = plan_all_owners,
    canary_invoker: BatchCanaryInvokerV2 = invoke_canary,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    started_at = monotonic()
    blocked: list[dict[str, Any]] = []
    blocked_owners: set[uuid.UUID] = set()
    results: list[dict[str, Any]] = []
    stop_reason = "max_jobs_reached"
    while len(results) < args.max_jobs:
        if monotonic() - started_at >= args.max_runtime_seconds:
            stop_reason = "runtime_limit_reached"
            break
        planned = await plan_loader(
            dsn,
            owners,
            max_attempts=args.max_attempts,
            selector_version=args.selector_version,
        )
        ordered_targets = [
            item for item in ordered_owner_targets(planned)
            if item[0] not in blocked_owners
        ]
        if not ordered_targets:
            if blocked_owners:
                stop_reason = "all_owners_blocked"
            else:
                stop_reason = no_target_outcome(
                    [item[1] for item in planned]
                )
            break
        made_progress = False
        for owner, target in ordered_targets:
            child_run_id = batch_child_run_id(
                batch_run_id,
                ordinal=len(results),
                owner=owner,
                job_id=uuid.UUID(str(target["job_id"])),
            )
            result = canary_invoker(
                args,
                owner=owner,
                run_id=child_run_id,
                target=target,
                api_key=api_key,
            )
            if result.get("outcome") in {"quota_exhausted", "circuit_open"}:
                blocked.append(result)
                blocked_owners.add(owner)
                continue
            results.append(result)
            made_progress = True
            break
        if not made_progress:
            stop_reason = "all_owners_blocked"
            break
    return {
        "outcome": stop_reason,
        "owner_count": len(owners),
        "max_jobs": args.max_jobs,
        "max_runtime_seconds": args.max_runtime_seconds,
        **sanitized_batch_summary(results),
        "blocked_owner_count": len(blocked_owners),
        "blocked": blocked,
    }


async def run() -> int:
    args = arguments()
    run_id = validate_arguments(args)
    root = Path(__file__).resolve().parents[1]
    profile = load_runtime_profile_v2(root, args.contract_profile)
    if args.apply and profile.lifecycle == "offline_review_only":
        raise RuntimeError("review-only predicate profile cannot run scheduler")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    planned = await plan_all_owners(
        dsn,
        owners,
        max_attempts=args.max_attempts,
        selector_version=args.selector_version,
    )

    plans = [report for _, report, _, _ in planned]
    ordered_targets = ordered_owner_targets(planned)
    selected = ordered_targets[0] if ordered_targets else None
    if not args.apply:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "predicate_contract_profile": profile.name,
                    "extraction_contract_version": profile.contract_version,
                    "predicate_registry_version": profile.registry_version,
                    "apply": False,
                    "owner_count": len(owners),
                    "max_jobs": args.max_jobs,
                    "max_runtime_seconds": args.max_runtime_seconds,
                    "plans": plans,
                    "next_owner_user_id_sha256": (
                        sha256_text(str(selected[0])) if selected is not None else None
                    ),
                    "external_model_calls": 0,
                    "local_model_calls": 0,
                    "write_counts": {
                        "queue": 0,
                        "ledger": 0,
                        "packets": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                }
            )
        )
        return 0
    queue_summary = sanitized_queue_summary(plans)
    if selected is None:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "predicate_contract_profile": profile.name,
                    "extraction_contract_version": profile.contract_version,
                    "predicate_registry_version": profile.registry_version,
                    "apply": True,
                    "outcome": no_target_outcome(plans),
                    "owner_count": len(owners),
                    "queue": queue_summary,
                    "external_model_calls": 0,
                    "local_model_calls": 0,
                }
            )
        )
        return 0

    api_key = load_api_key(args.credential_name)
    batch = await execute_batch_v2(
        args,
        batch_run_id=run_id,
        owners=owners,
        dsn=dsn,
        api_key=api_key,
    )
    print(
        stable_json(
            {
                "worker_version": WORKER_VERSION,
                "predicate_contract_profile": profile.name,
                "extraction_contract_version": profile.contract_version,
                "predicate_registry_version": profile.registry_version,
                "apply": True,
                **batch,
            }
        )
    )
    return 0


def main() -> int:
    return asyncio.run(run())


def guarded_main() -> int:
    try:
        return main()
    except Exception as exc:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "scheduler_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "claims": 0,
                    "qdrant": 0,
                    "prompt_influence": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
