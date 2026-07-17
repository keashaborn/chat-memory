#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_preference_project_apply import (
    CONFIRMATION,
    CONTROL_CONTRACT,
    MANIFEST_VERSION,
    PreferenceProjectApplyError,
    run_controlled_preference_project_apply,
)
from rag_engine.memory_v1_preference_project_extraction import (
    EvidenceState,
    build_extraction_plan,
    load_extraction_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_MANIFEST = (
    ROOT
    / "ops"
    / "manifests"
    / "memory_v1_preference_project_extraction_20260713.json"
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def extraction_database_controls() -> dict[str, object]:
    return {
        "effective_role_brains_app": True,
        "transaction_read_only": True,
        "specialized_tables_forced_rls": True,
        "evidence_forced_rls": True,
        "preference_candidate_insert_allowed": True,
        "project_candidate_insert_allowed": True,
        "direct_preference_review_insert_denied": True,
        "direct_project_review_insert_denied": True,
        "direct_user_preference_insert_denied": True,
        "direct_project_head_insert_denied": True,
        "project_registration_execute_allowed": True,
        "existing_preference_candidates": 0,
        "existing_project_candidates": 0,
        "existing_project_spaces": 0,
        "existing_preference_reviews": 0,
        "existing_project_reviews": 0,
        "existing_durable_preferences": 0,
        "existing_durable_project_heads": 0,
    }


def build_fixture(root: Path):
    extraction = load_extraction_manifest(EXTRACTION_MANIFEST)
    decisions = []
    states = []
    observed = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    for index, mapping in enumerate(extraction.mappings):
        content_hash = sha(f"apply-integration-evidence-{index}")
        target = mapping["target"]
        decisions.append(
            {
                "evidence_id": mapping["evidence_id"],
                "review_unit_id": f"apply-integration-review-{index}",
                "input_lock_sha256": sha(f"apply-integration-input-{index}"),
                "content_sha256": content_hash,
                "target": target,
                "decision": "rewrite",
                "recommended_action": (
                    "preference_candidate_after_schema"
                    if target == "preference"
                    else "project_candidate_after_schema"
                ),
                "canonical_drafts": [f"Controlled candidate fixture {index}."],
                "reason_codes": ["controlled_apply_integration"],
            }
        )
        states.append(
            EvidenceState(
                evidence_id=uuid.UUID(mapping["evidence_id"]),
                ledger_content_sha256=content_hash,
                content_sha256=content_hash,
                observed_at=observed,
                sensitivity="high",
                status="active",
            )
        )
    for index in range(2):
        decisions.append(
            {
                "evidence_id": f"deferred-project-{index}",
                "target": "project_knowledge",
                "decision": "defer",
                "recommended_action": "reextract_expanded_context",
                "canonical_drafts": [],
            }
        )
    source_review = {"packets": [{"route": "integration", "decisions": decisions}]}
    report = build_extraction_plan(
        extraction,
        source_review,
        states,
        extraction_database_controls(),
    )
    report_path = root / "extraction_report.json"
    report_sha = write_json(report_path, report)
    registration = report["project_registration"]
    authorization = {
        "manifest_version": MANIFEST_VERSION,
        "owner_user_id": str(extraction.owner_user_id),
        "batch_id": str(extraction.batch_id),
        "apply_authorized": True,
        "extraction_manifest_sha256": extraction.sha256,
        "extraction_report_sha256": report_sha,
        "project_registration": {
            "request_id": registration["request_id"],
            "project_key": registration["project_key"],
            "display_name": registration["display_name"],
        },
        "expected": {
            "preference_candidates": 3,
            "preference_evidence_links": 3,
            "project_candidates": 5,
            "project_evidence_links": 5,
            "project_spaces": 1,
            "project_registration_events": 1,
            "preference_reviews": 0,
            "project_reviews": 0,
            "durable_preferences": 0,
            "durable_project_heads": 0,
            "durable_preference_revisions": 0,
            "durable_project_revisions": 0,
        },
        "preference_candidates": [
            {
                "candidate_id": row["candidate_id"],
                "candidate_hash": row["candidate_hash"],
            }
            for row in report["preference_lane"]["candidates"]
        ],
        "project_candidates": [
            {
                "candidate_id": row["candidate_id"],
                "candidate_hash": row["candidate_hash"],
            }
            for row in report["project_lane"]["candidates"]
        ],
        "controls": CONTROL_CONTRACT,
    }
    authorization_path = root / "authorization.json"
    write_json(authorization_path, authorization)
    return extraction, states, report_path, authorization_path


async def seed_evidence_batch(conn, extraction, states) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", str(extraction.owner_user_id)
        )
        evidence_payload = [
            (
                state.evidence_id,
                extraction.owner_user_id,
                f"apply-integration-{index}",
                f"Controlled evidence fixture {index}.",
                state.content_sha256,
                state.observed_at,
            )
            for index, state in enumerate(states)
        ]
        await conn.executemany(
            """
            INSERT INTO memory.evidence(
              evidence_id, owner_user_id, kind, source_system, external_id,
              content, content_sha256, observed_at, sensitivity, status, metadata
            ) VALUES($1,$2,'user_statement','apply_integration',$3,$4,$5,$6,'high','active','{}')
            """,
            evidence_payload,
        )
        await conn.execute(
            """
            INSERT INTO memory.evidence_ingest_batch(
              batch_id, owner_user_id, batch_key, manifest_version, plan_version,
              input_fingerprint_sha256, reviewed_report_sha256,
              authorization_manifest_sha256, source_snapshot_sha256,
              source_row_count, expected_evidence_count, inserted_count, reused_count,
              actor_user_id, invoked_by_role, metadata
            ) VALUES(
              $1,$2,'apply-integration-source-batch','integration','integration',
              $3,$3,$3,$3,$4,$4,$4,0,$2,current_user,'{}'
            )
            """,
            extraction.batch_id,
            extraction.owner_user_id,
            sha("batch-lock"),
            len(states),
        )
        await conn.executemany(
            """
            INSERT INTO memory.evidence_ingest_batch_row(
              owner_user_id, batch_id, evidence_id, external_id,
              content_sha256, operation
            ) VALUES($1,$2,$3,$4,$5,'inserted')
            """,
            [
                (
                    extraction.owner_user_id,
                    extraction.batch_id,
                    state.evidence_id,
                    f"apply-integration-{index}",
                    state.content_sha256,
                )
                for index, state in enumerate(states)
            ],
        )


async def target_counts(conn, owner) -> dict[str, int]:
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.preference_candidate) AS preference_candidates,
              (SELECT count(*) FROM memory.preference_candidate_evidence) AS preference_links,
              (SELECT count(*) FROM memory.project_space) AS project_spaces,
              (SELECT count(*) FROM memory.project_space_registration_event) AS registration_events,
              (SELECT count(*) FROM memory.project_knowledge_candidate) AS project_candidates,
              (SELECT count(*) FROM memory.project_knowledge_candidate_evidence) AS project_links,
              (SELECT count(*) FROM memory.preference_candidate_review) AS preference_reviews,
              (SELECT count(*) FROM memory.project_knowledge_candidate_review) AS project_reviews,
              (SELECT count(*) FROM memory.user_preference) AS durable_preferences,
              (SELECT count(*) FROM memory.preference_revision) AS preference_revisions,
              (SELECT count(*) FROM memory.project_knowledge_head) AS durable_project_heads,
              (SELECT count(*) FROM memory.project_knowledge_revision) AS project_revisions
            """
        )
    return {key: int(value) for key, value in dict(row).items()}


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    database_name = f"memoryv1_candidate_apply_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn, database="postgres")
    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}" TEMPLATE memory')
        conn = await asyncpg.connect(dsn, database=database_name)
        with tempfile.TemporaryDirectory(prefix="memory-v1-preference-project-apply-") as temp:
            extraction, states, report_path, authorization_path = build_fixture(Path(temp))
            await seed_evidence_batch(conn, extraction, states)
            preflight = await run_controlled_preference_project_apply(
                conn,
                manifest_path=authorization_path,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=report_path,
            )
            if preflight["status"] != "preflight_verified" or not preflight["ready_for_apply"]:
                raise AssertionError("authorized candidate preflight failed")
            try:
                await run_controlled_preference_project_apply(
                    conn,
                    manifest_path=authorization_path,
                    extraction_manifest_path=EXTRACTION_MANIFEST,
                    extraction_report_path=report_path,
                    apply=True,
                    confirmation="WRONG",
                )
            except PreferenceProjectApplyError:
                pass
            else:
                raise AssertionError("candidate apply accepted the wrong confirmation")
            committed = await run_controlled_preference_project_apply(
                conn,
                manifest_path=authorization_path,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=report_path,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if committed["status"] != "committed" or committed["database_writes"] != 18:
                raise AssertionError("candidate-only transaction did not commit exactly 18 rows")
            counts_after_commit = await target_counts(conn, extraction.owner_user_id)
            expected = {
                "preference_candidates": 3,
                "preference_links": 3,
                "project_spaces": 1,
                "registration_events": 1,
                "project_candidates": 5,
                "project_links": 5,
                "preference_reviews": 0,
                "project_reviews": 0,
                "durable_preferences": 0,
                "preference_revisions": 0,
                "durable_project_heads": 0,
                "project_revisions": 0,
            }
            if counts_after_commit != expected:
                raise AssertionError(f"candidate-only counts changed: {counts_after_commit}")
            replay = await run_controlled_preference_project_apply(
                conn,
                manifest_path=authorization_path,
                extraction_manifest_path=EXTRACTION_MANIFEST,
                extraction_report_path=report_path,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if replay["status"] != "verified_replay" or replay["database_writes"] != 0:
                raise AssertionError("candidate replay was not exact and zero-write")
            if await target_counts(conn, extraction.owner_user_id) != counts_after_commit:
                raise AssertionError("candidate replay changed database counts")
            other_owner = "22222222-2222-4222-8222-222222222222"
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL ROLE brains_app")
                await conn.execute("SELECT set_config('app.user_id',$1,true)", other_owner)
                leaked = await conn.fetchval(
                    "SELECT count(*) FROM memory.preference_candidate WHERE owner_user_id=$1",
                    extraction.owner_user_id,
                )
            if leaked != 0:
                raise AssertionError("cross-owner candidate rows were visible")
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
    print("memory_v1_preference_project_apply_integration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
