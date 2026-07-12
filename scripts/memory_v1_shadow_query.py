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
)
from rag_engine.memory_v1_retrieval import build_memory_packet
from rag_engine.openai_client import embed_text
from rag_engine.qdrant_compat import make_qdrant_client


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Memory V1 retrieval without sending results to the live prompt."
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--explicit-recall", action="store_true")
    parser.add_argument("--max-claims", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument("--max-sensitivity", default="medium")
    parser.add_argument("--candidate-limit", type=int, default=24)
    parser.add_argument(
        "--collection",
        default=os.environ.get("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION),
    )
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

    query_vector = await asyncio.to_thread(
        embed_text, args.query, model=embed_model
    )
    qdrant = make_qdrant_client(url=qdrant_url, timeout=15.0)
    index = ClaimVectorIndex(
        qdrant,
        collection_name=args.collection,
        vector_size=DEFAULT_VECTOR_SIZE,
    )
    candidate_hits = await asyncio.to_thread(
        index.search_claims,
        args.owner_user_id,
        query_vector,
        limit=args.candidate_limit,
    )

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        packet = await build_memory_packet(
            conn,
            args.owner_user_id,
            query=args.query,
            intent=args.intent,
            domain=args.domain,
            candidate_hits=candidate_hits,
            max_claims=args.max_claims,
            max_tokens=args.max_tokens,
            max_sensitivity=args.max_sensitivity,
            explicit_recall=args.explicit_recall,
        )
    finally:
        await conn.close()
        qdrant.close()

    print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
