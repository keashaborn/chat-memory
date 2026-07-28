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

CAPTURE_VERSION = "memory_v1_v5_chat_capture_20260717_v1"
SOURCE_SYSTEM = "public.chat_log"
SOURCE_TYPE = "frontend/chat:user"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy authenticated immutable chat turns into owner-scoped V5 "
            "evidence without model calls or semantic writes."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(stable_json(value))


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
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


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


async def assert_no_source_conflict(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> None:
    conflict = await conn.fetchrow(
        """
        SELECT log.id, evidence.evidence_id, evidence.status::text
          FROM public.chat_log AS log
          JOIN memory.evidence AS evidence
            ON evidence.owner_user_id=log.owner_user_id
           AND evidence.source_system=$2
           AND evidence.external_id IN (
             log.id::text,
             'chat_log:' || log.id::text
           )
         WHERE log.owner_user_id=$1
           AND log.source=$3
           AND (
             evidence.status<>'active'
             OR evidence.content_sha256 IS DISTINCT FROM CASE
               WHEN log.text IS NULL THEN NULL
               ELSE encode(
                 public.digest(convert_to(log.text,'UTF8'),'sha256'),
                 'hex'
               )
             END
           )
         ORDER BY log.created_at,log.id,evidence.evidence_id
         LIMIT 1
        """,
        owner,
        SOURCE_SYSTEM,
        SOURCE_TYPE,
    )
    if conflict:
        raise RuntimeError(
            "chat/evidence source conflict for "
            f"{conflict['id']} ({conflict['status']})"
        )


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    await set_actor(conn, owner)
    await assert_no_source_conflict(conn, owner)
    rows = await conn.fetch(
        """
        SELECT log.id,log.text,log.created_at,log.thread_id,log.request_id
          FROM public.chat_log AS log
         WHERE log.owner_user_id=$1
           AND log.source=$2
           AND NOT EXISTS (
             SELECT 1
               FROM memory.evidence AS evidence
              WHERE evidence.owner_user_id=log.owner_user_id
                AND evidence.source_system=$3
                AND evidence.external_id IN (
                  log.id::text,
                  'chat_log:' || log.id::text
                )
           )
         ORDER BY log.created_at,log.id
         LIMIT $4
        """,
        owner,
        SOURCE_TYPE,
        SOURCE_SYSTEM,
        limit,
    )
    return [
        {
            "source_external_id": str(row["id"]),
            "source_sha256": (
                sha256_text(str(row["text"]))
                if row["text"] is not None
                else None
            ),
            "source_recorded_at": row["created_at"],
            "thread_id": str(row["thread_id"]) if row["thread_id"] else None,
            "request_id": str(row["request_id"]) if row["request_id"] else None,
            "content": row["text"],
        }
        for row in rows
    ]


async def count_missing(conn: asyncpg.Connection, owner: uuid.UUID) -> int:
    await set_actor(conn, owner)
    return int(
        await conn.fetchval(
            """
            SELECT count(*)
              FROM public.chat_log AS log
             WHERE log.owner_user_id=$1
               AND log.source=$2
               AND NOT EXISTS (
                 SELECT 1
                   FROM memory.evidence AS evidence
                  WHERE evidence.owner_user_id=log.owner_user_id
                    AND evidence.source_system=$3
                    AND evidence.external_id IN (
                      log.id::text,
                      'chat_log:' || log.id::text
                    )
               )
            """,
            owner,
            SOURCE_TYPE,
            SOURCE_SYSTEM,
        )
    )


async def record_source(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    source: dict[str, Any],
) -> dict[str, Any]:
    await set_actor(conn, owner)
    source_id = source["source_external_id"]
    independence_key = (
        f"public.chat_log:thread:{source['thread_id']}"
        if source["thread_id"]
        else f"public.chat_log:source:{source_id}"
    )
    result = await conn.fetchrow(
        """
        SELECT evidence_id,outcome,content_sha256
          FROM memory.record_owner_evidence_v1(
            'user_statement'::memory.evidence_kind,
            $1,$2,$3,$4,1,1,$5,
            'high'::memory.sensitivity_level,$6::jsonb
          )
        """,
        SOURCE_SYSTEM,
        source_id,
        source["content"],
        source["source_recorded_at"],
        independence_key,
        stable_json(
            {
                "capture_version": CAPTURE_VERSION,
                "source_type": SOURCE_TYPE,
                "source_external_id": source_id,
                "thread_id": source["thread_id"],
                "request_id": source["request_id"],
                "semantic_processing": "pending",
            }
        ),
    )
    if result is None or result["outcome"] not in {"applied", "replayed"}:
        raise RuntimeError(f"unexpected evidence result for {source_id}")
    if result["content_sha256"] != source["source_sha256"]:
        raise RuntimeError(f"evidence hash mismatch for {source_id}")
    return {
        "source_external_id": source_id,
        "source_sha256": source["source_sha256"],
        "evidence_id": str(result["evidence_id"]),
        "outcome": str(result["outcome"]),
    }


def public_plan(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_external_id": row["source_external_id"],
            "source_sha256": row["source_sha256"],
            "source_recorded_at": row["source_recorded_at"].isoformat().replace(
                "+00:00", "Z"
            ),
            "thread_id": row["thread_id"],
        }
        for row in rows
    ]


