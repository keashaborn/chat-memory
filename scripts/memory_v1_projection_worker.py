#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag_engine.memory_v1_projection import (
    DEFAULT_VECTOR_SIZE,
    ClaimVectorIndex,
    process_owner_projection_outbox,
)
from seebx.adapters.openai import embed_text
from rag_engine.qdrant_compat import make_qdrant_client
from scripts.memory_v1_authenticated_owners import (
    MAX_AUTHENTICATED_OWNERS,
    explicit_owners,
    resolve_authenticated_owners,
)


WORKER_VERSION = "memory_claim_projection_worker_v2"
MAX_DRAIN_BATCHES_PER_OWNER = 100
COUNTER_KEYS = ("claimed", "upserted", "deleted", "errors", "stale")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project authenticated Memory V1 claim owners to Qdrant."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument(
        "--collection",
        default=os.environ.get("MEMORY_V1_COLLECTION"),
    )
    parser.add_argument("--vector-size", type=int, default=DEFAULT_VECTOR_SIZE)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--lease-seconds", type=int, default=600)
    parser.add_argument("--ensure-collection", action="store_true")
    parser.add_argument("--drain", action="store_true")
    return parser.parse_args()


async def _owners(args: argparse.Namespace, dsn: str) -> list[uuid.UUID]:
    cli_owners = explicit_owners(list(args.owner_user_id))
    if cli_owners:
        return cli_owners
    configured = explicit_owners(
        [
        value.strip()
        for value in os.getenv("MEMORY_V1_PROJECTION_OWNER_IDS", "").split(",")
        if value.strip()
        ]
    )
    discovered = await resolve_authenticated_owners(dsn, [])
    owners = sorted({*configured, *discovered}, key=str)
    if not owners:
        raise RuntimeError("no authenticated projection owner is available")
    if len(owners) > MAX_AUTHENTICATED_OWNERS:
        raise RuntimeError(
            f"authenticated projection owner count exceeds {MAX_AUTHENTICATED_OWNERS}"
        )
    return owners


def _validate_limits(args: argparse.Namespace) -> None:
    if not getattr(args, "collection", None):
        raise RuntimeError("MEMORY_V1_COLLECTION or --collection is required")
    if not 1 <= int(args.limit) <= 100:
        raise RuntimeError("limit must be between 1 and 100")
    if not 1 <= int(args.max_attempts) <= 50:
        raise RuntimeError("max-attempts must be between 1 and 50")
    if not 30 <= int(args.lease_seconds) <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if int(args.vector_size) < 1:
        raise RuntimeError("vector-size must be positive")


async def _process_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    *,
    index: ClaimVectorIndex,
    embedder,
    limit: int,
    max_attempts: int,
    worker_id: str,
    lease_seconds: int,
    drain: bool,
) -> dict[str, Any]:
    totals = {key: 0 for key in COUNTER_KEYS}
    batches = 0
    while True:
        batch = await process_owner_projection_outbox(
            conn,
            owner,
            index=index,
            embedder=embedder,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=limit,
            max_attempts=max_attempts,
        )
        batches += 1
        for key in COUNTER_KEYS:
            totals[key] += int(batch[key])
        if not drain or batch["claimed"] == 0:
            break
        if batches >= MAX_DRAIN_BATCHES_PER_OWNER:
            raise RuntimeError(
                f"projection drain exceeded {MAX_DRAIN_BATCHES_PER_OWNER} "
                f"batches for owner {owner}"
            )
    return {"batches": batches, **totals}


async def main() -> int:
    args = arguments()
    _validate_limits(args)
    dsn = os.environ.get("POSTGRES_DSN")
    qdrant_url = os.environ.get("QDRANT_URL")
    embed_model = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    if not qdrant_url:
        raise RuntimeError("QDRANT_URL is required")
    if embed_model != "text-embedding-3-large" or args.vector_size != 3072:
        raise RuntimeError("projection provenance requires text-embedding-3-large/3072")
    owners = await _owners(args, dsn)

    qdrant = make_qdrant_client(url=qdrant_url, timeout=15.0)
    index = ClaimVectorIndex(
        qdrant,
        collection_name=args.collection,
        vector_size=args.vector_size,
        embedding_model=embed_model,
    )
    created = index.ensure_collection() if args.ensure_collection else False

    def embed(value: str):
        return embed_text(value, model=embed_model)

    conn = await asyncpg.connect(dsn, command_timeout=30)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    totals = {key: 0 for key in COUNTER_KEYS}
    owner_results: dict[str, dict[str, Any]] = {}
    try:
        for owner in owners:
            result = await _process_owner(
                conn,
                owner,
                index=index,
                embedder=embed,
                limit=args.limit,
                max_attempts=args.max_attempts,
                worker_id=worker_id,
                lease_seconds=args.lease_seconds,
                drain=bool(args.drain),
            )
            owner_results[str(owner)] = result
            for key in COUNTER_KEYS:
                totals[key] += int(result[key])
    finally:
        await conn.close()
        qdrant.close()

    print(
        json.dumps(
            {
                "version": WORKER_VERSION,
                "owner_count": len(owners),
                "owners": owner_results,
                "collection": args.collection,
                "collection_created": created,
                **totals,
            },
            sort_keys=True,
        )
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
