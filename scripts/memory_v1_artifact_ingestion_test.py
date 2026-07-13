#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

import asyncpg

from rag_engine.memory_v1_artifacts import (
    ArtifactManifestEntry,
    ArtifactSourceConflict,
    ArtifactSourceRow,
    artifact_dry_run_report,
    build_artifact_plan,
    persist_artifact_plan,
)


ACTOR_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTOR_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
SOURCE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_fixture() -> tuple[ArtifactManifestEntry, ArtifactSourceRow]:
    text = (
        "I want this outline retained as a reference.\n\n"
        "# Memory Plan\n\n"
        "Preserve sources before extracting claims.\n\n"
        "## Security\n\n"
        "Every row must remain owner-scoped.\n\n"
        "## Retrieval\n\n"
        "Do not inject unreviewed sections."
    )
    entry = ArtifactManifestEntry.from_mapping(
        {
            "source_id": str(SOURCE_ID),
            "expected_sha256": sha256_text(text),
            "title": "Synthetic Memory Plan",
            "artifact_kind": "technical_design_proposal",
            "authorship": "mixed",
            "body_marker": "# Memory Plan",
            "body_authorship": "assistant",
            "endorsement_level": "reference",
            "endorsement_explicit": True,
            "endorsement_rationale": "Explicitly retained as reference, not policy.",
            "document_state": "proposal",
            "extraction_policy": "review_only",
            "sensitivity": "medium",
        },
        owner_user_id=ACTOR_A,
        manifest_version="artifact_integration_fixture_v1",
        source_system="public.chat_log",
    )
    source = ArtifactSourceRow(
        source_id=SOURCE_ID,
        owner_user_id=ACTOR_A,
        source="frontend/chat:user",
        text=text,
        created_at=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        thread_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        vantage_id="RESSE",
        request_id="artifact-integration-request",
    )
    return entry, source


async def expect_error(exc_type, awaitable, label: str) -> None:
    try:
        await awaitable
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}: {label}")


async def main() -> int:
    entry, source = build_fixture()
    plan = build_artifact_plan(entry, source)
    if len(plan.sections) != 4:
        raise AssertionError(f"expected 4 deterministic sections, got {len(plan.sections)}")
    if plan.sections[0].authorship != "user":
        raise AssertionError("submission wrapper was not attributed to the user")
    if any(section.authorship != "assistant" for section in plan.sections[1:]):
        raise AssertionError("document body sections were not attributed to the assistant")
    if plan.sections[2].parent_section_id != plan.sections[1].section_id:
        raise AssertionError("Markdown heading hierarchy was not preserved")
    if plan.sections[3].parent_section_id != plan.sections[1].section_id:
        raise AssertionError("second child heading hierarchy was not preserved")

    report = artifact_dry_run_report([plan])
    if report["mode"] != "dry_run_no_writes":
        raise AssertionError("report mode is not dry-run")
    if report["all_sections_retrieval_eligible"]:
        raise AssertionError("dry-run enabled section retrieval")
    if report["all_sections_promotion_eligible"]:
        raise AssertionError("dry-run enabled section promotion")
    if "content" in report["artifacts"][0]["sections"][0]:
        raise AssertionError("dry-run report exposed full section content")

    bad_entry = ArtifactManifestEntry.from_mapping(
        {
            "source_id": str(SOURCE_ID),
            "expected_sha256": "0" * 64,
            "title": "Bad Hash",
            "artifact_kind": "technical_design_proposal",
            "authorship": "mixed",
            "body_marker": "# Memory Plan",
            "body_authorship": "assistant",
            "endorsement_level": "reference",
            "endorsement_explicit": False,
            "endorsement_rationale": "Hash mismatch fixture.",
            "document_state": "proposal",
            "extraction_policy": "review_only",
            "sensitivity": "medium",
        },
        owner_user_id=ACTOR_A,
        manifest_version="artifact_integration_fixture_v1",
        source_system="public.chat_log",
    )
    try:
        build_artifact_plan(bad_entry, source)
    except ArtifactSourceConflict:
        pass
    else:
        raise AssertionError("hash mismatch did not fail closed")

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn)
    outer = conn.transaction()
    await outer.start()
    try:
        await conn.execute("SET LOCAL ROLE brains_app")
        first = await persist_artifact_plan(conn, ACTOR_A, plan)
        second = await persist_artifact_plan(conn, ACTOR_A, plan)
        if first["inserted"] != {
            "artifact": 1,
            "occurrence": 1,
            "sections": 4,
            "endorsement": 1,
        }:
            raise AssertionError(f"unexpected first insert counts: {first['inserted']}")
        if second["inserted"] != {
            "artifact": 0,
            "occurrence": 0,
            "sections": 0,
            "endorsement": 0,
        }:
            raise AssertionError(f"idempotent replay inserted rows: {second['inserted']}")

        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(ACTOR_A))
        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.evidence) AS evidence_count,
              (SELECT count(*) FROM memory.artifact) AS artifact_count,
              (SELECT count(*) FROM memory.artifact_occurrence) AS occurrence_count,
              (SELECT count(*) FROM memory.artifact_section) AS section_count,
              (SELECT count(*) FROM memory.artifact_endorsement) AS endorsement_count,
              (SELECT count(*) FROM memory.artifact_section
               WHERE retrieval_eligible OR promotion_eligible) AS enabled_section_count
            """
        )
        expected_counts = {
            "evidence_count": 1,
            "artifact_count": 1,
            "occurrence_count": 1,
            "section_count": 4,
            "endorsement_count": 1,
            "enabled_section_count": 0,
        }
        if dict(counts) != expected_counts:
            raise AssertionError(f"unexpected persisted counts: {dict(counts)}")

        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(ACTOR_B))
        if await conn.fetchval("SELECT count(*) FROM memory.artifact") != 0:
            raise AssertionError("actor B can see actor A artifact")
        if await conn.fetchval("SELECT count(*) FROM memory.artifact_section") != 0:
            raise AssertionError("actor B can see actor A artifact sections")
        await expect_error(
            ArtifactSourceConflict,
            persist_artifact_plan(conn, ACTOR_B, plan),
            "cross-owner artifact persistence",
        )

        print("memory_v1_artifact_ingestion: PASS")
        return 0
    finally:
        await outer.rollback()
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
