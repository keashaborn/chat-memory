#!/usr/bin/env python3
"""Assess a bounded, owner-fair batch of V5 observations on private inference."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LocalProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_local_entailment_provider import (
    LlamaCppSecureTransport,
    POLICY_VERSION,
    PROVIDER_VERSION,
    assess,
    governed_decision,
)
from scripts.memory_v1_v5_local_inference_canary import (
    PINNED_MODEL_ALIAS,
    PINNED_MODEL_FILE_SHA256,
    PINNED_RUNTIME_REVISION,
)
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_local_entailment_scheduler_v2"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_entailment_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("9f4e9b20-b877-55d3-af04-5bd63baf36ba")


class LocalEntailmentError(RuntimeError):
    pass


class LocalEntailmentBatchError(LocalEntailmentError):
    def __init__(self, completed: int, error_code: str, error_sha256: str):
        super().__init__(f"bounded entailment stopped after {completed} records")
        self.completed = completed
        self.error_code = error_code
        self.error_sha256 = error_sha256


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded private V5 observation assessment batch."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument("--model", default=PINNED_MODEL_ALIAS)
    parser.add_argument("--model-file-sha256", default=PINNED_MODEL_FILE_SHA256)
    parser.add_argument("--runtime-revision", default=PINNED_RUNTIME_REVISION)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--max-records", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise LocalEntailmentError("timeout must be between 1 and 600 seconds")
    if not 64 <= args.max_output_tokens <= 512:
        raise LocalEntailmentError("output token limit must be between 64 and 512")
    if not 1 <= args.max_records <= 20:
        raise LocalEntailmentError("record limit must be between 1 and 20")
    if args.model != PINNED_MODEL_ALIAS:
        raise LocalEntailmentError("local entailment model alias is not pinned")
    if args.model_file_sha256 != PINNED_MODEL_FILE_SHA256:
        raise LocalEntailmentError("local entailment model file hash is not pinned")
    if args.runtime_revision != PINNED_RUNTIME_REVISION:
        raise LocalEntailmentError("local entailment runtime is not pinned")
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_ENTAILMENT_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise LocalEntailmentError("local entailment apply capability is absent")


def load_api_key(credential_name: str) -> str:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        raise LocalEntailmentError("systemd credential directory is unavailable")
    if not credential_name or "/" in credential_name or "\\" in credential_name:
        raise LocalEntailmentError("credential name is invalid")
    key = (Path(directory) / credential_name).read_text(encoding="utf-8").strip()
    if not 32 <= len(key) <= 500 or any(character.isspace() for character in key):
        raise LocalEntailmentError("local inference credential is invalid")
    return key


def operation_ids(
    owner: uuid.UUID,
    stage_admission_id: uuid.UUID,
    observation_id: uuid.UUID,
    observation_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{stage_admission_id}|{observation_id}|"
        f"{observation_sha256}|{POLICY_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"assessment|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"request|{identity}"),
    )


def json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_entailment_v1($1)",
            limit,
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise LocalEntailmentError("read-only entailment planning assigned a txid")
    if len(rows) > limit:
        raise LocalEntailmentError("entailment planner exceeded its row budget")
    return [dict(row) for row in rows]


def bounded_targets(
    plans: list[tuple[uuid.UUID, list[dict[str, Any]]]],
    max_records: int,
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    """Select records round-robin so one owner cannot monopolize a cycle."""
    selected: list[tuple[uuid.UUID, dict[str, Any]]] = []
    seen: set[tuple[uuid.UUID, str]] = set()
    depth = max((len(rows) for _, rows in plans), default=0)
    for index in range(depth):
        for owner, rows in plans:
            if index >= len(rows):
                continue
            row = rows[index]
            key = (owner, str(row["observation_id"]))
            if key in seen:
                raise LocalEntailmentError("entailment planner returned a duplicate")
            seen.add(key)
            selected.append((owner, row))
            if len(selected) == max_records:
                return selected
    return selected


def single_local_call_delta(before: int, after: int) -> int:
    """Convert the shared transport's cumulative counter into a per-record count."""
    if before < 0 or after < before:
        raise LocalEntailmentError("local model call counter regressed")
    delta = after - before
    if delta != 1:
        raise LocalEntailmentError(
            "entailment assessment must make exactly one local model call"
        )
    return delta


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "observation_id_sha256": (
            sha256_text(str(row["observation_id"])) if row else None
        ),
        "route": "private_entailment_assessment" if row else "no_work",
    }


async def persist_assessment(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    assessment: Any,
    model: str,
    model_file_sha256: str,
    runtime_revision: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage_admission_id = uuid.UUID(str(target["stage_admission_id"]))
    observation_id = uuid.UUID(str(target["observation_id"]))
    assessment_id, request_id = operation_ids(
        owner,
        stage_admission_id,
        observation_id,
        str(target["observation_sha256"]),
    )
    decision, reason_code = governed_decision(assessment)
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{owner}|{observation_id}|local_entailment_v1",
        )
        first = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_entailment_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
              $17::jsonb,$18
            )
            """,
            assessment_id,
            request_id,
            stage_admission_id,
            observation_id,
            target["observation_sha256"],
            target["evidence_content_sha256"],
            canonical_sha256(model),
            model_file_sha256,
            canonical_sha256(runtime_revision),
            assessment.request_sha256,
            assessment.response_sha256,
            assessment.output_schema_sha256,
            assessment.decision,
            assessment.confidence,
            decision,
            reason_code,
            json.dumps(json_value(target["source_spans"])),
            POLICY_VERSION,
        )
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        replay = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_entailment_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
              $17::jsonb,$18
            )
            """,
            assessment_id,
            request_id,
            stage_admission_id,
            observation_id,
            target["observation_sha256"],
            target["evidence_content_sha256"],
            canonical_sha256(model),
            model_file_sha256,
            canonical_sha256(runtime_revision),
            assessment.request_sha256,
            assessment.response_sha256,
            assessment.output_schema_sha256,
            assessment.decision,
            assessment.confidence,
            decision,
            reason_code,
            json.dumps(json_value(target["source_spans"])),
            POLICY_VERSION,
        )
    if first is None or replay is None:
        raise LocalEntailmentError("entailment persistence returned no row")
    return dict(first), dict(replay)


