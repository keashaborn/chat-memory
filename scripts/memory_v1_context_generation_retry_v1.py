#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlparse
import uuid

import asyncpg


WORKER_VERSION = "memory_v1_context_generation_retry_v1"
SELECTOR_VERSION = "20260729_v5_context_generation_retry_v1"
APPLY_TOKEN = "memory_v1_context_generation_retry_apply_v1"
EXPECTED_SOURCE_SELECTOR = "20260729_v4_contextual_resplit"
EXPECTED_REASON = "context_generation_overlap_corrected"
IDENTITY_NAMESPACE = uuid.UUID("fc7299cb-508c-4c37-a348-0d2171e1877c")
SHA256_CHARS = frozenset("0123456789abcdef")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or append exact owner-scoped extraction jobs after the "
            "context-generation overlap correction."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--expected-count", type=int, default=22)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-path", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not (set(value) - SHA256_CHARS)
    )


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("context generation retry DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("context generation retry DSN must be loopback-only")
    return value


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def ids_for(
    owner: uuid.UUID,
    source_job_id: uuid.UUID,
    evidence_content_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{source_job_id}|{evidence_content_sha256}|"
        f"{SELECTOR_VERSION}|{WORKER_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"job|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"terminal|{identity}"),
    )


def canonical_plan(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if (
            row["source_selector_version"] != EXPECTED_SOURCE_SELECTOR
            or row["source_attempts"] != 1
            or row["reason_code"] != EXPECTED_REASON
            or row["next_selector_version"] != SELECTOR_VERSION
            or not is_sha256(row["evidence_content_sha256"])
            or not is_sha256(row["source_result_sha256"])
            or not is_sha256(row["generation_plan_sha256"])
        ):
            raise RuntimeError("context generation retry plan contract changed")
        result.append(
            {
                "source_job_id": str(row["source_job_id"]),
                "evidence_id": str(row["evidence_id"]),
                "evidence_content_sha256": row[
                    "evidence_content_sha256"
                ],
                "source_attempts": row["source_attempts"],
                "source_selector_version": row[
                    "source_selector_version"
                ],
                "source_result_sha256": row["source_result_sha256"],
                "parent_evidence_id": str(row["parent_evidence_id"]),
                "splitter_version": row["splitter_version"],
                "generation_plan_sha256": row[
                    "generation_plan_sha256"
                ],
                "reason_code": row["reason_code"],
                "next_selector_version": row["next_selector_version"],
            }
        )
    return result


async def read_plan(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
) -> list[dict[str, Any]]:
    async with conn.transaction(readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(owner),
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM memory.plan_owner_context_generation_retry_v1(NULL)
            """
        )
    return [dict(row) for row in rows]


async def invoke(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(owner),
        )
        for row in plan:
            source_job_id = uuid.UUID(row["source_job_id"])
            operation_id, job_id, terminal_id = ids_for(
                owner,
                source_job_id,
                row["evidence_content_sha256"],
            )
            applied = await conn.fetchrow(
                """
                SELECT *
                FROM memory.enqueue_owner_context_generation_retry_v1(
                  $1,$2,$3,$4,$5,$6
                )
                """,
                operation_id,
                source_job_id,
                job_id,
                terminal_id,
                row["evidence_content_sha256"],
                SELECTOR_VERSION,
            )
            if applied is None:
                raise RuntimeError(
                    "context generation retry apply returned no row"
                )
            results.append(dict(applied))
    return results


async def run() -> int:
    args = arguments()
    try:
        owner = uuid.UUID(args.owner_user_id)
    except ValueError as exc:
        raise RuntimeError("owner_user_id must be a UUID") from exc
    if not 1 <= args.expected_count <= 100:
        raise RuntimeError("expected_count must be between 1 and 100")
    if args.expected_plan_sha256 is not None and not is_sha256(
        args.expected_plan_sha256
    ):
        raise RuntimeError("expected plan hash is invalid")
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires expected plan hash")
    if args.apply and os.getenv(
        "MEMORY_V1_CONTEXT_GENERATION_RETRY_APPLY"
    ) != APPLY_TOKEN:
        raise RuntimeError("context generation retry capability is absent")

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(
        loopback_dsn(dsn),
        command_timeout=60,
        ssl=False,
    )
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError(
                "context generation retry requires brains_app session"
            )
        raw_plan = await read_plan(conn, owner)
        plan = canonical_plan(raw_plan)
        if len(plan) != args.expected_count:
            raise RuntimeError(
                "context generation retry candidate count changed"
            )
        plan_sha256 = sha256_text(stable_json(plan))
        if (
            args.expected_plan_sha256 is not None
            and plan_sha256 != args.expected_plan_sha256
        ):
            raise RuntimeError("context generation retry plan hash changed")

        report: dict[str, Any] = {
            "contract_version": WORKER_VERSION,
            "apply": args.apply,
            "owner_user_id_sha256": sha256_text(str(owner)),
            "candidate_count": len(plan),
            "candidate_id_sha256s": [
                sha256_text(row["source_job_id"]) for row in plan
            ],
            "plan_sha256": plan_sha256,
            "source_selector_version_sha256": sha256_text(
                EXPECTED_SOURCE_SELECTOR
            ),
            "next_selector_version_sha256": sha256_text(
                SELECTOR_VERSION
            ),
            "reason_code": EXPECTED_REASON,
            "external_model_calls": 0,
            "local_model_calls": 0,
            "packet_writes": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
        }
        if not args.apply:
            report.update(
                {
                    "outcome": "eligible",
                    "write_counts": {
                        "jobs": 0,
                        "terminals": 0,
                        "events": 0,
                    },
                    "zero_write_replay_proved": True,
                }
            )
        else:
            applied = await invoke(conn, owner=owner, plan=plan)
            replayed = await invoke(conn, owner=owner, plan=plan)
            if any(
                row["apply_outcome"] not in {"applied", "replayed"}
                for row in applied
            ):
                raise RuntimeError(
                    "context generation retry apply outcome is invalid"
                )
            if any(
                row["apply_outcome"] != "replayed" for row in replayed
            ):
                raise RuntimeError(
                    "context generation retry replay wrote again"
                )
            applied_count = sum(
                row["apply_outcome"] == "applied" for row in applied
            )
            report.update(
                {
                    "outcome": "queued",
                    "write_counts": {
                        "jobs": applied_count,
                        "terminals": applied_count,
                        "events": applied_count,
                    },
                    "zero_write_replay_proved": True,
                }
            )

        report_sha256 = secure_write(Path(args.report_path), report)
        print(
            stable_json(
                {
                    "outcome": report["outcome"],
                    "candidate_count": report["candidate_count"],
                    "plan_sha256": plan_sha256,
                    "report_sha256": report_sha256,
                    "write_counts": report["write_counts"],
                    "zero_write_replay_proved": True,
                }
            )
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            stable_json(
                {
                    "contract_version": WORKER_VERSION,
                    "outcome": "rejected",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "local_model_calls": 0,
                    "packet_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            ),
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
