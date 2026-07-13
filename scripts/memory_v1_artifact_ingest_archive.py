#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_artifacts import (
    build_manifest_plans,
    fetch_manifest_sources_readonly,
    load_artifact_manifest,
    persist_artifact_plan,
)


EXPECTED_OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
EXPECTED_MANIFEST_SHA256 = "f1f97c5a172f0c33ed2655233932b489dd9fb2156070d176da0b3cd9534fb856"
EXPECTED_ARTIFACTS = 5
EXPECTED_SECTIONS = 213
CONFIRMATION = "ARCHIVE_ONLY_NO_RETRIEVAL_NO_PROMOTION"
DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "manifests"
    / "memory_v1_artifacts_20260713.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-transaction archive-only ingestion for the reviewed 2026-07-13 manifest."
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def validate_cutover(args: argparse.Namespace) -> tuple[Path, list]:
    if not args.apply:
        raise RuntimeError("refusing write: --apply is required")
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"refusing write: --confirm must equal {CONFIRMATION}")

    manifest_path = Path(args.manifest).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
        raise RuntimeError(
            "refusing write: manifest hash mismatch: "
            f"expected {EXPECTED_MANIFEST_SHA256}, got {manifest_sha256}"
        )

    entries = load_artifact_manifest(manifest_path)
    if len(entries) != EXPECTED_ARTIFACTS:
        raise RuntimeError(
            f"refusing write: expected {EXPECTED_ARTIFACTS} artifacts, got {len(entries)}"
        )
    if {str(entry.owner_user_id) for entry in entries} != {EXPECTED_OWNER}:
        raise RuntimeError("refusing write: manifest owner mismatch")
    if any(entry.extraction_policy != "review_only" for entry in entries):
        raise RuntimeError("refusing write: every artifact must be review_only")
    if any(entry.endorsement_level == "ratified" for entry in entries):
        raise RuntimeError("refusing write: archive ingestion must not ratify policy")
    return manifest_path, entries


async def main() -> int:
    args = parse_args()
    manifest_path, entries = validate_cutover(args)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn)
    try:
        sources = await fetch_manifest_sources_readonly(conn, entries)
        plans = build_manifest_plans(entries, sources)
        if sum(len(plan.sections) for plan in plans) != EXPECTED_SECTIONS:
            raise RuntimeError(
                "refusing write: section count changed from reviewed dry run"
            )

        artifact_ids = [plan.artifact_id for plan in plans]
        results = []
        async with conn.transaction():
            for plan in plans:
                results.append(
                    await persist_artifact_plan(conn, EXPECTED_OWNER, plan)
                )

            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", EXPECTED_OWNER
            )
            counts = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM memory.artifact
                   WHERE artifact_id = ANY($1::uuid[])) AS artifacts,
                  (SELECT count(*) FROM memory.artifact_occurrence
                   WHERE artifact_id = ANY($1::uuid[])) AS occurrences,
                  (SELECT count(*) FROM memory.artifact_section
                   WHERE artifact_id = ANY($1::uuid[])) AS sections,
                  (SELECT count(*) FROM memory.artifact_endorsement
                   WHERE artifact_id = ANY($1::uuid[])) AS endorsements,
                  (SELECT count(*) FROM memory.artifact
                   WHERE artifact_id = ANY($1::uuid[])
                     AND extraction_policy <> 'review_only') AS non_archive_artifacts,
                  (SELECT count(*) FROM memory.artifact_section
                   WHERE artifact_id = ANY($1::uuid[])
                     AND (retrieval_eligible OR promotion_eligible)) AS enabled_sections
                """,
                artifact_ids,
            )
            expected = {
                "artifacts": EXPECTED_ARTIFACTS,
                "occurrences": EXPECTED_ARTIFACTS,
                "sections": EXPECTED_SECTIONS,
                "endorsements": EXPECTED_ARTIFACTS,
                "non_archive_artifacts": 0,
                "enabled_sections": 0,
            }
            if dict(counts) != expected:
                raise RuntimeError(
                    f"refusing commit: archive-only verification failed: {dict(counts)}"
                )

        print(
            json.dumps(
                {
                    "status": "committed",
                    "mode": "archive_only_no_retrieval_no_promotion",
                    "manifest": str(manifest_path),
                    "manifest_sha256": EXPECTED_MANIFEST_SHA256,
                    "counts": dict(counts),
                    "inserted": [result["inserted"] for result in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
