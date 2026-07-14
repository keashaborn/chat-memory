from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import asyncpg

from .memory_v1_preference_project_extraction import (
    MANIFEST_VERSION as EXTRACTION_MANIFEST_VERSION,
    NEW_TABLES,
    REPORT_MODE,
    ExtractionManifest,
    load_extraction_manifest,
)


MANIFEST_VERSION = "memory_v1_preference_project_apply_authorization_20260713_v1"
CONFIRMATION = "APPLY_MEMORY_V1_8_PREFERENCE_PROJECT_CANDIDATES"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CONTRACT = {
    "allowed_writes": [
        "memory.project_space:INSERT_VIA_CONTROLLED_FUNCTION",
        "memory.project_space_registration_event:INSERT_VIA_CONTROLLED_FUNCTION",
        "memory.preference_candidate:INSERT",
        "memory.preference_candidate_evidence:INSERT",
        "memory.project_knowledge_candidate:INSERT",
        "memory.project_knowledge_candidate_evidence:INSERT",
    ],
    "candidate_rows": 8,
    "candidate_evidence_links": 8,
    "review_writes": 0,
    "durable_preference_writes": 0,
    "durable_project_writes": 0,
    "claim_writes": 0,
    "evidence_writes": 0,
    "projection_outbox_writes": 0,
    "qdrant_writes": 0,
    "prompt_injection": False,
}


class PreferenceProjectApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateLock:
    candidate_id: uuid.UUID
    candidate_hash: str


