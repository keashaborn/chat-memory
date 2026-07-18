#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import asyncpg


WORKER_VERSION = "memory_v1_v5_local_inference_scheduler_v1"
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
    "write_counts",
}


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
    parser.add_argument("--run-id")
    parser.add_argument("--canary", default=str(DEFAULT_CANARY))
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--max-jobs", type=int, default=1)
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
    if args.max_jobs != 1:
        raise RuntimeError("initial local scheduler requires max-jobs=1")
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


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    *,
    max_attempts: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT status::text AS status,count(*)::integer AS count
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1 AND route='relational_extraction'
            GROUP BY status ORDER BY status
            """,
            owner,
        )
        target_row = await conn.fetchrow(
            """
            SELECT job_id,evidence_id,evidence_content_sha256,selector_version,
                   status::text AS status,attempts
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1
              AND route='relational_extraction'
              AND status IN ('pending','error')
              AND attempts<$2
              AND available_at<=clock_timestamp()
            ORDER BY priority,available_at,created_at,job_id
            LIMIT 1
            """,
            owner,
            max_attempts,
        )
    target = dict(target_row) if target_row else None
    report = {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "route": "relational_extraction",
        "status_counts": {str(row["status"]): int(row["count"]) for row in rows},
        "next_job_id_sha256": (
            sha256_text(str(target["job_id"])) if target is not None else None
        ),
    }
    return report, target


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
    return {key: payload[key] for key in sorted(SAFE_CANARY_FIELDS) if key in payload}


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


async def run() -> int:
    args = arguments()
    run_id = validate_arguments(args)
    owners = canonical_owners(args.owner_user_id)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("local scheduler requires brains_app session")
        planned = [
            (owner, *(await plan_owner(conn, owner, max_attempts=args.max_attempts)))
            for owner in owners
        ]
    finally:
        await conn.close()

    plans = [report for _, report, _ in planned]
    selected = next(
        ((owner, target) for owner, _, target in planned if target is not None),
        None,
    )
    if not args.apply:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "owner_count": len(owners),
                    "max_jobs": 1,
                    "plans": plans,
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
    if selected is None:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": "no_work",
                    "owner_count": len(owners),
                    "external_model_calls": 0,
                    "local_model_calls": 0,
                }
            )
        )
        return 0

    owner, target = selected
    api_key = load_api_key(args.credential_name)
    result = invoke_canary(
        args,
        owner=owner,
        run_id=run_id,
        target=target,
        api_key=api_key,
    )
    print(
        stable_json(
            {
                "worker_version": WORKER_VERSION,
                "apply": True,
                "owner_count": len(owners),
                "processed": 1,
                "result": result,
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
