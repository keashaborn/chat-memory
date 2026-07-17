#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_candidate_review import run_candidate_review_dry_run


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "manifests"
    / "memory_v1_candidate_review_20260713.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the owner-scoped Memory V1 candidate extraction and review "
            "plan. This command is read-only and exposes no apply path."
        )
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn)
    try:
        report = await run_candidate_review_dry_run(
            conn,
            manifest_path=args.manifest,
        )
    finally:
        await conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