@dataclass(frozen=True)
class PreferenceProjectApplyManifest:
    path: Path
    sha256: str
    owner_user_id: uuid.UUID
    batch_id: uuid.UUID
    apply_authorized: bool
    extraction_manifest_sha256: str
    extraction_report_sha256: str
    project_request_id: uuid.UUID
    project_key: str
    project_display_name: str
    expected: Dict[str, int]
    preference_candidates: tuple[CandidateLock, ...]
    project_candidates: tuple[CandidateLock, ...]
    controls: Dict[str, Any]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _required_text(value: Any, field: str, *, max_length: int = 1000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreferenceProjectApplyError(f"{field} is required")
    if len(text) > max_length:
        raise PreferenceProjectApplyError(f"{field} exceeds {max_length} characters")
    return text


def _required_sha(value: Any, field: str) -> str:
    text = _required_text(value, field, max_length=64)
    if not SHA256_RE.fullmatch(text):
        raise PreferenceProjectApplyError(f"{field} must be lowercase SHA-256 hex")
    return text


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PreferenceProjectApplyError(f"{field} must be a UUID") from exc
    if result.int == 0:
        raise PreferenceProjectApplyError(f"{field} must be non-nil")
    return result


def _object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreferenceProjectApplyError(f"{field} must be an object")
    return dict(value)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PreferenceProjectApplyError(f"{field} must be a non-negative integer")
    return value


def _load_candidate_locks(value: Any, field: str) -> tuple[CandidateLock, ...]:
    if not isinstance(value, list):
        raise PreferenceProjectApplyError(f"{field} must be an array")
    result: list[CandidateLock] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{field}[{index}]")
        if set(item) != {"candidate_id", "candidate_hash"}:
            raise PreferenceProjectApplyError(f"{field}[{index}] has unexpected fields")
        result.append(
            CandidateLock(
                candidate_id=_uuid(item["candidate_id"], f"{field}[{index}].candidate_id"),
                candidate_hash=_required_sha(
                    item["candidate_hash"], f"{field}[{index}].candidate_hash"
                ),
            )
        )
    identities = {(item.candidate_id, item.candidate_hash) for item in result}
    if len(identities) != len(result):
        raise PreferenceProjectApplyError(f"{field} contains duplicate locks")
    return tuple(result)


def load_preference_project_apply_manifest(
    path: str | Path,
) -> PreferenceProjectApplyManifest:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        root = _object(json.loads(raw_bytes), "authorization manifest")
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectApplyError(f"cannot read authorization manifest: {resolved}") from exc
    if root.get("manifest_version") != MANIFEST_VERSION:
        raise PreferenceProjectApplyError("unsupported authorization manifest_version")
    if not isinstance(root.get("apply_authorized"), bool):
        raise PreferenceProjectApplyError("apply_authorized must be boolean")
    controls = _object(root.get("controls"), "controls")
    if controls != CONTROL_CONTRACT:
        raise PreferenceProjectApplyError("authorization control contract changed")
    expected_raw = _object(root.get("expected"), "expected")
    expected_fields = {
        "preference_candidates",
        "preference_evidence_links",
        "project_candidates",
        "project_evidence_links",
        "project_spaces",
        "project_registration_events",
        "preference_reviews",
        "project_reviews",
        "durable_preferences",
        "durable_project_heads",
        "durable_preference_revisions",
        "durable_project_revisions",
    }
    if set(expected_raw) != expected_fields:
        raise PreferenceProjectApplyError("authorization expected-count contract changed")
    expected = {
        field: _nonnegative_int(expected_raw[field], f"expected.{field}")
        for field in sorted(expected_fields)
    }
    preference_locks = _load_candidate_locks(
        root.get("preference_candidates"), "preference_candidates"
    )
    project_locks = _load_candidate_locks(
        root.get("project_candidates"), "project_candidates"
    )
    if len(preference_locks) != expected["preference_candidates"]:
        raise PreferenceProjectApplyError("preference candidate lock count changed")
    if len(project_locks) != expected["project_candidates"]:
        raise PreferenceProjectApplyError("project candidate lock count changed")
    if expected["preference_evidence_links"] != len(preference_locks):
        raise PreferenceProjectApplyError("preference link count must equal candidate count")
    if expected["project_evidence_links"] != len(project_locks):
        raise PreferenceProjectApplyError("project link count must equal candidate count")
    if len(preference_locks) + len(project_locks) != controls["candidate_rows"]:
        raise PreferenceProjectApplyError("authorized candidate-row count changed")
    if (
        expected["preference_evidence_links"] + expected["project_evidence_links"]
        != controls["candidate_evidence_links"]
    ):
        raise PreferenceProjectApplyError("authorized evidence-link count changed")
    zero_fields = (
        "preference_reviews",
        "project_reviews",
        "durable_preferences",
        "durable_project_heads",
        "durable_preference_revisions",
        "durable_project_revisions",
    )
    if any(expected[field] != 0 for field in zero_fields):
        raise PreferenceProjectApplyError("reviews and durable records must remain zero")
    if expected["project_spaces"] != 1 or expected["project_registration_events"] != 1:
        raise PreferenceProjectApplyError("exactly one project registration is required")
    registration = _object(root.get("project_registration"), "project_registration")
    return PreferenceProjectApplyManifest(
        path=resolved,
        sha256=_sha256_bytes(raw_bytes),
        owner_user_id=_uuid(root.get("owner_user_id"), "owner_user_id"),
        batch_id=_uuid(root.get("batch_id"), "batch_id"),
        apply_authorized=root["apply_authorized"],
        extraction_manifest_sha256=_required_sha(
            root.get("extraction_manifest_sha256"), "extraction_manifest_sha256"
        ),
        extraction_report_sha256=_required_sha(
            root.get("extraction_report_sha256"), "extraction_report_sha256"
        ),
        project_request_id=_uuid(registration.get("request_id"), "project_registration.request_id"),
        project_key=_required_text(
            registration.get("project_key"), "project_registration.project_key", max_length=500
        ),
        project_display_name=_required_text(
            registration.get("display_name"),
            "project_registration.display_name",
            max_length=500,
        ),
        expected=expected,
        preference_candidates=preference_locks,
        project_candidates=project_locks,
        controls=controls,
    )


def _load_locked_inputs(
    manifest: PreferenceProjectApplyManifest,
    extraction_manifest_path: str | Path,
    extraction_report_path: str | Path,
) -> tuple[ExtractionManifest, Dict[str, Any]]:
    extraction_path = Path(extraction_manifest_path).resolve()
    if _sha256_bytes(extraction_path.read_bytes()) != manifest.extraction_manifest_sha256:
        raise PreferenceProjectApplyError("extraction manifest SHA-256 changed")
    extraction = load_extraction_manifest(extraction_path)
    if extraction.owner_user_id != manifest.owner_user_id:
        raise PreferenceProjectApplyError("extraction manifest owner changed")
    if extraction.batch_id != manifest.batch_id:
        raise PreferenceProjectApplyError("extraction manifest batch changed")
    project_registration = extraction.project_registration
    if (
        project_registration.get("project_key") != manifest.project_key
        or project_registration.get("display_name") != manifest.project_display_name
    ):
        raise PreferenceProjectApplyError("extraction project registration changed")

    report_path = Path(extraction_report_path).resolve()
    try:
        report_bytes = report_path.read_bytes()
        report = _object(json.loads(report_bytes), "extraction report")
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectApplyError(f"cannot read extraction report: {report_path}") from exc
    if _sha256_bytes(report_bytes) != manifest.extraction_report_sha256:
        raise PreferenceProjectApplyError("extraction report SHA-256 changed")
    required_report = {
        "mode": REPORT_MODE,
        "manifest_version": EXTRACTION_MANIFEST_VERSION,
        "manifest_sha256": manifest.extraction_manifest_sha256,
        "owner_user_id": str(manifest.owner_user_id),
        "batch_id": str(manifest.batch_id),
        "ready_for_persistence_review": True,
    }
    for field, expected in required_report.items():
        if report.get(field) != expected:
            raise PreferenceProjectApplyError(
                f"extraction report {field} changed: expected={expected!r}, actual={report.get(field)!r}"
            )
    summary = _object(report.get("summary"), "extraction report summary")
    expected_summary = {
        "preference_candidates_proposed": manifest.expected["preference_candidates"],
        "preference_evidence_links_proposed": manifest.expected["preference_evidence_links"],
        "project_candidates_proposed": manifest.expected["project_candidates"],
        "project_evidence_links_proposed": manifest.expected["project_evidence_links"],
        "candidate_records_created": 0,
        "review_records_created": 0,
        "durable_records_created": 0,
    }
    for field, expected in expected_summary.items():
        if summary.get(field) != expected:
            raise PreferenceProjectApplyError(f"extraction summary changed: {field}")
    registration = _object(report.get("project_registration"), "project_registration")
    if (
        registration.get("request_id") != str(manifest.project_request_id)
        or registration.get("project_key") != manifest.project_key
        or registration.get("display_name") != manifest.project_display_name
        or registration.get("metadata") != project_registration.get("metadata")
        or registration.get("operation") != "proposed_not_executed"
    ):
        raise PreferenceProjectApplyError("extraction project registration proposal changed")
    report_controls = _object(report.get("controls"), "extraction report controls")
    for field, expected in {
        "apply_authorized": False,
        "apply_option_exposed": False,
        "candidate_writes": 0,
        "review_writes": 0,
        "durable_preference_writes": 0,
        "durable_project_writes": 0,
        "persistence_authorized": False,
        "source_evidence_hashes_verified": True,
        "owner_rls_context_required": True,
    }.items():
        if report_controls.get(field) != expected:
            raise PreferenceProjectApplyError(f"extraction report control changed: {field}")

    preference_lane = _object(report.get("preference_lane"), "preference_lane")
    project_lane = _object(report.get("project_lane"), "project_lane")
    preference_candidates = preference_lane.get("candidates")
    preference_links = preference_lane.get("evidence_links")
    project_candidates = project_lane.get("candidates")
    project_links = project_lane.get("evidence_links")
    for value, field, expected in (
        (preference_candidates, "preference candidates", manifest.expected["preference_candidates"]),
        (preference_links, "preference links", manifest.expected["preference_evidence_links"]),
        (project_candidates, "project candidates", manifest.expected["project_candidates"]),
        (project_links, "project links", manifest.expected["project_evidence_links"]),
    ):
        if not isinstance(value, list) or len(value) != expected:
            raise PreferenceProjectApplyError(f"{field} are incomplete")
    locked_preferences = [
        {"candidate_id": str(item.candidate_id), "candidate_hash": item.candidate_hash}
        for item in manifest.preference_candidates
    ]
    locked_projects = [
        {"candidate_id": str(item.candidate_id), "candidate_hash": item.candidate_hash}
        for item in manifest.project_candidates
    ]
    if [
        {"candidate_id": row.get("candidate_id"), "candidate_hash": row.get("candidate_hash")}
        for row in preference_candidates
    ] != locked_preferences:
        raise PreferenceProjectApplyError("preference candidate identity locks changed")
    if [
        {"candidate_id": row.get("candidate_id"), "candidate_hash": row.get("candidate_hash")}
        for row in project_candidates
    ] != locked_projects:
        raise PreferenceProjectApplyError("project candidate identity locks changed")
    _validate_lane_ownership(manifest, preference_candidates, preference_links, False)
    _validate_lane_ownership(manifest, project_candidates, project_links, True)
    return extraction, report


def _validate_lane_ownership(
    manifest: PreferenceProjectApplyManifest,
    candidates: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
    project_lane: bool,
) -> None:
    candidate_ids = {str(row.get("candidate_id")) for row in candidates}
    link_candidate_ids = {str(row.get("candidate_id")) for row in links}
    if candidate_ids != link_candidate_ids or len(link_candidate_ids) != len(links):
        raise PreferenceProjectApplyError("candidate/evidence links are not one-to-one")
    evidence_ids: set[str] = set()
    for row in [*candidates, *links]:
        if row.get("owner_user_id") != str(manifest.owner_user_id):
            raise PreferenceProjectApplyError("candidate plan contains a different owner")
        if project_lane and row.get("project_key") != manifest.project_key:
            raise PreferenceProjectApplyError("project candidate plan contains a different project")
    for link in links:
        evidence_id = str(_uuid(link.get("evidence_id"), "evidence link evidence_id"))
        if evidence_id in evidence_ids:
            raise PreferenceProjectApplyError("candidate lane reuses an evidence link")
        evidence_ids.add(evidence_id)
    for candidate in candidates:
        metadata = _object(candidate.get("metadata"), "candidate metadata")
        if metadata.get("batch_id") != str(manifest.batch_id):
            raise PreferenceProjectApplyError("candidate metadata batch changed")
        if metadata.get("extraction_manifest_sha256") != manifest.extraction_manifest_sha256:
            raise PreferenceProjectApplyError("candidate metadata manifest lock changed")
        _required_sha(metadata.get("source_content_sha256"), "source_content_sha256")


def _normalize(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _stored_row(row: asyncpg.Record | None, *json_fields: str) -> Dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in json_fields:
        value = result.get(field)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PreferenceProjectApplyError(
                    f"stored {field} is not valid JSON"
                ) from exc
            result[field] = value
    return result


async def _audit_database_controls(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
    *,
    read_only: bool,
) -> Dict[str, bool]:
    controls = {
        "effective_role_brains_app": bool(await conn.fetchval("SELECT current_user='brains_app'")),
        "actor_context_exact": bool(
            await conn.fetchval(
                "SELECT memory.current_actor_user_id()=$1", manifest.owner_user_id
            )
        ),
        "transaction_mode_exact": bool(
            await conn.fetchval(
                "SELECT current_setting('transaction_read_only')=$1",
                "on" if read_only else "off",
            )
        ),
        "specialized_tables_forced_rls": bool(
            await conn.fetchval(
                """
                SELECT count(*)=$1
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='memory' AND c.relname=ANY($2::text[])
                  AND c.relrowsecurity AND c.relforcerowsecurity
                """,
                len(NEW_TABLES),
                list(NEW_TABLES),
            )
        ),
        "candidate_insert_only_grants": bool(
            await conn.fetchval(
                """
                SELECT bool_and(
                  has_table_privilege(current_user, format('memory.%I', name), 'SELECT')
                  AND has_table_privilege(current_user, format('memory.%I', name), 'INSERT')
                  AND NOT has_table_privilege(current_user, format('memory.%I', name), 'UPDATE')
                  AND NOT has_table_privilege(current_user, format('memory.%I', name), 'DELETE')
                )
                FROM unnest(ARRAY[
                  'preference_candidate','preference_candidate_evidence',
                  'project_knowledge_candidate','project_knowledge_candidate_evidence'
                ]) AS name
                """
            )
        ),
        "candidate_append_only_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=4
                FROM pg_trigger
                WHERE tgrelid=ANY(ARRAY[
                  'memory.preference_candidate'::regclass,
                  'memory.preference_candidate_evidence'::regclass,
                  'memory.project_knowledge_candidate'::regclass,
                  'memory.project_knowledge_candidate_evidence'::regclass
                ]) AND tgname='specialized_append_only_guard'
                  AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
        "active_evidence_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=2
                FROM pg_trigger
                WHERE tgrelid=ANY(ARRAY[
                  'memory.preference_candidate_evidence'::regclass,
                  'memory.project_knowledge_candidate_evidence'::regclass
                ]) AND tgname IN (
                  'preference_candidate_active_evidence_guard',
                  'project_candidate_active_evidence_guard'
                ) AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
        "direct_review_and_durable_insert_denied": bool(
            await conn.fetchval(
                """
                SELECT bool_and(NOT has_table_privilege(
                  current_user, format('memory.%I', name), 'INSERT'
                ))
                FROM unnest(ARRAY[
                  'preference_candidate_review','preference_candidate_review_replacement',
                  'project_knowledge_candidate_review',
                  'project_knowledge_candidate_review_replacement',
                  'user_preference','project_knowledge_head',
                  'preference_revision','preference_revision_evidence',
                  'preference_apply_event','project_knowledge_revision',
                  'project_knowledge_revision_evidence','project_knowledge_apply_event'
                ]) AS name
                """
            )
        ),
        "project_registration_execute_allowed": bool(
            await conn.fetchval(
                """
                SELECT has_function_privilege(
                  current_user,
                  'memory.register_project_space(uuid,text,text,jsonb)',
                  'EXECUTE'
                )
                """
            )
        ),
        "project_registration_function_hardened": bool(
            await conn.fetchval(
                """
                SELECT p.prosecdef
                  AND r.rolname='memory_review_maintainer'
                  AND p.proconfig @> ARRAY['search_path=pg_catalog','row_security=on']::text[]
                FROM pg_proc p
                JOIN pg_roles r ON r.oid=p.proowner
                WHERE p.oid='memory.register_project_space(uuid,text,text,jsonb)'::regprocedure
                """
            )
        ),
        "source_evidence_forced_rls": bool(
            await conn.fetchval(
                """
                SELECT count(*)=3
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='memory'
                  AND c.relname=ANY(ARRAY[
                    'evidence','evidence_ingest_batch','evidence_ingest_batch_row'
                  ]) AND c.relrowsecurity AND c.relforcerowsecurity
                """
            )
        ),
    }
    return controls


def _planned_rows(report: Mapping[str, Any]) -> tuple[list[Dict[str, Any]], ...]:
    preference = _object(report.get("preference_lane"), "preference_lane")
    project = _object(report.get("project_lane"), "project_lane")
    return (
        [dict(row) for row in preference["candidates"]],
        [dict(row) for row in preference["evidence_links"]],
        [dict(row) for row in project["candidates"]],
        [dict(row) for row in project["evidence_links"]],
    )


async def _verify_source_evidence(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
    report: Mapping[str, Any],
) -> None:
    preference_candidates, preference_links, project_candidates, project_links = _planned_rows(report)
    candidates = {
        row["candidate_id"]: row for row in [*preference_candidates, *project_candidates]
    }
    links = [*preference_links, *project_links]
    evidence_ids = [uuid.UUID(row["evidence_id"]) for row in links]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise PreferenceProjectApplyError("authorized source evidence is not one-to-one")
    rows = await conn.fetch(
        """
        SELECT e.evidence_id, e.owner_user_id, e.content_sha256,
               e.status::text, ledger.content_sha256 AS ledger_content_sha256
        FROM memory.evidence e
        JOIN memory.evidence_ingest_batch_row ledger
          ON ledger.owner_user_id=e.owner_user_id
         AND ledger.evidence_id=e.evidence_id
         AND ledger.batch_id=$2
        JOIN memory.evidence_ingest_batch batch
          ON batch.owner_user_id=ledger.owner_user_id
         AND batch.batch_id=ledger.batch_id
         AND batch.status='committed'
        WHERE e.owner_user_id=$1 AND e.evidence_id=ANY($3::uuid[])
        ORDER BY e.evidence_id
        """,
        manifest.owner_user_id,
        manifest.batch_id,
        evidence_ids,
    )
    if len(rows) != len(evidence_ids):
        raise PreferenceProjectApplyError("source evidence batch membership is incomplete")
    state = {str(row["evidence_id"]): row for row in rows}
    for link in links:
        evidence = state.get(link["evidence_id"])
        candidate = candidates[link["candidate_id"]]
        expected_hash = candidate["metadata"]["source_content_sha256"]
        if evidence is None:
            raise PreferenceProjectApplyError("authorized evidence is missing")
        if uuid.UUID(str(evidence["owner_user_id"])) != manifest.owner_user_id:
            raise PreferenceProjectApplyError("source evidence owner changed")
        if evidence["status"] != "active":
            raise PreferenceProjectApplyError("source evidence is not active")
        if evidence["content_sha256"] != expected_hash:
            raise PreferenceProjectApplyError("source evidence content hash changed")
        if evidence["ledger_content_sha256"] != expected_hash:
            raise PreferenceProjectApplyError("source evidence ledger hash changed")


async def _owner_counts(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
) -> Dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM memory.evidence WHERE owner_user_id=$1) AS evidence,
          (SELECT count(*) FROM memory.candidate WHERE owner_user_id=$1) AS claim_candidates,
          (SELECT count(*) FROM memory.claim WHERE owner_user_id=$1) AS claims,
          (SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=$1) AS outbox,
          (SELECT count(*) FROM memory.evidence_lifecycle_event WHERE owner_user_id=$1) AS lifecycle,
          (SELECT count(*) FROM memory.preference_candidate WHERE owner_user_id=$1) AS preference_candidates,
          (SELECT count(*) FROM memory.preference_candidate_evidence WHERE owner_user_id=$1) AS preference_links,
          (SELECT count(*) FROM memory.preference_candidate_review WHERE owner_user_id=$1) AS preference_reviews,
          (SELECT count(*) FROM memory.preference_candidate_review_replacement WHERE owner_user_id=$1) AS preference_replacements,
          (SELECT count(*) FROM memory.user_preference WHERE owner_user_id=$1) AS durable_preferences,
          (SELECT count(*) FROM memory.preference_revision WHERE owner_user_id=$1) AS preference_revisions,
          (SELECT count(*) FROM memory.preference_apply_event WHERE owner_user_id=$1) AS preference_apply_events,
          (SELECT count(*) FROM memory.project_space WHERE owner_user_id=$1 AND project_key=$2) AS project_spaces,
          (SELECT count(*) FROM memory.project_space_registration_event WHERE owner_user_id=$1) AS project_registration_events,
          (SELECT count(*) FROM memory.project_knowledge_candidate WHERE owner_user_id=$1) AS project_candidates,
          (SELECT count(*) FROM memory.project_knowledge_candidate_evidence WHERE owner_user_id=$1) AS project_links,
          (SELECT count(*) FROM memory.project_knowledge_candidate_review WHERE owner_user_id=$1) AS project_reviews,
          (SELECT count(*) FROM memory.project_knowledge_candidate_review_replacement WHERE owner_user_id=$1) AS project_replacements,
          (SELECT count(*) FROM memory.project_knowledge_head WHERE owner_user_id=$1) AS durable_project_heads,
          (SELECT count(*) FROM memory.project_knowledge_revision WHERE owner_user_id=$1) AS project_revisions,
          (SELECT count(*) FROM memory.project_knowledge_apply_event WHERE owner_user_id=$1) AS project_apply_events
        """,
        manifest.owner_user_id,
        manifest.project_key,
    )
    return {key: int(value) for key, value in dict(row).items()}


async def _fetch_stored_state(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
) -> Dict[str, Any]:
    preference_ids = [item.candidate_id for item in manifest.preference_candidates]
    project_ids = [item.candidate_id for item in manifest.project_candidates]
    project = await conn.fetchrow(
        """
        SELECT project_id, owner_user_id, project_key, display_name, metadata
        FROM memory.project_space
        WHERE owner_user_id=$1 AND project_key=$2
        """,
        manifest.owner_user_id,
        manifest.project_key,
    )
    registration = await conn.fetchrow(
        """
        SELECT owner_user_id, request_id, request_sha256, project_id,
               outcome, actor_user_id, invoked_by_role::text, metadata
        FROM memory.project_space_registration_event
        WHERE owner_user_id=$1 AND request_id=$2
        """,
        manifest.owner_user_id,
        manifest.project_request_id,
    )
    preferences = await conn.fetch(
        """
        SELECT candidate_id, owner_user_id, preference_class, preference_domain,
               preference_key, value, polarity, scope, explicit, stability,
               surface_policy, extraction_confidence, sensitivity::text,
               candidate_hash, extractor, extractor_version, metadata
        FROM memory.preference_candidate
        WHERE owner_user_id=$1 AND candidate_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[], candidate_id)
        """,
        manifest.owner_user_id,
        preference_ids,
    )
    preference_links = await conn.fetch(
        """
        SELECT owner_user_id, candidate_id, evidence_id, stance::text,
               relevance, rationale
        FROM memory.preference_candidate_evidence
        WHERE owner_user_id=$1 AND candidate_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[], candidate_id), evidence_id
        """,
        manifest.owner_user_id,
        preference_ids,
    )
    projects = await conn.fetch(
        """
        SELECT candidate_id, owner_user_id, project_id, knowledge_kind,
               knowledge_key, canonical_text, document_state, authority_level,
               authority_source, effective_at, expires_at, extraction_confidence,
               sensitivity::text, candidate_hash, extractor, extractor_version, metadata
        FROM memory.project_knowledge_candidate
        WHERE owner_user_id=$1 AND candidate_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[], candidate_id)
        """,
        manifest.owner_user_id,
        project_ids,
    )
    project_links = await conn.fetch(
        """
        SELECT owner_user_id, project_id, candidate_id, evidence_id,
               stance::text, relevance, rationale
        FROM memory.project_knowledge_candidate_evidence
        WHERE owner_user_id=$1 AND candidate_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[], candidate_id), evidence_id
        """,
        manifest.owner_user_id,
        project_ids,
    )
    return {
        "project": _stored_row(project, "metadata"),
        "registration": _stored_row(registration, "metadata"),
        "preference_candidates": [
            _stored_row(row, "value", "scope", "metadata") for row in preferences
        ],
        "preference_links": [dict(row) for row in preference_links],
        "project_candidates": [
            _stored_row(row, "authority_source", "metadata") for row in projects
        ],
        "project_links": [dict(row) for row in project_links],
    }


def _state_presence(state: Mapping[str, Any]) -> int:
    return sum(
        [
            int(state["project"] is not None),
            int(state["registration"] is not None),
            len(state["preference_candidates"]),
            len(state["preference_links"]),
            len(state["project_candidates"]),
            len(state["project_links"]),
        ]
    )


def _verify_stored_state(
    manifest: PreferenceProjectApplyManifest,
    extraction: ExtractionManifest,
    report: Mapping[str, Any],
    state: Mapping[str, Any],
) -> uuid.UUID:
    preference_candidates, preference_links, project_candidates, project_links = _planned_rows(report)
    project = state.get("project")
    registration = state.get("registration")
    if project is None or registration is None:
        raise PreferenceProjectApplyError("stored project registration is incomplete")
    project_id = uuid.UUID(str(project["project_id"]))
    expected_project = {
        "project_id": str(project_id),
        "owner_user_id": str(manifest.owner_user_id),
        "project_key": manifest.project_key,
        "display_name": manifest.project_display_name,
        "metadata": extraction.project_registration["metadata"],
    }
    if _normalize(project) != expected_project:
        raise PreferenceProjectApplyError("stored project registration differs from plan")
    normalized_event = _normalize(registration)
    if (
        normalized_event.get("owner_user_id") != str(manifest.owner_user_id)
        or normalized_event.get("request_id") != str(manifest.project_request_id)
        or normalized_event.get("project_id") != str(project_id)
        or normalized_event.get("outcome") != "created"
        or normalized_event.get("actor_user_id") != str(manifest.owner_user_id)
        or normalized_event.get("metadata") != extraction.project_registration["metadata"]
        or not SHA256_RE.fullmatch(str(normalized_event.get("request_sha256", "")))
        or not str(normalized_event.get("invoked_by_role", "")).strip()
    ):
        raise PreferenceProjectApplyError("stored project registration event differs from plan")

    expected_preferences = preference_candidates
    expected_preference_links = preference_links
    expected_projects = [
        {
            **{key: value for key, value in row.items() if key != "project_key"},
            "project_id": str(project_id),
        }
        for row in project_candidates
    ]
    expected_project_links = [
        {
            **{key: value for key, value in row.items() if key != "project_key"},
            "project_id": str(project_id),
        }
        for row in project_links
    ]
    comparisons = (
        (state["preference_candidates"], expected_preferences, "preference candidates"),
        (state["preference_links"], expected_preference_links, "preference links"),
        (state["project_candidates"], expected_projects, "project candidates"),
        (state["project_links"], expected_project_links, "project links"),
    )
    for actual, expected, label in comparisons:
        if _normalize(actual) != _normalize(expected):
            raise PreferenceProjectApplyError(f"stored {label} differ from the locked report")
    return project_id


async def _register_project(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
    extraction: ExtractionManifest,
) -> uuid.UUID:
    rows = await conn.fetch(
        """
        SELECT project_id
        FROM memory.register_project_space($1,$2,$3,$4::jsonb)
        """,
        manifest.project_request_id,
        manifest.project_key,
        manifest.project_display_name,
        _stable_json(extraction.project_registration["metadata"]),
    )
    if len(rows) != 1:
        raise PreferenceProjectApplyError("controlled project registration returned an unexpected row count")
    return uuid.UUID(str(rows[0]["project_id"]))


async def _insert_candidates(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectApplyManifest,
    report: Mapping[str, Any],
    project_id: uuid.UUID,
) -> None:
    preference_candidates, preference_links, project_candidates, project_links = _planned_rows(report)
    inserted_preferences = await conn.fetch(
        """
        INSERT INTO memory.preference_candidate(
          candidate_id, owner_user_id, preference_class, preference_domain,
          preference_key, value, polarity, scope, explicit, stability,
          surface_policy, extraction_confidence, sensitivity, candidate_hash,
          extractor, extractor_version, metadata
        )
        SELECT p.candidate_id, p.owner_user_id, p.preference_class,
               p.preference_domain, p.preference_key, p.value, p.polarity,
               p.scope, p.explicit, p.stability, p.surface_policy,
               p.extraction_confidence, p.sensitivity::memory.sensitivity_level,
               p.candidate_hash, p.extractor, p.extractor_version, p.metadata
        FROM jsonb_to_recordset($1::jsonb) AS p(
          candidate_id uuid, owner_user_id uuid, preference_class text,
          preference_domain text, preference_key text, value jsonb, polarity text,
          scope jsonb, explicit boolean, stability text, surface_policy text,
          extraction_confidence numeric, sensitivity text, candidate_hash text,
          extractor text, extractor_version text, metadata jsonb
        )
        RETURNING candidate_id
        """,
        _stable_json(preference_candidates),
    )
    if {row["candidate_id"] for row in inserted_preferences} != {
        item.candidate_id for item in manifest.preference_candidates
    }:
        raise PreferenceProjectApplyError("inserted preference candidate identities changed")
    preference_link_result = await conn.execute(
        """
        INSERT INTO memory.preference_candidate_evidence(
          owner_user_id, candidate_id, evidence_id, stance, relevance, rationale
        )
        SELECT p.owner_user_id, p.candidate_id, p.evidence_id,
               p.stance::memory.evidence_stance, p.relevance, p.rationale
        FROM jsonb_to_recordset($1::jsonb) AS p(
          owner_user_id uuid, candidate_id uuid, evidence_id uuid,
          stance text, relevance numeric, rationale text
        )
        """,
        _stable_json(preference_links),
    )
    if preference_link_result != f"INSERT 0 {manifest.expected['preference_evidence_links']}":
        raise PreferenceProjectApplyError("inserted preference link count changed")

    project_payload = [
        {**{key: value for key, value in row.items() if key != "project_key"}, "project_id": str(project_id)}
        for row in project_candidates
    ]
    inserted_projects = await conn.fetch(
        """
        INSERT INTO memory.project_knowledge_candidate(
          candidate_id, owner_user_id, project_id, knowledge_kind, knowledge_key,
          canonical_text, document_state, authority_level, authority_source,
          effective_at, expires_at, extraction_confidence, sensitivity,
          candidate_hash, extractor, extractor_version, metadata
        )
        SELECT p.candidate_id, p.owner_user_id, p.project_id, p.knowledge_kind,
               p.knowledge_key, p.canonical_text, p.document_state,
               p.authority_level, p.authority_source, p.effective_at, p.expires_at,
               p.extraction_confidence, p.sensitivity::memory.sensitivity_level,
               p.candidate_hash, p.extractor, p.extractor_version, p.metadata
        FROM jsonb_to_recordset($1::jsonb) AS p(
          candidate_id uuid, owner_user_id uuid, project_id uuid,
          knowledge_kind text, knowledge_key text, canonical_text text,
          document_state text, authority_level text, authority_source jsonb,
          effective_at timestamptz, expires_at timestamptz,
          extraction_confidence numeric, sensitivity text, candidate_hash text,
          extractor text, extractor_version text, metadata jsonb
        )
        RETURNING candidate_id
        """,
        _stable_json(project_payload),
    )
    if {row["candidate_id"] for row in inserted_projects} != {
        item.candidate_id for item in manifest.project_candidates
    }:
        raise PreferenceProjectApplyError("inserted project candidate identities changed")
    project_link_payload = [
        {**{key: value for key, value in row.items() if key != "project_key"}, "project_id": str(project_id)}
        for row in project_links
    ]
    project_link_result = await conn.execute(
        """
        INSERT INTO memory.project_knowledge_candidate_evidence(
          owner_user_id, project_id, candidate_id, evidence_id,
          stance, relevance, rationale
        )
        SELECT p.owner_user_id, p.project_id, p.candidate_id, p.evidence_id,
               p.stance::memory.evidence_stance, p.relevance, p.rationale
        FROM jsonb_to_recordset($1::jsonb) AS p(
          owner_user_id uuid, project_id uuid, candidate_id uuid, evidence_id uuid,
          stance text, relevance numeric, rationale text
        )
        """,
        _stable_json(project_link_payload),
    )
    if project_link_result != f"INSERT 0 {manifest.expected['project_evidence_links']}":
        raise PreferenceProjectApplyError("inserted project link count changed")


def _verify_zero_lanes(counts: Mapping[str, int]) -> None:
    zero_fields = (
        "preference_reviews",
        "preference_replacements",
        "durable_preferences",
        "preference_revisions",
        "preference_apply_events",
        "project_reviews",
        "project_replacements",
        "durable_project_heads",
        "project_revisions",
        "project_apply_events",
    )
    changed = {field: counts[field] for field in zero_fields if counts[field] != 0}
    if changed:
        raise PreferenceProjectApplyError(f"review or durable lanes are not zero: {changed}")


async def run_controlled_preference_project_apply(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
    extraction_manifest_path: str | Path,
    extraction_report_path: str | Path,
    apply: bool = False,
    confirmation: str = "",
) -> Dict[str, Any]:
    manifest = load_preference_project_apply_manifest(manifest_path)
    extraction, report = _load_locked_inputs(
        manifest, extraction_manifest_path, extraction_report_path
    )
    if apply:
        if not manifest.apply_authorized:
            raise PreferenceProjectApplyError("authorization manifest does not permit apply")
        if confirmation != CONFIRMATION:
            raise PreferenceProjectApplyError(f"confirmation must equal {CONFIRMATION}")

    async with conn.transaction(isolation="serializable", readonly=not apply):
        await conn.execute("SELECT set_config('lock_timeout','5s',true)")
        await conn.execute("SELECT set_config('statement_timeout','120s',true)")
        if await conn.fetchval("SELECT current_user <> 'brains_app'"):
            await conn.execute("SET LOCAL ROLE brains_app")
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", str(manifest.owner_user_id)
        )
        if apply:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"{manifest.owner_user_id}:{manifest.sha256}",
            )
        controls = await _audit_database_controls(
            conn, manifest, read_only=not apply
        )
        if not all(controls.values()):
            raise PreferenceProjectApplyError(f"database safety controls failed: {controls}")
        await _verify_source_evidence(conn, manifest, report)
        before = await _owner_counts(conn, manifest)
        _verify_zero_lanes(before)
        state = await _fetch_stored_state(conn, manifest)
        presence = _state_presence(state)
        expected_presence = (
            2
            + manifest.expected["preference_candidates"]
            + manifest.expected["preference_evidence_links"]
            + manifest.expected["project_candidates"]
            + manifest.expected["project_evidence_links"]
        )
        if presence:
            if presence != expected_presence:
                raise PreferenceProjectApplyError("partial candidate persistence state detected")
            project_id = _verify_stored_state(manifest, extraction, report, state)
            if apply:
                verified_project_id = await _register_project(conn, manifest, extraction)
                if verified_project_id != project_id:
                    raise PreferenceProjectApplyError(
                        "idempotent project registration returned a different project"
                    )
            expected_replay_counts = {
                "preference_candidates": manifest.expected["preference_candidates"],
                "preference_links": manifest.expected["preference_evidence_links"],
                "project_spaces": manifest.expected["project_spaces"],
                "project_registration_events": manifest.expected["project_registration_events"],
                "project_candidates": manifest.expected["project_candidates"],
                "project_links": manifest.expected["project_evidence_links"],
            }
            for field, expected in expected_replay_counts.items():
                if before[field] != expected:
                    raise PreferenceProjectApplyError(f"replay count changed: {field}")
            after_replay = await _owner_counts(conn, manifest)
            if after_replay != before:
                raise PreferenceProjectApplyError("replay changed database counts")
            return {
                "status": "verified_replay",
                "owner_user_id": str(manifest.owner_user_id),
                "project_id": str(project_id),
                "project_key": manifest.project_key,
                "preference_candidates": manifest.expected["preference_candidates"],
                "project_candidates": manifest.expected["project_candidates"],
                "candidate_evidence_links": (
                    manifest.expected["preference_evidence_links"]
                    + manifest.expected["project_evidence_links"]
                ),
                "reviews": 0,
                "durable_records": 0,
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "controls": controls,
            }

        report_database_controls = _object(
            report.get("database_controls"), "extraction report database_controls"
        )
        fresh_expectations = {
            "preference_candidates": report_database_controls.get("existing_preference_candidates"),
            "project_candidates": report_database_controls.get("existing_project_candidates"),
            "project_spaces": report_database_controls.get("existing_project_spaces"),
            "preference_reviews": report_database_controls.get("existing_preference_reviews"),
            "project_reviews": report_database_controls.get("existing_project_reviews"),
            "durable_preferences": report_database_controls.get("existing_durable_preferences"),
            "durable_project_heads": report_database_controls.get("existing_durable_project_heads"),
        }
        if any(value != 0 for value in fresh_expectations.values()):
            raise PreferenceProjectApplyError("locked extraction report was not based on empty lanes")
        for field, expected in fresh_expectations.items():
            if before[field] != expected:
                raise PreferenceProjectApplyError(f"fresh-state count changed: {field}")
        if before["preference_links"] != 0 or before["project_links"] != 0:
            raise PreferenceProjectApplyError("fresh candidate link lanes are not empty")

        if not apply:
            return {
                "status": "preflight_verified",
                "apply_authorized": manifest.apply_authorized,
                "ready_for_apply": manifest.apply_authorized,
                "owner_user_id": str(manifest.owner_user_id),
                "project_key": manifest.project_key,
                "preference_candidates": manifest.expected["preference_candidates"],
                "project_candidates": manifest.expected["project_candidates"],
                "candidate_evidence_links": (
                    manifest.expected["preference_evidence_links"]
                    + manifest.expected["project_evidence_links"]
                ),
                "reviews": 0,
                "durable_records": 0,
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "controls": controls,
            }

        project_id = await _register_project(conn, manifest, extraction)
        await _insert_candidates(conn, manifest, report, project_id)
        stored_state = await _fetch_stored_state(conn, manifest)
        stored_project_id = _verify_stored_state(
            manifest, extraction, report, stored_state
        )
        if stored_project_id != project_id:
            raise PreferenceProjectApplyError("registered project identity changed")
        after = await _owner_counts(conn, manifest)
        _verify_zero_lanes(after)
        deltas = {field: after[field] - before[field] for field in after}
        expected_deltas = {
            "preference_candidates": manifest.expected["preference_candidates"],
            "preference_links": manifest.expected["preference_evidence_links"],
            "project_spaces": manifest.expected["project_spaces"],
            "project_registration_events": manifest.expected["project_registration_events"],
            "project_candidates": manifest.expected["project_candidates"],
            "project_links": manifest.expected["project_evidence_links"],
        }
        for field, expected in expected_deltas.items():
            if deltas[field] != expected:
                raise PreferenceProjectApplyError(f"authorized count delta changed: {field}")
        changed_elsewhere = {
            field: delta
            for field, delta in deltas.items()
            if field not in expected_deltas and delta != 0
        }
        if changed_elsewhere:
            raise PreferenceProjectApplyError(
                f"unauthorized downstream write detected: {changed_elsewhere}"
            )
        return {
            "status": "committed",
            "owner_user_id": str(manifest.owner_user_id),
            "project_id": str(project_id),
            "project_key": manifest.project_key,
            "preference_candidates_inserted": manifest.expected["preference_candidates"],
            "project_candidates_inserted": manifest.expected["project_candidates"],
            "candidate_evidence_links_inserted": (
                manifest.expected["preference_evidence_links"]
                + manifest.expected["project_evidence_links"]
            ),
            "reviews": 0,
            "durable_records": 0,
            "authorization_manifest_sha256": manifest.sha256,
            "database_writes": 18,
            "before": before,
            "after": after,
            "controls": controls,
        }
