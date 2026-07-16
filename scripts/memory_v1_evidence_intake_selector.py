#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import uuid
from typing import Any

import asyncpg

from rag_engine.memory_v1_store import actor_uuid


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", required=True)
    parser.add_argument("--selector-version", default="20260716_v1")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--record-terminal", action="store_true")
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
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def serialized_row(row: asyncpg.Record) -> dict[str, Any]:
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
    return [serialized_row(row) for row in rows]


async def run_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    limit: int,
    record_terminal: bool,
) -> dict[str, Any]:
    async with conn.transaction():
        before = await plan(conn, owner, selector_version, limit)
        applied = 0
        replayed = 0
        if record_terminal:
            for row in before:
                if row["outcome"] not in {"empty", "skipped"}:
                    continue
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
                    applied += 1
                elif recorded["apply_outcome"] == "replayed":
                    replayed += 1
                else:
                    raise RuntimeError("unexpected terminal apply outcome")
        after = await plan(conn, owner, selector_version, limit)

    terminal_before = sum(
        row["outcome"] in {"empty", "skipped"} for row in before
    )
    if record_terminal and len(after) != len(before) - applied:
        raise RuntimeError("recorded terminal rows were not removed from the plan")
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
            "terminal_rows": terminal_before,
        },
        "record": {
            "enabled": record_terminal,
            "applied": applied,
            "replayed": replayed,
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
    owners = sorted(
        {actor_uuid(value) for value in args.owner_user_id},
        key=str,
    )
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        owner_reports = [
            await run_owner(
                conn,
                owner,
                args.selector_version,
                args.limit,
                args.record_terminal,
            )
            for owner in owners
        ]
    finally:
        await conn.close()

    report = {
        "contract_version": "memory_v1_evidence_intake_selector_report_v1",
        "completed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "selector_version": args.selector_version,
        "record_terminal": args.record_terminal,
        "model_calls": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
        "owners": owner_reports,
    }
    report["report_sha256"] = digest(report)
    report_path = Path(args.report_path).expanduser().resolve()
    write_secure_json(report_path, report)

    totals = Counter()
    for owner_report in owner_reports:
        totals.update(owner_report["before"]["outcomes"])
    print(stable_json({
        "owners": len(owner_reports),
        "outcomes": dict(sorted(totals.items())),
        "record_terminal": args.record_terminal,
        "report": str(report_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
