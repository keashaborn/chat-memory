#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

import asyncpg

from memory_v1_preference_project_apply_integration import (
    EXTRACTION_MANIFEST,
    build_fixture,
    seed_evidence_batch,
    write_json,
)
from memory_v1_preference_project_review_integration import build_review_manifest
from rag_engine.memory_v1_preference_project_apply import (
    CONFIRMATION as CANDIDATE_CONFIRMATION,
    run_controlled_preference_project_apply,
)
from rag_engine.memory_v1_preference_project_durable_apply import (
    CONFIRMATION,
    CONTROL_CONTRACT,
    EXPECTED_CONTRACT,
    MANIFEST_VERSION,
    PreferenceProjectDurableApplyError,
    run_controlled_preference_project_durable_apply,
)
from rag_engine.memory_v1_preference_project_review import (
    CONFIRMATION as REVIEW_CONFIRMATION,
    run_controlled_preference_project_review,
)


REQUEST_NAMESPACE = uuid.UUID("7f936952-4ab3-4d9b-bfbc-aa62c8fc724f")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_durable_manifest(
    path: Path,
    *,
    extraction_report_path: Path,
    review_manifest_path: Path,
    review_apply_report_path: Path,
    review_replay_report_path: Path,
    project_id: str,
    apply_authorized: bool,
) -> Path:
    extraction_report = json.loads(extraction_report_path.read_text(encoding="utf-8"))
    review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    review_apply_report = json.loads(
        review_apply_report_path.read_text(encoding="utf-8")
    )
    review_sha = file_sha(review_manifest_path)
    metadata = {
        "apply_contract": "memory_v1_preference_project_durable_apply_v1",
        "source_review_manifest_sha256": review_sha,
    }
    preference_plans = {
        item["candidate_id"]: item
        for item in extraction_report["preference_lane"]["candidates"]
    }
    project_plans = {
        item["candidate_id"]: item
        for item in extraction_report["project_lane"]["candidates"]
    }

    def preference_item(source: dict[str, object]) -> dict[str, object]:
        candidate_id = str(source["candidate_id"])
        plan = preference_plans[candidate_id]
        return {
            "candidate_id": candidate_id,
            "candidate_hash": source["candidate_hash"],
            "accepted_review_id": review_apply_report["review_ids"][candidate_id],
            "review_request_id": source["request_id"],
            "apply_request_id": str(
                uuid.uuid5(REQUEST_NAMESPACE, f"preference:{candidate_id}")
            ),
            "head_key": plan["preference_key"],
            "expected_current_revision_id": None,
            "event_metadata": metadata,
        }

    def project_item(source: dict[str, object]) -> dict[str, object]:
        candidate_id = str(source["candidate_id"])
        plan = project_plans[candidate_id]
        return {
            "candidate_id": candidate_id,
            "candidate_hash": source["candidate_hash"],
            "accepted_review_id": review_apply_report["review_ids"][candidate_id],
            "review_request_id": source["request_id"],
            "apply_request_id": str(
                uuid.uuid5(REQUEST_NAMESPACE, f"project:{candidate_id}")
            ),
            "head_key": plan["knowledge_key"],
            "knowledge_kind": plan["knowledge_kind"],
            "expected_current_revision_id": None,
            "event_metadata": metadata,
        }

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "owner_user_id": review_manifest["owner_user_id"],
        "project_id": project_id,
        "project_key": review_manifest["project_key"],
        "dry_run_authorized": True,
        "apply_authorized": apply_authorized,
        "review_manifest_sha256": review_sha,
        "review_apply_report_sha256": file_sha(review_apply_report_path),
        "review_replay_report_sha256": file_sha(review_replay_report_path),
        "expected": EXPECTED_CONTRACT,
        "preference_applies": [
            preference_item(item) for item in review_manifest["preference_reviews"]
        ],
        "project_applies": [
            project_item(item) for item in review_manifest["project_reviews"]
        ],
        "controls": CONTROL_CONTRACT,
    }
    write_json(path, manifest)
    return path


