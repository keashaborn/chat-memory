#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag_engine.memory_v1_projection import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ClaimVectorIndex,
    process_owner_projection_outbox,
)
from rag_engine.openai_client import embed_text
from rag_engine.qdrant_compat import make_qdrant_client


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project one authenticated owner's Memory V1 claims to Qdrant."
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument(
        "--collection",
        default=os.environ.get("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION),
    )
    parser.add_argument("--vector-size", type=int, default=DEFAULT_VECTOR_SIZE)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--ensure-collection", action="store_true")
    parser.add_argument("--drain", action="store_true")
    return parser.parse_args()


async def main() -> int:
    args = arguments()
    dsn = os.environ.get("POSTGRES_DSN")
    qdrant_url = os.environ.get("QDRANT_URL")
    embed_model = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    if not qdrant_url:
        raise RuntimeError("QDRANT_URL is required")

    qdrant = make_qdrant_client(url=qdrant_url, timeout=15.0)
    index = ClaimVectorIndex(
        qdrant,
        collection_name=args.collection,
        vector_size=args.vector_size,
    )
    created = index.ensure_collection() if args.ensure_collection else False

    def embed(value: str):
        return embed_text(value, model=embed_model)

    conn = await asyncpg.connect(dsn, command_timeout=30)
    totals = {"claimed": 0, "upserted": 0, "deleted": 0, "errors": 0, "stale": 0}
    batches = 0
    try:
        while True:
            batch = await process_owner_projection_outbox(
                conn,
                args.owner_user_id,
                index=index,
                embedder=embed,
                limit=args.limit,
                max_attempts=args.max_attempts,
            )
            batches += 1
            for key in totals:
                totals[key] += int(batch[key])
            if not args.drain or batch["claimed"] == 0:
                break
            if batches >= 100:
                raise RuntimeError("projection drain exceeded 100 batches")
    finally:
        await conn.close()
        qdrant.close()

    print(
        json.dumps(
            {
                "version": "memory_claim_projection_worker_v1",
                "owner_user_id": args.owner_user_id,
                "collection": args.collection,
                "collection_created": created,
                "batches": batches,
                **totals,
            },
            sort_keys=True,
        )
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