async def run() -> int:
    args = arguments()
    validate_arguments(args)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalEntailmentError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalEntailmentError("local entailment requires brains_app")
        plans = [
            (owner, await plan_owner(conn, owner, args.max_records))
            for owner in owners
        ]
        selected = bounded_targets(plans, args.max_records)
        safe_plans = [
            sanitized_plan(owner, rows[0] if rows else None)
            | {"planned_records": len(rows)}
            for owner, rows in plans
        ]
        if not args.apply:
            print(stable_json({
                "worker_version": WORKER_VERSION,
                "apply": False,
                "plans": safe_plans,
                "max_records": args.max_records,
                "selected_records": len(selected),
                "database_writes": 0,
                "local_model_calls": 0,
                "external_model_calls": 0,
                "qdrant_writes": 0,
                "claim_writes": 0,
                "projection_writes": 0,
                "prompt_influence": 0,
                "filesystem_writes": 0,
            }))
            return 0
        if not selected:
            print(stable_json({
                "worker_version": WORKER_VERSION,
                "apply": True,
                "outcome": "no_work",
                "plans": safe_plans,
                "max_records": args.max_records,
                "processed_records": 0,
                "database_rows_created": 0,
                "local_model_calls": 0,
                "external_model_calls": 0,
                "qdrant_writes": 0,
                "claim_writes": 0,
                "projection_writes": 0,
                "prompt_influence": 0,
                "filesystem_writes": 0,
                "zero_write_replay_proved": True,
            }))
            return 0

        transport = LlamaCppSecureTransport(
            endpoint=args.endpoint,
            enable_token=LOCAL_CALL_ENABLE_TOKEN,
            api_key=load_api_key(args.credential_name),
            allow_loopback_http=True,
        )
        processed = 0
        database_rows_created = 0
        local_model_calls = 0
        decision_counts: dict[str, int] = {}
        for owner, target in selected:
            try:
                calls_before = int(transport.local_model_calls)
                assessment = assess(
                    transport,
                    model=args.model,
                    evidence_content=str(target["evidence_content"]),
                    observation_payload=json_value(target["observation_payload"]),
                    timeout_seconds=args.timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                )
                calls_for_record = single_local_call_delta(
                    calls_before,
                    int(transport.local_model_calls),
                )
                first, replay = await persist_assessment(
                    conn,
                    owner=owner,
                    target=target,
                    assessment=assessment,
                    model=args.model,
                    model_file_sha256=args.model_file_sha256,
                    runtime_revision=args.runtime_revision,
                )
                if (
                    first["outcome"] != "applied"
                    or replay["outcome"] != "replayed"
                    or int(replay["rows_written"]) != 0
                    or first["decision"] != replay["decision"]
                ):
                    raise LocalEntailmentError(
                        "entailment apply/replay invariant failed"
                    )
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, LocalProviderAdapterError)
                    else type(exc).__name__
                )
                raise LocalEntailmentBatchError(
                    processed,
                    code,
                    sha256_text(str(exc)),
                ) from exc
            processed += 1
            local_model_calls += calls_for_record
            rows_written = int(first["rows_written"])
            database_rows_created += rows_written
            decision = str(first["decision"])
            decision_counts[decision] = decision_counts.get(decision, 0) + 1
        print(stable_json({
            "worker_version": WORKER_VERSION,
            "apply": True,
            "outcome": "observation_entailment_batch_recorded",
            "plans": safe_plans,
            "max_records": args.max_records,
            "processed_records": processed,
            "governed_decision_counts": decision_counts,
            "database_rows_created": database_rows_created,
            "local_model_calls": local_model_calls,
            "external_model_calls": 0,
            "zero_write_replay_proved": True,
            "write_counts": {
                "assessment_and_entailment_audit": database_rows_created,
                "claims": 0,
                "projections": 0,
                "qdrant": 0,
                "prompt_influence": 0,
            },
            "filesystem_writes": 0,
        }))
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        if isinstance(exc, LocalEntailmentBatchError):
            code = exc.error_code
            error_sha256 = exc.error_sha256
            processed = exc.completed
        else:
            code = exc.code if isinstance(exc, LocalProviderAdapterError) else None
            error_sha256 = sha256_text(str(exc))
            processed = 0
        print(stable_json({
            "worker_version": WORKER_VERSION,
            "apply": False,
            "outcome": "local_entailment_error",
            "error_code": code or type(exc).__name__,
            "error_sha256": error_sha256,
            "processed_before_failure": processed,
            "external_model_calls": 0,
            "qdrant_writes": 0,
            "claim_writes": 0,
            "projection_writes": 0,
            "prompt_influence": 0,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
