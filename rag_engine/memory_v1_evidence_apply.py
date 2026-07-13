from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_artifacts import ArtifactManifestEntry, load_artifact_manifest
from .memory_v1_atomic_spans import build_atomic_span_plans
from .memory_v1_compound_spans import resolve_compound_plans
from .memory_v1_evidence_persistence import (
    PLAN_VERSION,
    SOURCE_SYSTEM,
    ExistingEvidenceRow,
    EvidencePersistenceDecision,
    EvidenceSpanPlan,
    build_evidence_span_plans,
    build_persistence_decisions,
    evidence_persistence_dry_run_report,
    evidence_plan_fingerprint,
)
from .memory_v1_evidence_triage import (
    SOURCE_ROLE,
    ExistingEvidence,
    TriageSourceRow,
    triage_sources,
)
from .memory_v1_store import actor_uuid


MANIFEST_VERSION = "memory_v1_evidence_apply_authorization_20260713_v1"
CONFIRMATION = "APPLY_REVIEWED_164_EVIDENCE_ROWS"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CONTRACT = {
    "allowed_writes": [
        "memory.evidence:INSERT",
        "memory.evidence_ingest_batch:INSERT",
        "memory.evidence_ingest_batch_row:INSERT",
    ],
    "candidate_writes": 0,
    "claim_writes": 0,
    "evidence_updates": 0,
    "evidence_deletes": 0,
    "preference_writes": 0,
    "projection_outbox_writes": 0,
    "qdrant_writes": 0,
    "prompt_injection": False,
}


class EvidenceApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceApplyManifest:
    path: Path
    sha256: str
    manifest_version: str
    batch_id: uuid.UUID
    batch_key: str
    owner_user_id: uuid.UUID
    plan_version: str
    apply_authorized: bool
    reviewed_report_sha256: str
    reviewed_input_fingerprint_sha256: str
    artifact_manifest_sha256: str
    source_last_created_at: datetime
    source_last_id: uuid.UUID
    source_row_count: int
    source_snapshot_sha256: str
    expected_evidence_count: int
    expected_atomic_review_span_count: int
    expected_compound_review_span_count: int
    controls: Dict[str, Any]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _required_sha(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(text):
        raise EvidenceApplyError(f"{field} must be lowercase SHA-256 hex")
    return text


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceApplyError(f"{field} is required")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EvidenceApplyError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceApplyError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise EvidenceApplyError(f"{field} must be a positive integer")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EvidenceApplyError(f"{field} must be a nonnegative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceApplyError(f"{field} must be a nonnegative integer") from exc
    if parsed < 0:
        raise EvidenceApplyError(f"{field} must be a nonnegative integer")
    return parsed


def _timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EvidenceApplyError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceApplyError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise EvidenceApplyError("source timestamp is timezone-naive")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def load_evidence_apply_manifest(path: str | Path) -> EvidenceApplyManifest:
    manifest_path = Path(path).resolve()
    raw_bytes = manifest_path.read_bytes()
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise EvidenceApplyError("authorization manifest is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise EvidenceApplyError("authorization manifest root must be an object")

    manifest_version = _required_text(raw.get("manifest_version"), "manifest_version")
    if manifest_version != MANIFEST_VERSION:
        raise EvidenceApplyError(f"unsupported manifest_version: {manifest_version}")
    plan_version = _required_text(raw.get("plan_version"), "plan_version")
    if plan_version != PLAN_VERSION:
        raise EvidenceApplyError(f"unsupported plan_version: {plan_version}")
    controls = raw.get("controls")
    if controls != CONTROL_CONTRACT:
        raise EvidenceApplyError("authorization manifest control contract changed")
    if not isinstance(raw.get("apply_authorized"), bool):
        raise EvidenceApplyError("apply_authorized must be boolean")

    try:
        batch_id = uuid.UUID(str(raw.get("batch_id")))
        source_last_id = uuid.UUID(
            str(raw.get("source_snapshot", {}).get("last_source_id"))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceApplyError("batch_id and source last_source_id must be UUIDs") from exc
    if batch_id.int == 0 or source_last_id.int == 0:
        raise EvidenceApplyError("batch_id and source last_source_id must be non-nil")

    source = raw.get("source_snapshot")
    expected = raw.get("expected")
    if not isinstance(source, dict) or not isinstance(expected, dict):
        raise EvidenceApplyError("source_snapshot and expected must be objects")

    return EvidenceApplyManifest(
        path=manifest_path,
        sha256=_sha256_bytes(raw_bytes),
        manifest_version=manifest_version,
        batch_id=batch_id,
        batch_key=_required_text(raw.get("batch_key"), "batch_key"),
        owner_user_id=actor_uuid(raw.get("owner_user_id")),
        plan_version=plan_version,
        apply_authorized=raw["apply_authorized"],
        reviewed_report_sha256=_required_sha(
            raw.get("reviewed_report_sha256"), "reviewed_report_sha256"
        ),
        reviewed_input_fingerprint_sha256=_required_sha(
            raw.get("reviewed_input_fingerprint_sha256"),
            "reviewed_input_fingerprint_sha256",
        ),
        artifact_manifest_sha256=_required_sha(
            raw.get("artifact_manifest_sha256"), "artifact_manifest_sha256"
        ),
        source_last_created_at=_timestamp(
            source.get("last_created_at"), "source_snapshot.last_created_at"
        ),
        source_last_id=source_last_id,
        source_row_count=_positive_int(
            source.get("row_count"), "source_snapshot.row_count"
        ),
        source_snapshot_sha256=_required_sha(
            source.get("sha256"), "source_snapshot.sha256"
        ),
        expected_evidence_count=_positive_int(
            expected.get("evidence_rows"), "expected.evidence_rows"
        ),
        expected_atomic_review_span_count=_nonnegative_int(
            expected.get("atomic_review_spans"), "expected.atomic_review_spans"
        ),
        expected_compound_review_span_count=_nonnegative_int(
            expected.get("compound_review_spans"), "expected.compound_review_spans"
        ),
        controls=dict(controls),
    )


def load_reviewed_report(path: str | Path, manifest: EvidenceApplyManifest) -> Dict[str, Any]:
    report_path = Path(path).resolve()
    raw_bytes = report_path.read_bytes()
    actual_sha = _sha256_bytes(raw_bytes)
    if actual_sha != manifest.reviewed_report_sha256:
        raise EvidenceApplyError(
            "reviewed report hash mismatch: "
            f"expected {manifest.reviewed_report_sha256}, got {actual_sha}"
        )
    try:
        report = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise EvidenceApplyError("reviewed report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise EvidenceApplyError("reviewed report root must be an object")
    if report.get("owner_user_id") != str(manifest.owner_user_id):
        raise EvidenceApplyError("reviewed report owner does not match manifest")
    if report.get("plan_version") != manifest.plan_version:
        raise EvidenceApplyError("reviewed report plan version does not match manifest")
    if report.get("input_fingerprint_sha256") != manifest.reviewed_input_fingerprint_sha256:
        raise EvidenceApplyError("reviewed report fingerprint does not match manifest")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise EvidenceApplyError("reviewed report summary is missing")
    expected_counts = {
        "planned_row_count": manifest.expected_evidence_count,
        "atomic_review_span_count": manifest.expected_atomic_review_span_count,
        "compound_review_span_count": manifest.expected_compound_review_span_count,
    }
    for field, expected in expected_counts.items():
        if summary.get(field) != expected:
            raise EvidenceApplyError(f"reviewed report {field} changed")
    if summary.get("actions") != {"plan_insert": manifest.expected_evidence_count}:
        raise EvidenceApplyError("reviewed report is not a fresh insert-only plan")
    if summary.get("conflict_count") != 0 or report.get("ready_for_joint_review") is not True:
        raise EvidenceApplyError("reviewed report is not conflict-free")
    if report.get("batch_source_count") != manifest.source_row_count:
        raise EvidenceApplyError("reviewed report source count does not match manifest")
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) != manifest.expected_evidence_count:
        raise EvidenceApplyError("reviewed report row set is incomplete")
    return report


def _source_snapshot_sha256(sources: Sequence[TriageSourceRow]) -> str:
    locked = [
        {
            "created_at": _timestamp_text(source.created_at),
            "owner_user_id": str(source.owner_user_id),
            "request_id": source.request_id,
            "source": source.source,
            "source_id": str(source.source_id),
            "text_sha256": _sha256_text(source.text),
            "thread_id": str(source.thread_id) if source.thread_id else None,
            "vantage_id": source.vantage_id,
        }
        for source in sources
    ]
    return _sha256_text(_stable_json(locked))


async def _fetch_locked_sources(
    conn: asyncpg.Connection,
    manifest: EvidenceApplyManifest,
) -> list[TriageSourceRow]:
    rows = await conn.fetch(
        """
        SELECT id, user_id, source, text, created_at, thread_id, vantage_id, request_id
        FROM public.chat_log
        WHERE user_id=$1
          AND source=$2
          AND (created_at, id) <= ($3::timestamptz, $4::uuid)
        ORDER BY created_at, id
        """,
        str(manifest.owner_user_id),
        SOURCE_ROLE,
        manifest.source_last_created_at,
        manifest.source_last_id,
    )
    sources: list[TriageSourceRow] = []
    for row in rows:
        owner = actor_uuid(row["user_id"])
        if owner != manifest.owner_user_id:
            raise EvidenceApplyError("source query returned a cross-owner row")
        sources.append(
            TriageSourceRow(
                source_id=uuid.UUID(str(row["id"])),
                owner_user_id=owner,
                source=str(row["source"]),
                text=str(row["text"] or ""),
                created_at=row["created_at"],
                thread_id=uuid.UUID(str(row["thread_id"])) if row["thread_id"] else None,
                vantage_id=str(row["vantage_id"]) if row["vantage_id"] else None,
                request_id=str(row["request_id"]) if row["request_id"] else None,
            )
        )
    if len(sources) != manifest.source_row_count:
        raise EvidenceApplyError(
            f"source cohort count changed: expected {manifest.source_row_count}, got {len(sources)}"
        )
    if not sources:
        raise EvidenceApplyError("source cohort is empty")
    last = sources[-1]
    if (last.created_at, last.source_id) != (
        manifest.source_last_created_at,
        manifest.source_last_id,
    ):
        raise EvidenceApplyError("source cohort terminal cursor changed")
    actual_sha = _source_snapshot_sha256(sources)
    if actual_sha != manifest.source_snapshot_sha256:
        raise EvidenceApplyError(
            "source cohort hash changed: "
            f"expected {manifest.source_snapshot_sha256}, got {actual_sha}"
        )
    return sources


def _load_artifact_entries(
    path: str | Path,
    manifest: EvidenceApplyManifest,
) -> Dict[uuid.UUID, ArtifactManifestEntry]:
    artifact_path = Path(path).resolve()
    actual_sha = _sha256_bytes(artifact_path.read_bytes())
    if actual_sha != manifest.artifact_manifest_sha256:
        raise EvidenceApplyError("artifact manifest hash changed")
    entries = load_artifact_manifest(artifact_path)
    owner_entries = {
        entry.source_id: entry
        for entry in entries
        if entry.owner_user_id == manifest.owner_user_id
    }
    if len(owner_entries) != len(entries):
        raise EvidenceApplyError("artifact manifest contains a different owner")
    return owner_entries


async def _fetch_source_evidence(
    conn: asyncpg.Connection,
    manifest: EvidenceApplyManifest,
    sources: Sequence[TriageSourceRow],
) -> Dict[uuid.UUID, ExistingEvidence]:
    source_ids = [source.source_id for source in sources]
    rows = await conn.fetch(
        """
        SELECT evidence_id, external_id, content_sha256, status::text
        FROM memory.evidence
        WHERE owner_user_id=$1
          AND source_system=$2
          AND external_id = ANY($3::text[])
        """,
        manifest.owner_user_id,
        SOURCE_SYSTEM,
        [str(source_id) for source_id in source_ids],
    )
    existing: Dict[uuid.UUID, ExistingEvidence] = {}
    for row in rows:
        try:
            source_id = uuid.UUID(str(row["external_id"]))
        except ValueError as exc:
            raise EvidenceApplyError("source evidence external_id is not a UUID") from exc
        if source_id not in source_ids or source_id in existing:
            raise EvidenceApplyError("source evidence identity is unexpected or duplicated")
        existing[source_id] = ExistingEvidence(
            evidence_id=uuid.UUID(str(row["evidence_id"])),
            content_sha256=str(row["content_sha256"]) if row["content_sha256"] else None,
            status=str(row["status"]),
        )
    return existing


def _json_object(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceApplyError("stored evidence metadata is not an object")
    return dict(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


async def _fetch_planned_evidence(
    conn: asyncpg.Connection,
    manifest: EvidenceApplyManifest,
    plans: Sequence[EvidenceSpanPlan],
) -> list[ExistingEvidenceRow]:
    identities = {plan.external_id for plan in plans}
    for plan in plans:
        identities.add(str(plan.source_id))
        identities.add(f"chat_log:{plan.source_id}")
    rows = await conn.fetch(
        """
        SELECT evidence_id, owner_user_id, kind::text, source_system,
               external_id, content, content_sha256, observed_at,
               directness, source_reliability, independence_key,
               sensitivity::text, status::text, metadata
        FROM memory.evidence
        WHERE owner_user_id=$1
          AND source_system=$2
          AND external_id = ANY($3::text[])
        ORDER BY external_id, evidence_id
        """,
        manifest.owner_user_id,
        SOURCE_SYSTEM,
        sorted(identities),
    )
    result: list[ExistingEvidenceRow] = []
    for row in rows:
        owner = actor_uuid(row["owner_user_id"])
        if owner != manifest.owner_user_id:
            raise EvidenceApplyError("evidence query returned a cross-owner row")
        result.append(
            ExistingEvidenceRow(
                evidence_id=uuid.UUID(str(row["evidence_id"])),
                owner_user_id=owner,
                kind=str(row["kind"]),
                source_system=str(row["source_system"]),
                external_id=str(row["external_id"]),
                content=str(row["content"]) if row["content"] is not None else None,
                content_sha256=(
                    str(row["content_sha256"]) if row["content_sha256"] is not None else None
                ),
                observed_at=row["observed_at"],
                directness=_optional_float(row["directness"]),
                source_reliability=_optional_float(row["source_reliability"]),
                independence_key=(
                    str(row["independence_key"])
                    if row["independence_key"] is not None
                    else None
                ),
                sensitivity=str(row["sensitivity"]),
                status=str(row["status"]),
                metadata=_json_object(row["metadata"]),
            )
        )
    return result


def _build_plans(
    manifest: EvidenceApplyManifest,
    sources: Sequence[TriageSourceRow],
    artifact_entries: Mapping[uuid.UUID, ArtifactManifestEntry],
    existing_source_evidence: Mapping[uuid.UUID, ExistingEvidence],
) -> tuple[list[EvidenceSpanPlan], int, int]:
    triage = triage_sources(
        sources,
        artifact_entries=artifact_entries,
        existing_evidence=existing_source_evidence,
    )
    atomic = build_atomic_span_plans(triage)
    compound = resolve_compound_plans(atomic)
    plans = build_evidence_span_plans(
        atomic,
        compound,
        owner_user_id=manifest.owner_user_id,
    )
    atomic_count = sum(plan.span_origin == "atomic" for plan in plans)
    compound_count = sum(plan.span_origin == "compound_child" for plan in plans)
    if len(plans) != manifest.expected_evidence_count:
        raise EvidenceApplyError("planned evidence count changed")
    if atomic_count != manifest.expected_atomic_review_span_count:
        raise EvidenceApplyError("atomic reviewed span count changed")
    if compound_count != manifest.expected_compound_review_span_count:
        raise EvidenceApplyError("compound reviewed span count changed")
    fingerprint = evidence_plan_fingerprint(plans)
    if fingerprint != manifest.reviewed_input_fingerprint_sha256:
        raise EvidenceApplyError(
            "evidence plan fingerprint changed: "
            f"expected {manifest.reviewed_input_fingerprint_sha256}, got {fingerprint}"
        )
    return plans, atomic_count, compound_count


def _verify_fresh_review(
    manifest: EvidenceApplyManifest,
    reviewed_report: Mapping[str, Any],
    decisions: Sequence[EvidencePersistenceDecision],
    atomic_count: int,
    compound_count: int,
) -> None:
    if any(decision.action != "plan_insert" for decision in decisions):
        actions = sorted({decision.action for decision in decisions})
        raise EvidenceApplyError(f"fresh batch is not entirely plan_insert: {actions}")
    current = evidence_persistence_dry_run_report(
        decisions,
        owner_user_id=manifest.owner_user_id,
        atomic_review_span_count=atomic_count,
        compound_review_span_count=compound_count,
    )
    if current["summary"] != reviewed_report.get("summary"):
        raise EvidenceApplyError("current plan summary differs from reviewed report")
    if current["rows"] != reviewed_report.get("rows"):
        raise EvidenceApplyError("current plan rows differ from reviewed report")


async def _audit_schema_controls(conn: asyncpg.Connection) -> Dict[str, bool]:
    schema_ready = bool(
        await conn.fetchval(
            """
            SELECT to_regclass('memory.evidence_ingest_batch') IS NOT NULL
               AND to_regclass('memory.evidence_ingest_batch_row') IS NOT NULL
            """
        )
    )
    controls = {
        "audit_schema_ready": schema_ready,
        "effective_role_brains_app": await conn.fetchval("SELECT current_user = 'brains_app'"),
        "evidence_select_insert_only": await conn.fetchval(
            """
            SELECT has_table_privilege(current_user, 'memory.evidence', 'SELECT')
               AND has_table_privilege(current_user, 'memory.evidence', 'INSERT')
               AND NOT has_table_privilege(current_user, 'memory.evidence', 'UPDATE')
               AND NOT has_table_privilege(current_user, 'memory.evidence', 'DELETE')
            """
        ),
        "evidence_insert_guard_live": await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1 FROM pg_trigger
              WHERE tgrelid='memory.evidence'::regclass
                AND tgname='evidence_insert_only_guard'
                AND NOT tgisinternal AND tgenabled <> 'D'
            )
            """
        ),
        "candidate_active_evidence_guard_live": await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1 FROM pg_trigger
              WHERE tgrelid='memory.candidate'::regclass
                AND tgname='candidate_active_evidence_guard'
                AND NOT tgisinternal AND tgenabled <> 'D'
            )
            """
        ),
    }
    if schema_ready:
        controls["batch_select_insert_only"] = await conn.fetchval(
            """
            SELECT has_table_privilege(current_user, 'memory.evidence_ingest_batch', 'SELECT')
               AND has_table_privilege(current_user, 'memory.evidence_ingest_batch', 'INSERT')
               AND NOT has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch', 'UPDATE'
               )
               AND NOT has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch', 'DELETE'
               )
               AND has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch_row', 'SELECT'
               )
               AND has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch_row', 'INSERT'
               )
               AND NOT has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch_row', 'UPDATE'
               )
               AND NOT has_table_privilege(
                 current_user, 'memory.evidence_ingest_batch_row', 'DELETE'
               )
            """
        )
        controls["batch_forced_rls"] = await conn.fetchval(
            """
            SELECT count(*) = 2
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory'
              AND c.relname IN ('evidence_ingest_batch', 'evidence_ingest_batch_row')
              AND c.relrowsecurity AND c.relforcerowsecurity
            """
        )
        controls["batch_append_guards_live"] = await conn.fetchval(
            """
            SELECT count(*) = 2
            FROM pg_trigger
            WHERE tgrelid IN (
              'memory.evidence_ingest_batch'::regclass,
              'memory.evidence_ingest_batch_row'::regclass
            )
              AND tgname IN (
                'evidence_ingest_batch_append_only_guard',
                'evidence_ingest_batch_row_append_only_guard'
              )
              AND NOT tgisinternal AND tgenabled <> 'D'
            """
        )
    else:
        controls.update(
            {
                "batch_select_insert_only": False,
                "batch_forced_rls": False,
                "batch_append_guards_live": False,
            }
        )
    return {key: bool(value) for key, value in controls.items()}


async def _fetch_batch(conn: asyncpg.Connection, manifest: EvidenceApplyManifest):
    return await conn.fetchrow(
        """
        SELECT batch_id, owner_user_id, batch_key, manifest_version, plan_version,
               input_fingerprint_sha256, reviewed_report_sha256,
               authorization_manifest_sha256, source_snapshot_sha256,
               source_row_count, expected_evidence_count, inserted_count,
               reused_count, status, actor_user_id, invoked_by_role::text
        FROM memory.evidence_ingest_batch
        WHERE owner_user_id=$1 AND batch_id=$2
        """,
        manifest.owner_user_id,
        manifest.batch_id,
    )


async def _verify_replay(
    conn: asyncpg.Connection,
    manifest: EvidenceApplyManifest,
    plans: Sequence[EvidenceSpanPlan],
    decisions: Sequence[EvidencePersistenceDecision],
    batch,
) -> Dict[str, Any]:
    expected_header = {
        "batch_id": manifest.batch_id,
        "owner_user_id": manifest.owner_user_id,
        "batch_key": manifest.batch_key,
        "manifest_version": manifest.manifest_version,
        "plan_version": manifest.plan_version,
        "input_fingerprint_sha256": manifest.reviewed_input_fingerprint_sha256,
        "reviewed_report_sha256": manifest.reviewed_report_sha256,
        "authorization_manifest_sha256": manifest.sha256,
        "source_snapshot_sha256": manifest.source_snapshot_sha256,
        "source_row_count": manifest.source_row_count,
        "expected_evidence_count": manifest.expected_evidence_count,
        "inserted_count": manifest.expected_evidence_count,
        "reused_count": 0,
        "status": "committed",
        "actor_user_id": manifest.owner_user_id,
        "invoked_by_role": "brains_app",
    }
    actual_header = dict(batch)
    if actual_header != expected_header:
        raise EvidenceApplyError("stored batch header differs from authorization manifest")
    if any(decision.action != "reuse_exact" for decision in decisions):
        raise EvidenceApplyError("replay evidence is missing or differs from the plan")
    rows = await conn.fetch(
        """
        SELECT evidence_id, external_id, content_sha256, operation
        FROM memory.evidence_ingest_batch_row
        WHERE owner_user_id=$1 AND batch_id=$2
        ORDER BY evidence_id
        """,
        manifest.owner_user_id,
        manifest.batch_id,
    )
    expected_rows = sorted(
        (
            str(plan.evidence_id),
            plan.external_id,
            plan.content_sha256,
            "inserted",
        )
        for plan in plans
    )
    actual_rows = sorted(
        (
            str(row["evidence_id"]),
            str(row["external_id"]),
            str(row["content_sha256"]),
            str(row["operation"]),
        )
        for row in rows
    )
    if actual_rows != expected_rows:
        raise EvidenceApplyError("stored batch row ledger differs from evidence plan")
    return {
        "status": "verified_replay",
        "batch_id": str(manifest.batch_id),
        "evidence_rows": len(plans),
        "database_writes": 0,
    }


async def _owner_counts(conn: asyncpg.Connection, owner: uuid.UUID) -> Dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM memory.evidence WHERE owner_user_id=$1) AS evidence,
          (SELECT count(*) FROM memory.candidate WHERE owner_user_id=$1) AS candidates,
          (SELECT count(*) FROM memory.claim WHERE owner_user_id=$1) AS claims,
          (SELECT count(*) FROM memory.user_preference WHERE owner_user_id=$1) AS preferences,
          (SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=$1) AS outbox,
          (SELECT count(*) FROM memory.evidence_lifecycle_event
           WHERE owner_user_id=$1) AS lifecycle_events
        """,
        owner,
    )
    return {key: int(value) for key, value in dict(row).items()}


def _insert_payload(plans: Sequence[EvidenceSpanPlan]) -> str:
    return _stable_json(
        [
            {
                "evidence_id": str(plan.evidence_id),
                "owner_user_id": str(plan.owner_user_id),
                "kind": plan.kind,
                "source_system": plan.source_system,
                "external_id": plan.external_id,
                "content": plan.content,
                "content_sha256": plan.content_sha256,
                "observed_at": plan.observed_at.isoformat(),
                "directness": plan.directness,
                "source_reliability": plan.source_reliability,
                "independence_key": plan.independence_key,
                "sensitivity": plan.sensitivity,
                "status": plan.status,
                "metadata": plan.metadata,
            }
            for plan in plans
        ]
    )


async def _insert_evidence(
    conn: asyncpg.Connection,
    plans: Sequence[EvidenceSpanPlan],
) -> set[uuid.UUID]:
    rows = await conn.fetch(
        """
        INSERT INTO memory.evidence(
          evidence_id, owner_user_id, kind, source_system, external_id,
          content, content_sha256, observed_at, directness, source_reliability,
          independence_key, sensitivity, status, metadata
        )
        SELECT p.evidence_id, p.owner_user_id, p.kind::memory.evidence_kind,
               p.source_system, p.external_id, p.content, p.content_sha256,
               p.observed_at, p.directness, p.source_reliability,
               p.independence_key, p.sensitivity::memory.sensitivity_level,
               p.status::memory.record_status, p.metadata
        FROM jsonb_to_recordset($1::jsonb) AS p(
          evidence_id uuid, owner_user_id uuid, kind text, source_system text,
          external_id text, content text, content_sha256 text,
          observed_at timestamptz, directness numeric, source_reliability numeric,
          independence_key text, sensitivity text, status text, metadata jsonb
        )
        ON CONFLICT DO NOTHING
        RETURNING evidence_id
        """,
        _insert_payload(plans),
    )
    return {uuid.UUID(str(row["evidence_id"])) for row in rows}


async def _insert_batch_ledger(
    conn: asyncpg.Connection,
    manifest: EvidenceApplyManifest,
    plans: Sequence[EvidenceSpanPlan],
) -> None:
    metadata = {
        "controls": CONTROL_CONTRACT,
        "reviewed_report_verified": True,
        "source_snapshot_verified": True,
        "transaction_isolation": "repeatable_read",
    }
    await conn.execute(
        """
        INSERT INTO memory.evidence_ingest_batch(
          batch_id, owner_user_id, batch_key, manifest_version, plan_version,
          input_fingerprint_sha256, reviewed_report_sha256,
          authorization_manifest_sha256, source_snapshot_sha256,
          source_row_count, expected_evidence_count, inserted_count, reused_count,
          actor_user_id, invoked_by_role, metadata
        ) VALUES (
          $1, $2, $3, $4, $5, $6, $7, $8, $9,
          $10, $11, $11, 0, $2, current_user, $12::jsonb
        )
        """,
        manifest.batch_id,
        manifest.owner_user_id,
        manifest.batch_key,
        manifest.manifest_version,
        manifest.plan_version,
        manifest.reviewed_input_fingerprint_sha256,
        manifest.reviewed_report_sha256,
        manifest.sha256,
        manifest.source_snapshot_sha256,
        manifest.source_row_count,
        manifest.expected_evidence_count,
        _stable_json(metadata),
    )
    row_payload = _stable_json(
        [
            {
                "owner_user_id": str(manifest.owner_user_id),
                "batch_id": str(manifest.batch_id),
                "evidence_id": str(plan.evidence_id),
                "external_id": plan.external_id,
                "content_sha256": plan.content_sha256,
                "operation": "inserted",
            }
            for plan in plans
        ]
    )
    await conn.execute(
        """
        INSERT INTO memory.evidence_ingest_batch_row(
          owner_user_id, batch_id, evidence_id, external_id,
          content_sha256, operation
        )
        SELECT p.owner_user_id, p.batch_id, p.evidence_id, p.external_id,
               p.content_sha256, p.operation
        FROM jsonb_to_recordset($1::jsonb) AS p(
          owner_user_id uuid, batch_id uuid, evidence_id uuid,
          external_id text, content_sha256 text, operation text
        )
        """,
        row_payload,
    )


async def run_controlled_evidence_apply(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
    reviewed_report_path: str | Path,
    artifact_manifest_path: str | Path,
    apply: bool = False,
    confirmation: str = "",
) -> Dict[str, Any]:
    manifest = load_evidence_apply_manifest(manifest_path)
    reviewed_report = load_reviewed_report(reviewed_report_path, manifest)
    artifact_entries = _load_artifact_entries(artifact_manifest_path, manifest)
    if apply:
        if not manifest.apply_authorized:
            raise EvidenceApplyError("manifest has not authorized apply")
        if confirmation != CONFIRMATION:
            raise EvidenceApplyError(f"confirmation must equal {CONFIRMATION}")

    transaction = conn.transaction(isolation="repeatable_read", readonly=not apply)
    async with transaction:
        await conn.execute("SELECT set_config('lock_timeout', '5s', true)")
        await conn.execute("SELECT set_config('statement_timeout', '120s', true)")
        if apply:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{manifest.owner_user_id}:{manifest.batch_id}",
            )

        # public.chat_log is read under the trusted session role. All memory reads
        # and writes below run as brains_app with forced owner RLS.
        sources = await _fetch_locked_sources(conn, manifest)
        schema_ready = bool(
            await conn.fetchval(
                """
                SELECT to_regclass('memory.evidence_ingest_batch') IS NOT NULL
                   AND to_regclass('memory.evidence_ingest_batch_row') IS NOT NULL
                """
            )
        )
        await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(manifest.owner_user_id)
        )
        controls = await _audit_schema_controls(conn)
        if controls["audit_schema_ready"] != schema_ready:
            raise EvidenceApplyError("audit schema visibility changed after SET ROLE")
        required_base = (
            "effective_role_brains_app",
            "evidence_select_insert_only",
            "evidence_insert_guard_live",
            "candidate_active_evidence_guard_live",
        )
        if not all(controls[key] for key in required_base):
            raise EvidenceApplyError(f"database safety controls failed: {controls}")
        if apply and not all(controls.values()):
            raise EvidenceApplyError(f"apply audit controls failed: {controls}")

        source_evidence = await _fetch_source_evidence(conn, manifest, sources)
        plans, atomic_count, compound_count = _build_plans(
            manifest,
            sources,
            artifact_entries,
            source_evidence,
        )
        existing = await _fetch_planned_evidence(conn, manifest, plans)
        decisions = build_persistence_decisions(plans, existing)
        batch = await _fetch_batch(conn, manifest) if schema_ready else None

        if batch is not None:
            result = await _verify_replay(conn, manifest, plans, decisions, batch)
            result["controls"] = controls
            return result

        _verify_fresh_review(
            manifest,
            reviewed_report,
            decisions,
            atomic_count,
            compound_count,
        )
        if not apply:
            return {
                "status": "preflight_verified",
                "apply_authorized": manifest.apply_authorized,
                "ready_for_apply": manifest.apply_authorized and all(controls.values()),
                "batch_id": str(manifest.batch_id),
                "owner_user_id": str(manifest.owner_user_id),
                "source_rows": len(sources),
                "evidence_rows": len(plans),
                "input_fingerprint_sha256": evidence_plan_fingerprint(plans),
                "source_snapshot_sha256": _source_snapshot_sha256(sources),
                "authorization_manifest_sha256": manifest.sha256,
                "controls": controls,
                "database_writes": 0,
            }

        before = await _owner_counts(conn, manifest.owner_user_id)
        inserted_ids = await _insert_evidence(conn, plans)
        expected_ids = {plan.evidence_id for plan in plans}
        if inserted_ids != expected_ids:
            raise EvidenceApplyError(
                f"inserted evidence identity mismatch: expected {len(expected_ids)}, "
                f"got {len(inserted_ids)}"
            )
        stored = await _fetch_planned_evidence(conn, manifest, plans)
        stored_decisions = build_persistence_decisions(plans, stored)
        if any(decision.action != "reuse_exact" for decision in stored_decisions):
            raise EvidenceApplyError("post-insert immutable evidence verification failed")
        await _insert_batch_ledger(conn, manifest, plans)
        stored_batch = await _fetch_batch(conn, manifest)
        if stored_batch is None:
            raise EvidenceApplyError("batch audit header was not inserted")
        await _verify_replay(
            conn,
            manifest,
            plans,
            stored_decisions,
            stored_batch,
        )
        after = await _owner_counts(conn, manifest.owner_user_id)
        if after["evidence"] != before["evidence"] + manifest.expected_evidence_count:
            raise EvidenceApplyError("evidence count delta is not the authorized count")
        unchanged = ("candidates", "claims", "preferences", "outbox", "lifecycle_events")
        if any(after[key] != before[key] for key in unchanged):
            raise EvidenceApplyError(
                f"unauthorized downstream write detected: before={before}, after={after}"
            )
        return {
            "status": "committed",
            "batch_id": str(manifest.batch_id),
            "owner_user_id": str(manifest.owner_user_id),
            "evidence_rows_inserted": len(inserted_ids),
            "authorization_manifest_sha256": manifest.sha256,
            "source_snapshot_sha256": manifest.source_snapshot_sha256,
            "input_fingerprint_sha256": manifest.reviewed_input_fingerprint_sha256,
            "before": before,
            "after": after,
            "controls": controls,
        }