async def main() -> int:
    args = arguments()
    if not 1 <= args.limit <= 500:
        raise RuntimeError("--limit must be between 1 and 500")
    if args.apply and os.getenv("MEMORY_V1_V5_CHAT_CAPTURE_APPLY") != "enabled":
        raise RuntimeError(
            "--apply requires MEMORY_V1_V5_CHAT_CAPTURE_APPLY=enabled"
        )
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)

    conn = await asyncpg.connect(dsn, command_timeout=60)
    owner_reports: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("chat capture requires brains_app session")
        async with conn.transaction(
            isolation="repeatable_read", readonly=not args.apply
        ):
            for owner in owners:
                planned = await plan_owner(conn, owner, args.limit)
                rows = (
                    [await record_source(conn, owner, row) for row in planned]
                    if args.apply
                    else []
                )
                remaining = await count_missing(conn, owner)
                if args.apply and remaining > max(0, len(planned) - args.limit):
                    # The selected records must disappear from the missing set.
                    selected_ids = {row["source_external_id"] for row in planned}
                    visible = await plan_owner(conn, owner, args.limit)
                    if selected_ids & {
                        row["source_external_id"] for row in visible
                    }:
                        raise RuntimeError("captured source remained eligible")
                owner_reports.append(
                    {
                        "owner_user_id": str(owner),
                        "planned_count": len(planned),
                        "remaining_count": remaining,
                        "plan_sha256": digest(public_plan(planned)),
                        "outcomes": dict(
                            sorted(Counter(row["outcome"] for row in rows).items())
                        ),
                        "sources": public_plan(planned),
                        "evidence_results": rows,
                    }
                )
    finally:
        await conn.close()

    report = {
        "contract_version": CAPTURE_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "apply": bool(args.apply),
        "owner_count": len(owners),
        "model_calls": 0,
        "qdrant_writes": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
        "project_writes": 0,
        "owners": owner_reports,
    }
    report["report_sha256"] = digest(report)
    output = Path(args.report_path).expanduser().resolve()
    output_sha = secure_write(output, report)
    print(
        stable_json(
            {
                "version": CAPTURE_VERSION,
                "apply": bool(args.apply),
                "owners": len(owners),
                "planned": sum(
                    item["planned_count"] for item in owner_reports
                ),
                "remaining": sum(
                    item["remaining_count"] for item in owner_reports
                ),
                "report": str(output),
                "report_file_sha256": output_sha,
                "model_calls": 0,
                "qdrant_writes": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
