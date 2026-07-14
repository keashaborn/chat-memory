#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_preference_project_apply import (
    CONFIRMATION,
    run_controlled_preference_project_apply,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTHORIZATION = (
    ROOT / "ops" / "manifests" / "memory_v1_preference_project_apply_20260713.json"
)
DEFAULT_EXTRACTION_MANIFEST = (
    ROOT
    / "ops"
    / "manifests"
    / "memory_v1_preference_project_extraction_20260713.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or transactionally persist the hash-locked Memory V1 "
            "preference and project candidates without creating reviews or durable records."
        )
    )
    parser.add_argument("--manifest", default=str(DEFAULT_AUTHORIZATION))
    parser.add_argument(
        "--extraction-manifest", default=str(DEFAULT_EXTRACTION_MANIFEST)
    )
    parser.add_argument("--extraction-report", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    if args.apply and args.confirm != CONFIRMATION:
        raise RuntimeError(f"--confirm must equal {CONFIRMATION}")
    conn = await asyncpg.connect(dsn)
    try:
        result = await run_controlled_preference_project_apply(
            conn,
            manifest_path=args.manifest,
            extraction_manifest_path=args.extraction_manifest,
            extraction_report_path=args.extraction_report,
            apply=args.apply,
            confirmation=args.confirm,
        )
    finally:
        await conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
