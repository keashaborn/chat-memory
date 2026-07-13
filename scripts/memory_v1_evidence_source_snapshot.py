#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime

import asyncpg

from rag_engine.memory_v1_evidence_apply import _source_snapshot_sha256
from rag_engine.memory_v1_evidence_triage import SOURCE_ROLE, TriageSourceRow
from rag_engine.memory_v1_store import actor_uuid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only SHA-256 lock for an owner-scoped chat_log cohort."
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--last-created-at", required=True)
    parser.add_argument("--last-source-id", required=True)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owner = actor_uuid(args.owner)
    last_created_at = datetime.fromisoformat(
        args.last_created_at.replace("Z", "+00:00")
    )
    last_source_id = uuid.UUID(args.last_source_id)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            rows = await conn.fetch(
                """
                SELECT id, user_id, source, text, created_at,
                       thread_id, vantage_id, request_id
                FROM public.chat_log
                WHERE user_id=$1
                  AND source=$2
                  AND (created_at, id) <= ($3::timestamptz, $4::uuid)
                ORDER BY created_at, id
                """,
                str(owner),
                SOURCE_ROLE,
                last_created_at,
                last_source_id,
            )
    finally:
        await conn.close()
    sources = [
        TriageSourceRow(
            source_id=uuid.UUID(str(row["id"])),
            owner_user_id=actor_uuid(row["user_id"]),
            source=str(row["source"]),
            text=str(row["text"] or ""),
            created_at=row["created_at"],
            thread_id=(
                uuid.UUID(str(row["thread_id"])) if row["thread_id"] else None
            ),
            vantage_id=str(row["vantage_id"]) if row["vantage_id"] else None,
            request_id=str(row["request_id"]) if row["request_id"] else None,
        )
        for row in rows
    ]
    if any(source.owner_user_id != owner for source in sources):
        raise RuntimeError("source query returned a cross-owner row")
    print(
        json.dumps(
            {
                "mode": "read_only_source_snapshot",
                "owner_user_id": str(owner),
                "row_count": len(sources),
                "last_created_at": (
                    sources[-1].created_at.isoformat() if sources else None
                ),
                "last_source_id": str(sources[-1].source_id) if sources else None,
                "sha256": _source_snapshot_sha256(sources),
                "raw_content_emitted": False,
                "database_writes": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
