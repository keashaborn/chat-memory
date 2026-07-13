#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_artifacts import (
    artifact_dry_run_report,
    build_manifest_plans,
    fetch_manifest_sources_readonly,
    load_artifact_manifest,
)


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "manifests"
    / "memory_v1_artifacts_20260713.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate hash-locked artifact sources and emit a section-level report. "
            "This command has no database write path."
        )
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="omit per-section metadata from the report",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    entries = load_artifact_manifest(args.manifest)
    conn = await asyncpg.connect(dsn)
    try:
        sources = await fetch_manifest_sources_readonly(conn, entries)
        plans = build_manifest_plans(entries, sources)
    finally:
        await conn.close()

    report = artifact_dry_run_report(
        plans,
        include_sections=not args.summary_only,
    )
    report["manifest"] = str(Path(args.manifest).resolve())
    report["manifest_version"] = entries[0].manifest_version
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
