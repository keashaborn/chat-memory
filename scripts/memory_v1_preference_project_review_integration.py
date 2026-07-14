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
from rag_engine.memory_v1_preference_project_apply import (
    CONFIRMATION as APPLY_CONFIRMATION,
    run_controlled_preference_project_apply,
)
from rag_engine.memory_v1_preference_project_review import (
    CONFIRMATION,
    CONTROL_CONTRACT,
    MANIFEST_VERSION,
    PreferenceProjectReviewError,
    run_controlled_preference_project_review,
)


REVIEW_NAMESPACE = uuid.UUID("eb7a937c-03b2-4fd2-a9f1-a78bd144c723")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_review_manifest(
    path: Path,
    *,
    candidate_manifest_path: Path,
    candidate_apply_report_path: Path,
    project_id: str,
) -> Path:
    candidate_manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    metadata = {
        "authorization_source": "user_instruction_continue_controlled_review_20260713",
        "durable_apply_authorized": False,
        "review_contract": "memory_v1_controlled_review_v1",
    }

    def decisions(lane: str) -> list[dict[str, object]]:
        return [
            {
                "candidate_id": item["candidate_id"],
                "candidate_hash": item["candidate_hash"],
                "request_id": str(
                    uuid.uuid5(REVIEW_NAMESPACE, f"{lane}:{item['candidate_id']}")
                ),
                "decision": "accept",
                "rationale": f"Controlled integration review for {lane} candidate {index}.",
                "reason_codes": [
                    "canonical_candidate_verified",
                    "controlled_review_integration",
                ],
                "replacement_candidate_ids": [],
                "metadata": metadata,
            }
            for index, item in enumerate(candidate_manifest[f"{lane}_candidates"])
        ]

    owner = candidate_manifest["owner_user_id"]
    review_manifest = {
        "manifest_version": MANIFEST_VERSION,
        "owner_user_id": owner,
        "project_id": project_id,
        "project_key": candidate_manifest["project_registration"]["project_key"],
        "review_authorized": True,
        "candidate_apply_manifest_sha256": file_sha(candidate_manifest_path),
        "candidate_apply_report_sha256": file_sha(candidate_apply_report_path),
        "reviewer_type": "user",
        "reviewer_ref": owner,
        "expected": {
            "preference_reviews": 3,
            "project_reviews": 5,
            "review_replacements": 0,
            "durable_preferences": 0,
            "durable_project_heads": 0,
            "durable_preference_revisions": 0,
            "durable_project_revisions": 0,
            "preference_apply_events": 0,
            "project_apply_events": 0,
        },
        "preference_reviews": decisions("preference"),
        "project_reviews": decisions("project"),
        "controls": CONTROL_CONTRACT,
    }
    write_json(path, review_manifest)
    return path


async def review_counts(conn: asyncpg.Connection, owner: uuid.UUID) -> dict[str, int]:
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.preference_candidate_review) AS preference_reviews,
              (SELECT count(*) FROM memory.preference_candidate_review_replacement) AS preference_replacements,
              (SELECT count(*) FROM memory.project_knowledge_candidate_review) AS project_reviews,
              (SELECT count(*) FROM memory.project_knowledge_candidate_review_replacement) AS project_replacements,
              (SELECT count(*) FROM memory.user_preference) AS durable_preferences,
              (SELECT count(*) FROM memory.preference_revision) AS preference_revisions,
              (SELECT count(*) FROM memory.preference_apply_event) AS preference_apply_events,
              (SELECT count(*) FROM memory.project_knowledge_head) AS durable_project_heads,
              (SELECT count(*) FROM memory.project_knowledge_revision) AS project_revisions,
              (SELECT count(*) FROM memory.project_knowledge_apply_event) AS project_apply_events
            """
        )
    return {key: int(value) for key, value in dict(row).items()}


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    database_name = f"memoryv1_candidate_review_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn, database="postgres")
    conn: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}" TEMPLATE memory')
        conn = await asyncpg.connect(dsn, database=database_name)
        with tempfile.TemporaryDirectory(prefix="memory-v1-preference-project-review-") as temp:
            root = Path(temp)
            extraction, states, extraction_report, candidate_manifest = build_fixture(root)
            await seed_evidence_batch(conn, extraction, states)
            candidate_result = await run_controlled_preference_project_apply(
                conn,
                manifest_path=candidate_manifest,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=APPLY_CONFIRMATION,
            )
            if candidate_result["status"] != "committed":
                raise AssertionError("candidate fixture did not commit")
            candidate_report = root / "candidate_apply_report.json"
            write_json(candidate_report, candidate_result)
            review_manifest = build_review_manifest(
                root / "review_manifest.json",
                candidate_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                project_id=candidate_result["project_id"],
            )

            preflight = await run_controlled_preference_project_review(
                conn,
                manifest_path=review_manifest,
                candidate_apply_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
            )
            if preflight["status"] != "preflight_verified" or not preflight["ready_for_review"]:
                raise AssertionError("controlled review preflight failed")
            try:
                await run_controlled_preference_project_review(
                    conn,
                    manifest_path=review_manifest,
                    candidate_apply_manifest_path=candidate_manifest,
                    candidate_apply_report_path=candidate_report,
                    extraction_manifest_path=EXTRACTION_MANIFEST,
                    extraction_report_path=extraction_report,
                    apply=True,
                    confirmation="WRONG",
                )
            except PreferenceProjectReviewError:
                pass
            else:
                raise AssertionError("controlled review accepted the wrong confirmation")

            committed = await run_controlled_preference_project_review(
                conn,
                manifest_path=review_manifest,
                candidate_apply_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if committed["status"] != "committed" or committed["database_writes"] != 8:
                raise AssertionError("controlled review did not commit exactly eight rows")
            expected_counts = {
                "preference_reviews": 3,
                "preference_replacements": 0,
                "project_reviews": 5,
                "project_replacements": 0,
                "durable_preferences": 0,
                "preference_revisions": 0,
                "preference_apply_events": 0,
                "durable_project_heads": 0,
                "project_revisions": 0,
                "project_apply_events": 0,
            }
            counts_after_commit = await review_counts(conn, extraction.owner_user_id)
            if counts_after_commit != expected_counts:
                raise AssertionError(f"review-only counts changed: {counts_after_commit}")

            replay = await run_controlled_preference_project_review(
                conn,
                manifest_path=review_manifest,
                candidate_apply_manifest_path=candidate_manifest,
                candidate_apply_report_path=candidate_report,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=extraction_report,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if replay["status"] != "verified_replay" or replay["database_writes"] != 0:
                raise AssertionError("controlled review replay was not exact and zero-write")
            if await review_counts(conn, extraction.owner_user_id) != counts_after_commit:
                raise AssertionError("controlled review replay changed database counts")

            other_owner = "22222222-2222-4222-8222-222222222222"
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL ROLE brains_app")
                await conn.execute("SELECT set_config('app.user_id',$1,true)", other_owner)
                leaked = await conn.fetchval(
                    """
                    SELECT
                      (SELECT count(*) FROM memory.preference_candidate_review)
                      + (SELECT count(*) FROM memory.project_knowledge_candidate_review)
                    """
                )
            if leaked != 0:
                raise AssertionError("cross-owner review rows were visible")
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
    print("memory_v1_preference_project_review_integration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
