#!/usr/bin/env python3
"""Assess at most one owner-scoped V5 observation on private inference."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg

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
    canonical_owners,
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_local_entailment_scheduler_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_entailment_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("9f4e9b20-b877-55d3-af04-5bd63baf36ba")


class LocalEntailmentError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one bounded private V5 observation assessment."
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
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise LocalEntailmentError("timeout must be between 1 and 600 seconds")
    if not 64 <= args.max_output_tokens <= 512:
        raise LocalEntailmentError("output token limit must be between 64 and 512")
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
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_entailment_v1(1)"
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise LocalEntailmentError("read-only entailment planning assigned a txid")
    if len(rows) > 1:
        raise LocalEntailmentError("entailment planner exceeded its row budget")
    return dict(rows[0]) if rows else None


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
    owners = canonical_owners(args.owner_user_id)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalEntailmentError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalEntailmentError("local entailment requires brains_app")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        safe_plans = [sanitized_plan(owner, row) for owner, row in plans]
        if not args.apply:
            print(stable_json({
                "worker_version": WORKER_VERSION,
                "apply": False,
                "plans": safe_plans,
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
        if selected is None:
            print(stable_json({
                "worker_version": WORKER_VERSION,
                "apply": True,
                "outcome": "no_work",
                "plans": safe_plans,
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

        owner, target = selected
        transport = LlamaCppSecureTransport(
            endpoint=args.endpoint,
            enable_token=LOCAL_CALL_ENABLE_TOKEN,
            api_key=load_api_key(args.credential_name),
            allow_loopback_http=True,
        )
        assessment = assess(
            transport,
            model=args.model,
            evidence_content=str(target["evidence_content"]),
            observation_payload=json_value(target["observation_payload"]),
            timeout_seconds=args.timeout_seconds,
            max_output_tokens=args.max_output_tokens,
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
            raise LocalEntailmentError("entailment apply/replay invariant failed")
        print(stable_json({
            "worker_version": WORKER_VERSION,
            "apply": True,
            "outcome": "observation_entailment_recorded",
            "plans": safe_plans,
            "governed_decision": first["decision"],
            "database_rows_created": int(first["rows_written"]),
            "local_model_calls": assessment.local_model_calls,
            "external_model_calls": 0,
            "zero_write_replay_proved": True,
            "write_counts": {
                "assessment_and_entailment_audit": int(first["rows_written"]),
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
        code = exc.code if isinstance(exc, LocalProviderAdapterError) else None
        print(stable_json({
            "worker_version": WORKER_VERSION,
            "apply": False,
            "outcome": "local_entailment_error",
            "error_code": code or type(exc).__name__,
            "error_sha256": sha256_text(str(exc)),
            "external_model_calls": 0,
            "qdrant_writes": 0,
            "claim_writes": 0,
            "projection_writes": 0,
            "prompt_influence": 0,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
