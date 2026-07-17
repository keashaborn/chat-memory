#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_preference_project_durable_apply import (
    CONFIRMATION,
    run_controlled_preference_project_durable_apply,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    ROOT
    / "ops"
    / "manifests"
    / "memory_v1_preference_project_durable_apply_20260714.json"
)
DEFAULT_REVIEW_MANIFEST = (
    ROOT / "ops" / "manifests" / "memory_v1_preference_project_review_20260714.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify or transactionally apply the eight reviewed Memory V1 "
            "preference/project candidates. The production manifest is dry-run-only."
        )
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--review-manifest", default=str(DEFAULT_REVIEW_MANIFEST))
    parser.add_argument("--review-apply-report", required=True)
    parser.add_argument("--review-replay-report", required=True)
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
        result = await run_controlled_preference_project_durable_apply(
            conn,
            manifest_path=args.manifest,
            review_manifest_path=args.review_manifest,
            review_apply_report_path=args.review_apply_report,
            review_replay_report_path=args.review_replay_report,
            apply=args.apply,
            confirmation=args.confirm,
        )
    finally:
        await conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
