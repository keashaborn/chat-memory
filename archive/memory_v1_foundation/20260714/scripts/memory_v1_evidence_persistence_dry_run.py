#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_artifacts import load_artifact_manifest
from rag_engine.memory_v1_atomic_spans import build_atomic_span_plans
from rag_engine.memory_v1_compound_spans import resolve_compound_plans
from rag_engine.memory_v1_evidence_persistence import (
    REVIEW_DISPOSITIONS,
    build_evidence_span_plans,
    build_persistence_decisions,
    evidence_persistence_dry_run_report,
    fetch_existing_evidence_readonly,
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
            "Plan owner-scoped immutable evidence rows for reviewed Memory V1 "
            "atomic and compound spans. This command has no write or apply path."
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
        sources, existing_triage = await fetch_triage_batch_readonly(
            conn,
            args.owner,
            limit=args.limit,
        )
        triage_decisions = triage_sources(
            sources,
            artifact_entries=artifact_entries,
            existing_evidence=existing_triage,
        )
        atomic_plans = build_atomic_span_plans(triage_decisions)
        compound_resolutions = resolve_compound_plans(atomic_plans)
        span_plans = build_evidence_span_plans(
            atomic_plans,
            compound_resolutions,
            owner_user_id=args.owner,
        )
        existing_rows = await fetch_existing_evidence_readonly(
            conn,
            args.owner,
            span_plans,
        )
    finally:
        await conn.close()

    decisions = build_persistence_decisions(span_plans, existing_rows)
    atomic_review_count = sum(
        span.disposition in REVIEW_DISPOSITIONS
        for plan in atomic_plans
        for span in plan.spans
    )
    compound_review_count = sum(
        child.disposition in REVIEW_DISPOSITIONS
        for resolution in compound_resolutions
        for child in resolution.children
    )
    report = evidence_persistence_dry_run_report(
        decisions,
        owner_user_id=args.owner,
        atomic_review_span_count=atomic_review_count,
        compound_review_span_count=compound_review_count,
    )
    report["batch_source_count"] = len(triage_decisions)
    report["split_source_count"] = len(atomic_plans)
    report["compound_parent_count"] = len(compound_resolutions)
    report["artifact_manifest"] = str(Path(args.artifact_manifest).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
