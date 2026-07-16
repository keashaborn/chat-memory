#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import uuid

import asyncpg

from rag_engine.memory_v1_store import EvidenceConflict, record_evidence


OWNER_A = uuid.UUID("77777777-7777-4777-8777-777777777777")
OWNER_B = uuid.UUID("88888888-8888-4888-8888-888888888888")


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("integration DSN must authenticate as brains_app")
        first = await record_evidence(
            conn,
            OWNER_A,
            kind="user_statement",
            source_system="public.chat_log",
            external_id="record-evidence-python-integration",
            content="Owner A controlled writer integration.",
            observed_at=datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc),
            directness=1.0,
            source_reliability=0.9,
            independence_key="record-evidence-python-integration:a",
            sensitivity="low",
            metadata={"test": "controlled_writer"},
        )
        replay = await record_evidence(
            conn,
            OWNER_A,
            kind="user_statement",
            source_system="public.chat_log",
            external_id="record-evidence-python-integration",
            content="Owner A controlled writer integration.",
            observed_at=datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc),
            directness=1.0,
            source_reliability=0.9,
            independence_key="record-evidence-python-integration:a",
            sensitivity="low",
            metadata={"test": "ignored_on_replay"},
        )
        if replay != first:
            raise AssertionError("controlled writer replay returned another evidence ID")
        try:
            await record_evidence(
                conn,
                OWNER_A,
                kind="user_statement",
                source_system="public.chat_log",
                external_id="record-evidence-python-integration",
                content="Changed content must fail.",
                sensitivity="low",
            )
        except EvidenceConflict:
            pass
        else:
            raise AssertionError("controlled writer accepted changed replay content")
        second_owner = await record_evidence(
            conn,
            OWNER_B,
            kind="user_statement",
            source_system="public.chat_log",
            external_id="record-evidence-python-integration",
            content="Owner B controlled writer integration.",
            observed_at=datetime(2026, 7, 16, 15, 1, tzinfo=timezone.utc),
            directness=1.0,
            source_reliability=0.9,
            independence_key="record-evidence-python-integration:b",
            sensitivity="low",
            metadata={"test": "controlled_writer"},
        )
        if second_owner == first:
            raise AssertionError("controlled writer reused evidence across owners")
    finally:
        await conn.close()
    print("memory_v1_record_evidence_api_integration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
