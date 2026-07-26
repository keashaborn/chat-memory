#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    EvidenceContextContractError,
    load_memory_evidence_context_v1,
)
from rag_engine.memory_v1_evidence_context_v1 import (
    sanitized_evidence_context_report_v1,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build one sanitized, read-only evidence-context window report."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument("--max-spans", type=int, default=12)
    return parser.parse_args()


async def run() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    target = uuid.UUID(args.target_evidence_id)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("probe requires brains_app")
        envelope = await load_memory_evidence_context_v1(
            conn,
            expected_owner_user_id=owner,
            target_evidence_id=target,
            expected_target_content_sha256=(
                args.expected_target_content_sha256
            ),
            max_spans=args.max_spans,
        )
    except EvidenceContextContractError as exc:
        print(
            json.dumps(
                {
                    "outcome": "rejected",
                    "rejection_class": type(exc).__name__,
                    "write_counts": {
                        "database": 0,
                        "qdrant": 0,
                        "retrieval": 0,
                        "prompt_influence": 0,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        await conn.close()

    print(
        json.dumps(
            {
                "outcome": "accepted",
                "report": sanitized_evidence_context_report_v1(envelope),
                "write_counts": {
                    "database": 0,
                    "qdrant": 0,
                    "retrieval": 0,
                    "prompt_influence": 0,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
