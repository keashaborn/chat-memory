#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_artifacts import load_artifact_manifest
from rag_engine.memory_v1_evidence_triage import (
    TriageCursor,
    fetch_triage_batch_readonly,
    triage_dry_run_report,
    triage_sources,
)


DEFAULT_ARTIFACT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "manifests"
    / "memory_v1_artifacts_20260713.json"
)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("cursor timestamp must include a timezone")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read authenticated chat turns and emit deterministic Memory V1 triage. "
            "This command has no database write path."
        )
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--after-created-at", type=parse_timestamp)
    parser.add_argument("--after-id")
    parser.add_argument("--artifact-manifest", default=str(DEFAULT_ARTIFACT_MANIFEST))
    return parser.parse_args()


def build_cursor(args: argparse.Namespace) -> TriageCursor | None:
    if bool(args.after_created_at) != bool(args.after_id):
        raise RuntimeError("--after-created-at and --after-id must be provided together")
    if not args.after_created_at:
        return None
    return TriageCursor(
        created_at=args.after_created_at,
        source_id=uuid.UUID(str(args.after_id)),
    )


async def main() -> int:
    args = parse_args()
    cursor = build_cursor(args)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    artifact_entries_list = load_artifact_manifest(args.artifact_manifest)
    artifact_entries = {
        entry.source_id: entry
        for entry in artifact_entries_list
        if str(entry.owner_user_id) == str(args.owner)
    }
    conn = await asyncpg.connect(dsn)
    try:
        sources, existing = await fetch_triage_batch_readonly(
            conn,
            args.owner,
            after=cursor,
            limit=args.limit,
        )
    finally:
        await conn.close()

    decisions = triage_sources(
        sources,
        artifact_entries=artifact_entries,
        existing_evidence=existing,
    )
    report = triage_dry_run_report(
        decisions,
        owner_user_id=args.owner,
        input_cursor=cursor,
    )
    report["artifact_manifest"] = str(Path(args.artifact_manifest).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
