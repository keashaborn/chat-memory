#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners


TERMINAL_OUTCOMES = {"empty", "skipped"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--selector-version", default="20260716_v1")
    parser.add_argument("--limit", type=int, default=100)
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


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def serialize(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "evidence_content_sha256": row["evidence_content_sha256"],
        "outcome": row["outcome"],
        "route": row["route"],
        "reason_code": row["reason_code"],
        "source_job_id": (
            str(row["source_job_id"]) if row["source_job_id"] else None
        ),
        "source_job_status": row["source_job_status"],
        "source_job_pipeline_version": row["source_job_pipeline_version"],
        "upstream_candidate_count": int(row["upstream_candidate_count"]),
    }


async def plan(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    limit: int,
) -> list[dict[str, Any]]:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
    rows = await conn.fetch(
        """
        SELECT *
        FROM memory.plan_owner_evidence_intake_v1($1,$2,NULL)
        """,
        selector_version,
        limit,
    )
    return [serialize(row) for row in rows]


async def dispatch_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    limit: int,
    apply: bool,
) -> dict[str, Any]:
    before = await plan(conn, owner, selector_version, limit)
    terminal_applied = 0
    terminal_replayed = 0
    queue_applied = 0
    queue_replayed = 0

    if apply:
        for row in before:
            if row["outcome"] in TERMINAL_OUTCOMES:
                recorded = await conn.fetchrow(
                    """
                    SELECT *
                    FROM memory.record_owner_evidence_intake_terminal_v1(
                      $1,$2,$3,$4,$5
                    )
                    """,
                    uuid.UUID(row["evidence_id"]),
                    selector_version,
                    row["evidence_content_sha256"],
                    row["outcome"],
                    row["reason_code"],
                )
                if recorded["apply_outcome"] == "applied":
                    terminal_applied += 1
                elif recorded["apply_outcome"] == "replayed":
                    terminal_replayed += 1
                else:
                    raise RuntimeError("unexpected terminal apply outcome")
            elif row["outcome"] == "eligible":
                queued = await conn.fetchrow(
                    """
                    SELECT *
                    FROM memory.enqueue_owner_evidence_extraction_v1(
                      $1,$2,$3,$4,$5
                    )
                    """,
                    uuid.UUID(row["evidence_id"]),
                    selector_version,
                    row["evidence_content_sha256"],
                    row["route"],
                    row["reason_code"],
                )
                if queued["apply_outcome"] == "applied":
                    queue_applied += 1
                elif queued["apply_outcome"] == "replayed":
                    queue_replayed += 1
                else:
                    raise RuntimeError("unexpected queue apply outcome")
            elif row["outcome"] != "deferred":
                raise RuntimeError(f"unexpected plan outcome: {row['outcome']}")

    after = await plan(conn, owner, selector_version, limit)
    removed = terminal_applied + queue_applied
    if apply and len(after) != len(before) - removed:
        raise RuntimeError("dispatch did not remove applied rows from intake plan")

    return {
        "owner_user_id": str(owner),
        "plan_sha256": digest(before),
        "before": {
            "rows": len(before),
            "outcomes": dict(sorted(Counter(
                row["outcome"] for row in before
            ).items())),
            "reasons": dict(sorted(Counter(
                row["reason_code"] for row in before
            ).items())),
        },
        "apply": {
            "enabled": apply,
            "terminal_applied": terminal_applied,
            "terminal_replayed": terminal_replayed,
            "queue_applied": queue_applied,
            "queue_replayed": queue_replayed,
        },
        "after": {
            "rows": len(after),
            "outcomes": dict(sorted(Counter(
                row["outcome"] for row in after
            ).items())),
        },
        "rows": before,
    }


def write_secure_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def main() -> int:
    args = arguments()
    if not 1 <= args.limit <= 500:
        raise ValueError("--limit must be between 1 and 500")
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)

    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        async with conn.transaction():
            owner_reports = [
                await dispatch_owner(
                    conn,
                    owner,
                    args.selector_version,
                    args.limit,
                    args.apply,
                )
                for owner in owners
            ]
    finally:
        await conn.close()

    report = {
        "contract_version": "memory_v1_evidence_intake_dispatch_report_v1",
        "completed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "selector_version": args.selector_version,
        "apply": args.apply,
        "model_calls": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
        "owners": owner_reports,
    }
    report["report_sha256"] = digest(report)
    report_path = Path(args.report_path).expanduser().resolve()
    write_secure_json(report_path, report)

    outcomes: Counter[str] = Counter()
    queued = 0
    terminals = 0
    for owner in owner_reports:
        outcomes.update(owner["before"]["outcomes"])
        queued += owner["apply"]["queue_applied"]
        terminals += owner["apply"]["terminal_applied"]
    print(stable_json({
        "apply": args.apply,
        "outcomes": dict(sorted(outcomes.items())),
        "owners": len(owner_reports),
        "queued": queued,
        "terminal_recorded": terminals,
        "report": str(report_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
