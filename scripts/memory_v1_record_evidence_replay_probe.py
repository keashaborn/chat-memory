#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import asyncpg

from rag_engine.memory_v1_store import record_evidence


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    return parser.parse_args()


async def main() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")

        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                str(owner),
            )
            evidence = await conn.fetchrow(
                """
                SELECT evidence_id,kind::text,source_system,external_id,content,
                       observed_at,directness,source_reliability,independence_key,
                       sensitivity::text,metadata
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND status='active'
                  AND content IS NOT NULL
                ORDER BY recorded_at,evidence_id
                LIMIT 1
                """,
                owner,
            )
        if evidence is None:
            raise RuntimeError("owner has no active evidence row with content")

        metadata = evidence["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            raise RuntimeError("selected evidence metadata is not a JSON object")

        replayed_id = await record_evidence(
            conn,
            owner,
            kind=evidence["kind"],
            source_system=evidence["source_system"],
            external_id=evidence["external_id"],
            content=evidence["content"],
            observed_at=evidence["observed_at"],
            directness=evidence["directness"],
            source_reliability=evidence["source_reliability"],
            independence_key=evidence["independence_key"],
            sensitivity=evidence["sensitivity"],
            metadata=metadata,
        )
        if replayed_id != evidence["evidence_id"]:
            raise AssertionError("replay returned a different evidence ID")
    finally:
        await conn.close()

    print(
        json.dumps(
            {
                "contract_version": "memory_v1_record_evidence_replay_probe_v1",
                "owner_user_id": str(owner),
                "same_evidence_id": True,
                "outcome": "replayed",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
