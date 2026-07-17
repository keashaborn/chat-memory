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

from rag_engine.memory_v1_artifacts import load_artifact_manifest
from rag_engine.memory_v1_atomic_spans import build_atomic_span_plans
from rag_engine.memory_v1_compound_spans import resolve_compound_plans
from rag_engine.memory_v1_evidence_apply import (
    CONFIRMATION,
    CONTROL_CONTRACT,
    MANIFEST_VERSION,
    EvidenceApplyError,
    _source_snapshot_sha256,
    run_controlled_evidence_apply,
)
from rag_engine.memory_v1_evidence_persistence import (
    PLAN_VERSION,
    build_evidence_span_plans,
    build_persistence_decisions,
    evidence_persistence_dry_run_report,
    evidence_plan_fingerprint,
)
from rag_engine.memory_v1_evidence_triage import TriageSourceRow, triage_sources


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ARTIFACT_SOURCE_ID = uuid.UUID("71000000-0000-4000-8000-000000000001")
REVIEW_SOURCE_ID = uuid.UUID("71000000-0000-4000-8000-000000000002")
THREAD_ID = uuid.UUID("72000000-0000-4000-8000-000000000001")
ARTIFACT_TEXT = "Archived assistant-authored design outline."
REVIEW_TEXT = (
    "I went to the University of Wisconsin. "
    "I retired about five years ago. "
    "I believe concise answers are better."
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return sha256_bytes(raw)


def source_rows() -> list[TriageSourceRow]:
    return [
        TriageSourceRow(
            source_id=ARTIFACT_SOURCE_ID,
            owner_user_id=OWNER,
            source="frontend/chat:user",
            text=ARTIFACT_TEXT,
            created_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
            thread_id=THREAD_ID,
            vantage_id="RESSE",
            request_id="apply-integration-artifact",
        ),
        TriageSourceRow(
            source_id=REVIEW_SOURCE_ID,
            owner_user_id=OWNER,
            source="frontend/chat:user",
            text=REVIEW_TEXT,
            created_at=datetime(2026, 7, 13, 10, 1, tzinfo=timezone.utc),
            thread_id=THREAD_ID,
            vantage_id="RESSE",
            request_id="apply-integration-review",
        ),
    ]


def build_fixture_files(root: Path) -> tuple[Path, Path, Path, int, int, int]:
    artifact_path = root / "artifacts.json"
    artifact = {
        "manifest_version": "apply_integration_artifacts_v1",
        "owner_user_id": str(OWNER),
        "source_system": "public.chat_log",
        "entries": [
            {
                "source_id": str(ARTIFACT_SOURCE_ID),
                "expected_sha256": sha256_text(ARTIFACT_TEXT),
                "title": "Apply integration archived outline",
                "artifact_kind": "technical_design_proposal",
                "authorship": "assistant",
                "body_marker": None,
                "body_authorship": None,
                "endorsement_level": "reference",
                "endorsement_explicit": False,
                "endorsement_rationale": "Fixture archive exclusion.",
                "document_state": "proposal",
                "extraction_policy": "review_only",
                "sensitivity": "medium",
            }
        ],
    }
    artifact_sha = write_json(artifact_path, artifact)
    artifact_entries = {
        entry.source_id: entry for entry in load_artifact_manifest(artifact_path)
    }
    sources = source_rows()
    triage = triage_sources(
        sources,
        artifact_entries=artifact_entries,
        existing_evidence={},
    )
    atomic = build_atomic_span_plans(triage)
    compound = resolve_compound_plans(atomic)
    plans = build_evidence_span_plans(atomic, compound, owner_user_id=OWNER)
    if not plans:
        raise AssertionError("integration fixture produced no reviewed evidence")
    atomic_count = sum(plan.span_origin == "atomic" for plan in plans)
    compound_count = sum(plan.span_origin == "compound_child" for plan in plans)
    decisions = build_persistence_decisions(plans, [])
    report = evidence_persistence_dry_run_report(
        decisions,
        owner_user_id=OWNER,
        atomic_review_span_count=atomic_count,
        compound_review_span_count=compound_count,
    )
    report["batch_source_count"] = len(sources)
    report_path = root / "reviewed_report.json"
    report_sha = write_json(report_path, report)
    last = sources[-1]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "batch_id": "73000000-0000-4000-8000-000000000001",
        "batch_key": "apply-integration-reviewed-batch",
        "owner_user_id": str(OWNER),
        "plan_version": PLAN_VERSION,
        "apply_authorized": False,
        "reviewed_report_sha256": report_sha,
        "reviewed_input_fingerprint_sha256": evidence_plan_fingerprint(plans),
        "artifact_manifest_sha256": artifact_sha,
        "source_snapshot": {
            "last_created_at": last.created_at.isoformat(),
            "last_source_id": str(last.source_id),
            "row_count": len(sources),
            "sha256": _source_snapshot_sha256(sources),
        },
        "expected": {
            "evidence_rows": len(plans),
            "atomic_review_spans": atomic_count,
            "compound_review_spans": compound_count,
        },
        "controls": CONTROL_CONTRACT,
    }
    manifest_path = root / "authorization.json"
    write_json(manifest_path, manifest)
    return (
        manifest_path,
        report_path,
        artifact_path,
        len(plans),
        atomic_count,
        compound_count,
    )


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn)
    outer = conn.transaction(isolation="repeatable_read")
    await outer.start()
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.chat_log(
              id uuid PRIMARY KEY,
              user_id text NOT NULL,
              source text NOT NULL,
              text text,
              created_at timestamptz NOT NULL,
              thread_id uuid,
              vantage_id text,
              request_id text
            )
            """
        )
        await conn.executemany(
            """
            INSERT INTO public.chat_log(
              id, user_id, source, text, created_at,
              thread_id, vantage_id, request_id
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            [
                (
                    source.source_id,
                    str(source.owner_user_id),
                    source.source,
                    source.text,
                    source.created_at,
                    source.thread_id,
                    source.vantage_id,
                    source.request_id,
                )
                for source in source_rows()
            ],
        )
        with tempfile.TemporaryDirectory(prefix="memory-v1-evidence-apply-") as temp:
            root = Path(temp)
            manifest_path, report_path, artifact_path, evidence_count, _, _ = (
                build_fixture_files(root)
            )
            preflight = await run_controlled_evidence_apply(
                conn,
                manifest_path=manifest_path,
                reviewed_report_path=report_path,
                artifact_manifest_path=artifact_path,
            )
            if preflight["status"] != "preflight_verified":
                raise AssertionError("unauthorized preflight did not verify")
            if preflight["apply_authorized"] or preflight["ready_for_apply"]:
                raise AssertionError("unauthorized manifest became apply-ready")
            await conn.execute("RESET ROLE")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["apply_authorized"] = True
            write_json(manifest_path, manifest)

            try:
                await run_controlled_evidence_apply(
                    conn,
                    manifest_path=manifest_path,
                    reviewed_report_path=report_path,
                    artifact_manifest_path=artifact_path,
                    apply=True,
                    confirmation="WRONG",
                )
            except EvidenceApplyError:
                pass
            else:
                raise AssertionError("apply accepted the wrong confirmation")

            committed = await run_controlled_evidence_apply(
                conn,
                manifest_path=manifest_path,
                reviewed_report_path=report_path,
                artifact_manifest_path=artifact_path,
                apply=True,
                confirmation=CONFIRMATION,
            )
            if committed["status"] != "committed":
                raise AssertionError("controlled evidence batch did not commit")
            if committed["evidence_rows_inserted"] != evidence_count:
                raise AssertionError("controlled evidence count changed")
            await conn.execute("RESET ROLE")
            replay = await run_controlled_evidence_apply(
                conn,
                manifest_path=manifest_path,
                reviewed_report_path=report_path,
                artifact_manifest_path=artifact_path,
            )
            if replay["status"] != "verified_replay" or replay["database_writes"] != 0:
                raise AssertionError("batch replay was not read-only and exact")
            await conn.execute("RESET ROLE")

            await conn.execute(
                "UPDATE public.chat_log SET text=text || ' changed' WHERE id=$1",
                REVIEW_SOURCE_ID,
            )
            try:
                await run_controlled_evidence_apply(
                    conn,
                    manifest_path=manifest_path,
                    reviewed_report_path=report_path,
                    artifact_manifest_path=artifact_path,
                )
            except EvidenceApplyError as exc:
                if "source cohort hash changed" not in str(exc):
                    raise
            else:
                raise AssertionError("source cohort mutation did not fail closed")

        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.evidence
               WHERE owner_user_id=$1) AS evidence,
              (SELECT count(*) FROM memory.evidence_ingest_batch
               WHERE owner_user_id=$1) AS batches,
              (SELECT count(*) FROM memory.evidence_ingest_batch_row
               WHERE owner_user_id=$1) AS batch_rows,
              (SELECT count(*) FROM memory.candidate
               WHERE owner_user_id=$1) AS candidates,
              (SELECT count(*) FROM memory.claim
               WHERE owner_user_id=$1) AS claims,
              (SELECT count(*) FROM memory.user_preference
               WHERE owner_user_id=$1) AS preferences,
              (SELECT count(*) FROM memory.projection_outbox
               WHERE owner_user_id=$1) AS outbox
            """,
            OWNER,
        )
        expected = {
            "evidence": evidence_count,
            "batches": 1,
            "batch_rows": evidence_count,
            "candidates": 0,
            "claims": 0,
            "preferences": 0,
            "outbox": 0,
        }
        if dict(counts) != expected:
            raise AssertionError(f"controlled apply side effects changed: {dict(counts)}")
        print("memory_v1_evidence_apply_integration: PASS")
        return 0
    finally:
        await outer.rollback()
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