async def durable_counts(conn: asyncpg.Connection, owner: uuid.UUID) -> dict[str, int]:
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.user_preference) AS preference_heads,
              (SELECT count(*) FROM memory.preference_revision) AS preference_revisions,
              (SELECT count(*) FROM memory.preference_revision_evidence) AS preference_links,
              (SELECT count(*) FROM memory.preference_apply_event) AS preference_events,
              (SELECT count(*) FROM memory.project_knowledge_head) AS project_heads,
              (SELECT count(*) FROM memory.project_knowledge_revision) AS project_revisions,
              (SELECT count(*) FROM memory.project_knowledge_revision_evidence) AS project_links,
              (SELECT count(*) FROM memory.project_knowledge_apply_event) AS project_events
            """
        )
    return {key: int(value) for key, value in dict(row).items()}


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    database_name = f"memoryv1_durable_apply_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn, database="postgres")
    conn: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}" TEMPLATE memory')
        conn = await asyncpg.connect(dsn, database=database_name)
        with tempfile.TemporaryDirectory(prefix="memory-v1-durable-apply-") as temp:
            root = Path(temp)
            extraction, states, extraction_report, candidate_manifest = build_fixture(root)
            await seed_evidence_batch(conn, extraction, states)
            candidate_result = await run_controlled_preference_project_apply(
                conn,
                manifest_path=candidate_manifest,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=CANDIDATE_CONFIRMATION,
            )
            candidate_report = root / "candidate_apply_report.json"
            write_json(candidate_report, candidate_result)

            review_manifest = build_review_manifest(
                root / "review_manifest.json",
                candidate_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                project_id=candidate_result["project_id"],
            )
            review_result = await run_controlled_preference_project_review(
                conn,
                manifest_path=review_manifest,
                candidate_apply_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=REVIEW_CONFIRMATION,
            )
            review_apply_report = root / "review_apply_report.json"
            write_json(review_apply_report, review_result)
            review_replay = await run_controlled_preference_project_review(
                conn,
                manifest_path=review_manifest,
                candidate_apply_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=REVIEW_CONFIRMATION,
            )
            review_replay_report = root / "review_replay_report.json"
            write_json(review_replay_report, review_replay)

            dry_run_manifest = build_durable_manifest(
                root / "durable_dry_run_manifest.json",
                extraction_report_path=extraction_report,
                review_manifest_path=review_manifest,
                review_apply_report_path=review_apply_report,
                review_replay_report_path=review_replay_report,
                project_id=candidate_result["project_id"],
                apply_authorized=False,
            )
            dry_run = await run_controlled_preference_project_durable_apply(
                conn,
                manifest_path=dry_run_manifest,
                review_manifest_path=review_manifest,
                review_apply_report_path=review_apply_report,
                review_replay_report_path=review_replay_report,
            )
            if (
                dry_run["status"] != "dry_run_verified"
                or dry_run["database_writes"] != 0
                or dry_run["projected_database_writes"] != 32
                or dry_run["apply_authorized"]
            ):
                raise AssertionError("authorization-disabled durable dry run failed")
            try:
                await run_controlled_preference_project_durable_apply(
                    conn,
                    manifest_path=dry_run_manifest,
                    review_manifest_path=review_manifest,
                    review_apply_report_path=review_apply_report,
                    review_replay_report_path=review_replay_report,
                    apply=True,
                    confirmation=CONFIRMATION,
                )
            except PreferenceProjectDurableApplyError:
                pass
            else:
                raise AssertionError("dry-run manifest authorized a durable write")

            apply_manifest = build_durable_manifest(
                root / "durable_apply_manifest.json",
                extraction_report_path=extraction_report,
                review_manifest_path=review_manifest,
                review_apply_report_path=review_apply_report,
                review_replay_report_path=review_replay_report,
                project_id=candidate_result["project_id"],
                apply_authorized=True,
            )
            try:
                await run_controlled_preference_project_durable_apply(
                    conn,
                    manifest_path=apply_manifest,
                    review_manifest_path=review_manifest,
                    review_apply_report_path=review_apply_report,
                    review_replay_report_path=review_replay_report,
                    apply=True,
                    confirmation="WRONG",
                )
            except PreferenceProjectDurableApplyError:
                pass
            else:
                raise AssertionError("durable apply accepted the wrong confirmation")

            committed = await run_controlled_preference_project_durable_apply(
                conn,
                manifest_path=apply_manifest,
                review_manifest_path=review_manifest,
                review_apply_report_path=review_apply_report,
                review_replay_report_path=review_replay_report,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if committed["status"] != "committed" or committed["database_writes"] != 32:
                raise AssertionError("durable apply did not commit exactly 32 rows")
            expected_counts = {
                "preference_heads": 3,
                "preference_revisions": 3,
                "preference_links": 3,
                "preference_events": 3,
                "project_heads": 5,
                "project_revisions": 5,
                "project_links": 5,
                "project_events": 5,
            }
            counts_after_commit = await durable_counts(conn, extraction.owner_user_id)
            if counts_after_commit != expected_counts:
                raise AssertionError(f"durable row counts changed: {counts_after_commit}")

            replay = await run_controlled_preference_project_durable_apply(
                conn,
                manifest_path=apply_manifest,
                review_manifest_path=review_manifest,
                review_apply_report_path=review_apply_report,
                review_replay_report_path=review_replay_report,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if replay["status"] != "verified_replay" or replay["database_writes"] != 0:
                raise AssertionError("durable apply replay was not exact and zero-write")
            if await durable_counts(conn, extraction.owner_user_id) != counts_after_commit:
                raise AssertionError("durable apply replay changed database counts")

            other_owner = "22222222-2222-4222-8222-222222222222"
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL ROLE brains_app")
                await conn.execute("SELECT set_config('app.user_id',$1,true)", other_owner)
                leaked = await conn.fetchval(
                    """
                    SELECT
                      (SELECT count(*) FROM memory.user_preference)
                      + (SELECT count(*) FROM memory.preference_revision)
                      + (SELECT count(*) FROM memory.project_knowledge_head)
                      + (SELECT count(*) FROM memory.project_knowledge_revision)
                    """
                )
            if leaked != 0:
                raise AssertionError("cross-owner durable rows were visible")
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
    print("memory_v1_preference_project_durable_apply_integration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
