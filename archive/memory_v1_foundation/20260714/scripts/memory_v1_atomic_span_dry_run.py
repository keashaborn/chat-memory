#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_artifacts import load_artifact_manifest
from rag_engine.memory_v1_atomic_spans import (
    atomic_span_dry_run_report,
    build_atomic_span_plans,
)
from rag_engine.memory_v1_evidence_triage import (
    fetch_triage_batch_readonly,
    triage_sources,
)


DEFAULT_ARTIFACT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "manifests"
    / "memory_v1_artifacts_20260713.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Emit exact, review-only atomic source spans for split-required "
            "Memory V1 chat turns. This command has no database write path."
        )
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--artifact-manifest", default=str(DEFAULT_ARTIFACT_MANIFEST))
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    artifact_entries = {
        entry.source_id: entry
        for entry in load_artifact_manifest(args.artifact_manifest)
        if str(entry.owner_user_id) == str(args.owner)
    }
    conn = await asyncpg.connect(dsn)
    try:
        sources, existing = await fetch_triage_batch_readonly(
            conn,
            args.owner,
            limit=args.limit,
        )
    finally:
        await conn.close()

    decisions = triage_sources(
        sources,
        artifact_entries=artifact_entries,
        existing_evidence=existing,
    )
    plans = build_atomic_span_plans(decisions)
    report = atomic_span_dry_run_report(
        plans,
        owner_user_id=args.owner,
        batch_source_count=len(decisions),
    )
    report["artifact_manifest"] = str(Path(args.artifact_manifest).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
