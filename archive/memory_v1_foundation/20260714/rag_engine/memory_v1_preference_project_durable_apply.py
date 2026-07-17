from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import asyncpg

from .memory_v1_preference_project_apply import _normalize, _stored_row
from .memory_v1_preference_project_extraction import NEW_TABLES
from .memory_v1_preference_project_review import (
    PreferenceProjectReviewManifest,
    load_preference_project_review_manifest,
)


MANIFEST_VERSION = "memory_v1_preference_project_durable_apply_plan_20260714_v1"
CONFIRMATION = "APPLY_MEMORY_V1_8_DURABLE_PREFERENCE_PROJECT_RECORDS"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CONTRACT = {
    "allowed_writes_when_authorized": [
        "memory.user_preference:CONTROLLED_INSERT_OR_UPDATE",
        "memory.preference_revision:INSERT",
        "memory.preference_revision_evidence:INSERT",
        "memory.preference_apply_event:INSERT",
        "memory.project_knowledge_head:INSERT",
        "memory.project_knowledge_revision:INSERT",
        "memory.project_knowledge_revision_evidence:INSERT",
        "memory.project_knowledge_apply_event:INSERT",
    ],
    "review_writes": 0,
    "candidate_writes": 0,
    "evidence_writes": 0,
    "claim_writes": 0,
    "projection_outbox_writes": 0,
    "qdrant_writes": 0,
    "prompt_injection": False,
    "retrieval_activation": False,
}
EXPECTED_CONTRACT = {
    "reviewed_preference_candidates": 3,
    "reviewed_project_candidates": 5,
    "existing_durable_records": 0,
    "preference_head_inserts": 3,
    "preference_revision_inserts": 3,
    "preference_revision_evidence_inserts": 3,
    "preference_apply_event_inserts": 3,
    "project_head_inserts": 5,
    "project_revision_inserts": 5,
    "project_revision_evidence_inserts": 5,
    "project_apply_event_inserts": 5,
    "database_writes_if_authorized": 32,
}
DURABLE_COUNT_FIELDS = (
    "durable_preferences",
    "preference_revisions",
    "preference_revision_links",
    "preference_apply_events",
    "durable_project_heads",
    "project_revisions",
    "project_revision_links",
    "project_apply_events",
)


class PreferenceProjectDurableApplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DurableApplyItem:
    candidate_id: uuid.UUID
    candidate_hash: str
    accepted_review_id: uuid.UUID
    review_request_id: uuid.UUID
    apply_request_id: uuid.UUID
    head_key: str
    expected_current_revision_id: uuid.UUID | None
    event_metadata: Dict[str, Any]
    knowledge_kind: str | None = None


@dataclass(frozen=True)
class DurableApplyManifest:
    path: Path
    sha256: str
    owner_user_id: uuid.UUID
    project_id: uuid.UUID
    project_key: str
    dry_run_authorized: bool
    apply_authorized: bool
    review_manifest_sha256: str
    review_apply_report_sha256: str
    review_replay_report_sha256: str
    expected: Dict[str, int]
    preference_applies: tuple[DurableApplyItem, ...]
    project_applies: tuple[DurableApplyItem, ...]
    controls: Dict[str, Any]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _required_text(value: Any, field: str, *, max_length: int = 16000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreferenceProjectDurableApplyError(f"{field} is required")
    if len(text) > max_length:
        raise PreferenceProjectDurableApplyError(
            f"{field} exceeds {max_length} characters"
        )
    return text


def _required_sha(value: Any, field: str) -> str:
    text = _required_text(value, field, max_length=64)
    if not SHA256_RE.fullmatch(text):
        raise PreferenceProjectDurableApplyError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return text


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PreferenceProjectDurableApplyError(f"{field} must be a UUID") from exc
    if result.int == 0:
        raise PreferenceProjectDurableApplyError(f"{field} must be non-nil")
    return result


def _optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _uuid(value, field)


def _object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreferenceProjectDurableApplyError(f"{field} must be an object")
    return dict(value)


def _load_json_locked(path: str | Path, expected_sha: str, field: str) -> Dict[str, Any]:
    resolved = Path(path).resolve()
    try:
        raw = resolved.read_bytes()
        value = _object(json.loads(raw), field)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectDurableApplyError(f"cannot read {field}: {resolved}") from exc
    if _sha256_bytes(raw) != expected_sha:
        raise PreferenceProjectDurableApplyError(f"{field} SHA-256 changed")
    return value


def _load_items(
    value: Any,
    field: str,
    *,
    project_lane: bool,
    review_manifest_sha256: str,
) -> tuple[DurableApplyItem, ...]:
    if not isinstance(value, list):
        raise PreferenceProjectDurableApplyError(f"{field} must be an array")
    result: list[DurableApplyItem] = []
    base_fields = {
        "candidate_id",
        "candidate_hash",
        "accepted_review_id",
        "review_request_id",
        "apply_request_id",
        "head_key",
        "expected_current_revision_id",
        "event_metadata",
    }
    expected_fields = base_fields | ({"knowledge_kind"} if project_lane else set())
    expected_metadata = {
        "apply_contract": "memory_v1_preference_project_durable_apply_v1",
        "source_review_manifest_sha256": review_manifest_sha256,
    }
    for index, raw in enumerate(value):
        item = _object(raw, f"{field}[{index}]")
        if set(item) != expected_fields:
            raise PreferenceProjectDurableApplyError(
                f"{field}[{index}] field contract changed"
            )
        metadata = _object(item.get("event_metadata"), "event_metadata")
        if metadata != expected_metadata:
            raise PreferenceProjectDurableApplyError("event metadata contract changed")
        result.append(
            DurableApplyItem(
                candidate_id=_uuid(item.get("candidate_id"), "candidate_id"),
                candidate_hash=_required_sha(
                    item.get("candidate_hash"), "candidate_hash"
                ),
                accepted_review_id=_uuid(
                    item.get("accepted_review_id"), "accepted_review_id"
                ),
                review_request_id=_uuid(
                    item.get("review_request_id"), "review_request_id"
                ),
                apply_request_id=_uuid(
                    item.get("apply_request_id"), "apply_request_id"
                ),
                head_key=_required_text(item.get("head_key"), "head_key", max_length=500),
                expected_current_revision_id=_optional_uuid(
                    item.get("expected_current_revision_id"),
                    "expected_current_revision_id",
                ),
                event_metadata=metadata,
                knowledge_kind=(
                    _required_text(
                        item.get("knowledge_kind"), "knowledge_kind", max_length=100
                    )
                    if project_lane
                    else None
                ),
            )
        )
    for attr in (
        "candidate_id",
        "accepted_review_id",
        "review_request_id",
        "apply_request_id",
        "head_key",
    ):
        values = [getattr(item, attr) for item in result]
        if len(values) != len(set(values)):
            raise PreferenceProjectDurableApplyError(f"{field} contains duplicate {attr}")
    return tuple(result)


def load_durable_apply_manifest(path: str | Path) -> DurableApplyManifest:
    resolved = Path(path).resolve()
    try:
        raw = resolved.read_bytes()
        root = _object(json.loads(raw), "durable apply manifest")
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectDurableApplyError(
            f"cannot read durable apply manifest: {resolved}"
        ) from exc
    if root.get("manifest_version") != MANIFEST_VERSION:
        raise PreferenceProjectDurableApplyError("unsupported manifest_version")
    for field in ("dry_run_authorized", "apply_authorized"):
        if not isinstance(root.get(field), bool):
            raise PreferenceProjectDurableApplyError(f"{field} must be boolean")
    review_manifest_sha = _required_sha(
        root.get("review_manifest_sha256"), "review_manifest_sha256"
    )
    controls = _object(root.get("controls"), "controls")
    if controls != CONTROL_CONTRACT:
        raise PreferenceProjectDurableApplyError("durable apply control contract changed")
    expected_raw = _object(root.get("expected"), "expected")
    if expected_raw != EXPECTED_CONTRACT:
        raise PreferenceProjectDurableApplyError("durable expected-count contract changed")
    preferences = _load_items(
        root.get("preference_applies"),
        "preference_applies",
        project_lane=False,
        review_manifest_sha256=review_manifest_sha,
    )
    projects = _load_items(
        root.get("project_applies"),
        "project_applies",
        project_lane=True,
        review_manifest_sha256=review_manifest_sha,
    )
    if len(preferences) != EXPECTED_CONTRACT["reviewed_preference_candidates"]:
        raise PreferenceProjectDurableApplyError("preference apply count changed")
    if len(projects) != EXPECTED_CONTRACT["reviewed_project_candidates"]:
        raise PreferenceProjectDurableApplyError("project apply count changed")
    all_request_ids = [item.apply_request_id for item in [*preferences, *projects]]
    if len(all_request_ids) != len(set(all_request_ids)):
        raise PreferenceProjectDurableApplyError(
            "apply request IDs must be unique across lanes"
        )
    if any(item.expected_current_revision_id is not None for item in [*preferences, *projects]):
        raise PreferenceProjectDurableApplyError(
            "this first durable apply plan requires null revision locks"
        )
    return DurableApplyManifest(
        path=resolved,
        sha256=_sha256_bytes(raw),
        owner_user_id=_uuid(root.get("owner_user_id"), "owner_user_id"),
        project_id=_uuid(root.get("project_id"), "project_id"),
        project_key=_required_text(root.get("project_key"), "project_key", max_length=500),
        dry_run_authorized=root["dry_run_authorized"],
        apply_authorized=root["apply_authorized"],
        review_manifest_sha256=review_manifest_sha,
        review_apply_report_sha256=_required_sha(
            root.get("review_apply_report_sha256"), "review_apply_report_sha256"
        ),
        review_replay_report_sha256=_required_sha(
            root.get("review_replay_report_sha256"), "review_replay_report_sha256"
        ),
        expected=dict(EXPECTED_CONTRACT),
        preference_applies=preferences,
        project_applies=projects,
        controls=controls,
    )


def _verify_source_review_reports(
    manifest: DurableApplyManifest,
    review_manifest_path: str | Path,
    review_apply_report_path: str | Path,
    review_replay_report_path: str | Path,
) -> PreferenceProjectReviewManifest:
    review_path = Path(review_manifest_path).resolve()
    try:
        review_bytes = review_path.read_bytes()
    except OSError as exc:
        raise PreferenceProjectDurableApplyError("cannot read review manifest") from exc
    if _sha256_bytes(review_bytes) != manifest.review_manifest_sha256:
        raise PreferenceProjectDurableApplyError("review manifest SHA-256 changed")
    review = load_preference_project_review_manifest(review_path)
    if (
        review.owner_user_id != manifest.owner_user_id
        or review.project_id != manifest.project_id
        or review.project_key != manifest.project_key
    ):
        raise PreferenceProjectDurableApplyError("source review identity changed")

    apply_report = _load_json_locked(
        review_apply_report_path,
        manifest.review_apply_report_sha256,
        "review apply report",
    )
    replay_report = _load_json_locked(
        review_replay_report_path,
        manifest.review_replay_report_sha256,
        "review replay report",
    )
    report_contracts = (
        (
            apply_report,
            "committed",
            manifest.expected["reviewed_preference_candidates"],
            manifest.expected["reviewed_project_candidates"],
            8,
        ),
        (
            replay_report,
            "verified_replay",
            manifest.expected["reviewed_preference_candidates"],
            manifest.expected["reviewed_project_candidates"],
            0,
        ),
    )
    for report, status, preference_count, project_count, writes in report_contracts:
        expected = {
            "status": status,
            "authorization_manifest_sha256": manifest.review_manifest_sha256,
            "owner_user_id": str(manifest.owner_user_id),
            "project_id": str(manifest.project_id),
            "project_key": manifest.project_key,
            "review_replacements": 0,
            "durable_records": 0,
            "database_writes": writes,
        }
        if status == "committed":
            expected["preference_reviews_inserted"] = preference_count
            expected["project_reviews_inserted"] = project_count
        else:
            expected["preference_reviews"] = preference_count
            expected["project_reviews"] = project_count
        for field, value in expected.items():
            if report.get(field) != value:
                raise PreferenceProjectDurableApplyError(
                    f"source review report changed: {status}.{field}"
                )

    apply_review_ids = _object(apply_report.get("review_ids"), "apply review_ids")
    replay_review_ids = _object(replay_report.get("review_ids"), "replay review_ids")
    if apply_review_ids != replay_review_ids:
        raise PreferenceProjectDurableApplyError("review replay IDs changed")
    for source, item in zip(review.preference_reviews, manifest.preference_applies):
        if (
            source.candidate_id != item.candidate_id
            or source.candidate_hash != item.candidate_hash
            or source.request_id != item.review_request_id
            or apply_review_ids.get(str(item.candidate_id))
            != str(item.accepted_review_id)
        ):
            raise PreferenceProjectDurableApplyError(
                "preference apply lock differs from accepted review"
            )
    for source, item in zip(review.project_reviews, manifest.project_applies):
        if (
            source.candidate_id != item.candidate_id
            or source.candidate_hash != item.candidate_hash
            or source.request_id != item.review_request_id
            or apply_review_ids.get(str(item.candidate_id))
            != str(item.accepted_review_id)
        ):
            raise PreferenceProjectDurableApplyError(
                "project apply lock differs from accepted review"
            )
    if set(apply_review_ids) != {
        str(item.candidate_id)
        for item in [*manifest.preference_applies, *manifest.project_applies]
    }:
        raise PreferenceProjectDurableApplyError("review report candidate set changed")
    return review


async def _audit_database_controls(
    conn: asyncpg.Connection,
    manifest: DurableApplyManifest,
    *,
    read_only: bool,
) -> Dict[str, bool]:
    return {
        "effective_role_brains_app": bool(
            await conn.fetchval("SELECT current_user='brains_app'")
        ),
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
        "direct_durable_writes_denied": bool(
            await conn.fetchval(
                """
                SELECT bool_and(
                  NOT has_table_privilege(current_user, format('memory.%I', name), 'INSERT')
                  AND NOT has_table_privilege(current_user, format('memory.%I', name), 'UPDATE')
                  AND NOT has_table_privilege(current_user, format('memory.%I', name), 'DELETE')
                )
                FROM unnest(ARRAY[
                  'user_preference','preference_revision',
                  'preference_revision_evidence','preference_apply_event',
                  'project_knowledge_head','project_knowledge_revision',
                  'project_knowledge_revision_evidence',
                  'project_knowledge_apply_event'
                ]) AS name
                """
            )
        ),
        "apply_execute_allowed": bool(
            await conn.fetchval(
                """
                SELECT has_function_privilege(
                  current_user,
                  'memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)',
                  'EXECUTE'
                ) AND has_function_privilege(
                  current_user,
                  'memory.apply_project_candidate(uuid,uuid,uuid,jsonb)',
                  'EXECUTE'
                )
                """
            )
        ),
        "apply_functions_hardened": bool(
            await conn.fetchval(
                """
                SELECT count(*)=2 AND bool_and(
                  p.prosecdef AND r.rolname='memory_review_maintainer'
                  AND p.proconfig @> ARRAY[
                    'search_path=pg_catalog','row_security=on'
                  ]::text[]
                )
                FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                WHERE p.oid=ANY(ARRAY[
                  'memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)'::regprocedure,
                  'memory.apply_project_candidate(uuid,uuid,uuid,jsonb)'::regprocedure
                ])
                """
            )
        ),
        "durable_append_only_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=7
                FROM pg_trigger
                WHERE tgrelid=ANY(ARRAY[
                  'memory.preference_revision'::regclass,
                  'memory.preference_revision_evidence'::regclass,
                  'memory.preference_apply_event'::regclass,
                  'memory.project_knowledge_head'::regclass,
                  'memory.project_knowledge_revision'::regclass,
                  'memory.project_knowledge_revision_evidence'::regclass,
                  'memory.project_knowledge_apply_event'::regclass
                ]) AND tgname='specialized_append_only_guard'
                  AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
        "controlled_insert_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=3
                FROM pg_trigger
                WHERE (
                  (tgrelid='memory.user_preference'::regclass
                   AND tgname='user_preference_controlled_write_guard')
                  OR (tgrelid='memory.preference_revision'::regclass
                      AND tgname='preference_revision_insert_guard')
                  OR (tgrelid='memory.project_knowledge_revision'::regclass
                      AND tgname='project_revision_insert_guard')
                ) AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
        "revision_active_evidence_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=2
                FROM pg_trigger
                WHERE (
                  (tgrelid='memory.preference_revision_evidence'::regclass
                   AND tgname='preference_revision_active_evidence_guard')
                  OR (tgrelid='memory.project_knowledge_revision_evidence'::regclass
                      AND tgname='project_revision_active_evidence_guard')
                ) AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
    }


async def _owner_counts(
    conn: asyncpg.Connection, manifest: DurableApplyManifest
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
          (SELECT count(*) FROM memory.preference_revision_evidence WHERE owner_user_id=$1) AS preference_revision_links,
          (SELECT count(*) FROM memory.preference_apply_event WHERE owner_user_id=$1) AS preference_apply_events,
          (SELECT count(*) FROM memory.project_space WHERE owner_user_id=$1 AND project_key=$2) AS project_spaces,
          (SELECT count(*) FROM memory.project_space_registration_event WHERE owner_user_id=$1) AS project_registration_events,
          (SELECT count(*) FROM memory.project_knowledge_candidate WHERE owner_user_id=$1) AS project_candidates,
          (SELECT count(*) FROM memory.project_knowledge_candidate_evidence WHERE owner_user_id=$1) AS project_links,
          (SELECT count(*) FROM memory.project_knowledge_candidate_review WHERE owner_user_id=$1) AS project_reviews,
          (SELECT count(*) FROM memory.project_knowledge_candidate_review_replacement WHERE owner_user_id=$1) AS project_replacements,
          (SELECT count(*) FROM memory.project_knowledge_head WHERE owner_user_id=$1) AS durable_project_heads,
          (SELECT count(*) FROM memory.project_knowledge_revision WHERE owner_user_id=$1) AS project_revisions,
          (SELECT count(*) FROM memory.project_knowledge_revision_evidence WHERE owner_user_id=$1) AS project_revision_links,
          (SELECT count(*) FROM memory.project_knowledge_relation WHERE owner_user_id=$1) AS project_relations,
          (SELECT count(*) FROM memory.project_knowledge_apply_event WHERE owner_user_id=$1) AS project_apply_events
        """,
        manifest.owner_user_id,
        manifest.project_key,
    )
    return {key: int(value) for key, value in dict(row).items()}


async def _fetch_source_rows(
    conn: asyncpg.Connection, manifest: DurableApplyManifest
) -> Dict[str, list[Dict[str, Any]]]:
    preference_ids = [item.candidate_id for item in manifest.preference_applies]
    preference_review_ids = [item.accepted_review_id for item in manifest.preference_applies]
    project_ids = [item.candidate_id for item in manifest.project_applies]
    project_review_ids = [item.accepted_review_id for item in manifest.project_applies]
    preference_rows = await conn.fetch(
        """
        SELECT c.owner_user_id, c.candidate_id, c.candidate_hash,
               c.preference_key AS head_key, r.review_id AS accepted_review_id,
               r.request_id AS review_request_id, r.review_number, r.decision,
               NOT EXISTS (
                 SELECT 1 FROM memory.preference_candidate_review newer
                 WHERE newer.owner_user_id=r.owner_user_id
                   AND newer.candidate_id=r.candidate_id
                   AND newer.review_number>r.review_number
               ) AS latest_review,
               count(l.evidence_id)::integer AS evidence_links,
               count(l.evidence_id) FILTER (WHERE e.status='active')::integer AS active_links,
               h.preference_id AS existing_head_id,
               h.current_revision_id AS current_revision_id
        FROM memory.preference_candidate c
        JOIN memory.preference_candidate_review r
          ON r.owner_user_id=c.owner_user_id AND r.candidate_id=c.candidate_id
         AND r.review_id=ANY($3::uuid[])
        LEFT JOIN memory.preference_candidate_evidence l
          ON l.owner_user_id=c.owner_user_id AND l.candidate_id=c.candidate_id
        LEFT JOIN memory.evidence e
          ON e.owner_user_id=l.owner_user_id AND e.evidence_id=l.evidence_id
        LEFT JOIN memory.user_preference h
          ON h.owner_user_id=c.owner_user_id AND h.preference_key=c.preference_key
        WHERE c.owner_user_id=$1 AND c.candidate_id=ANY($2::uuid[])
        GROUP BY c.owner_user_id,c.candidate_id,c.candidate_hash,c.preference_key,
                 r.review_id,r.request_id,r.review_number,r.decision,h.preference_id,
                 h.current_revision_id
        ORDER BY array_position($2::uuid[],c.candidate_id)
        """,
        manifest.owner_user_id,
        preference_ids,
        preference_review_ids,
    )
    project_rows = await conn.fetch(
        """
        SELECT c.owner_user_id, c.project_id, c.candidate_id, c.candidate_hash,
               c.knowledge_key AS head_key, c.knowledge_kind, c.document_state,
               c.authority_level, r.review_id AS accepted_review_id,
               r.request_id AS review_request_id, r.review_number, r.decision,
               NOT EXISTS (
                 SELECT 1 FROM memory.project_knowledge_candidate_review newer
                 WHERE newer.owner_user_id=r.owner_user_id
                   AND newer.project_id=r.project_id
                   AND newer.candidate_id=r.candidate_id
                   AND newer.review_number>r.review_number
               ) AS latest_review,
               count(l.evidence_id)::integer AS evidence_links,
               count(l.evidence_id) FILTER (WHERE e.status='active')::integer AS active_links,
               h.knowledge_id AS existing_head_id,
               latest.revision_id AS current_revision_id
        FROM memory.project_knowledge_candidate c
        JOIN memory.project_knowledge_candidate_review r
          ON r.owner_user_id=c.owner_user_id AND r.project_id=c.project_id
         AND r.candidate_id=c.candidate_id AND r.review_id=ANY($4::uuid[])
        LEFT JOIN memory.project_knowledge_candidate_evidence l
          ON l.owner_user_id=c.owner_user_id AND l.project_id=c.project_id
         AND l.candidate_id=c.candidate_id
        LEFT JOIN memory.evidence e
          ON e.owner_user_id=l.owner_user_id AND e.evidence_id=l.evidence_id
        LEFT JOIN memory.project_knowledge_head h
          ON h.owner_user_id=c.owner_user_id AND h.project_id=c.project_id
         AND h.knowledge_key=c.knowledge_key
        LEFT JOIN LATERAL (
          SELECT pr.revision_id
          FROM memory.project_knowledge_revision pr
          WHERE pr.owner_user_id=h.owner_user_id AND pr.project_id=h.project_id
            AND pr.knowledge_id=h.knowledge_id
          ORDER BY pr.revision_number DESC LIMIT 1
        ) latest ON true
        WHERE c.owner_user_id=$1 AND c.project_id=$2
          AND c.candidate_id=ANY($3::uuid[])
        GROUP BY c.owner_user_id,c.project_id,c.candidate_id,c.candidate_hash,
                 c.knowledge_key,c.knowledge_kind,c.document_state,c.authority_level,
                 r.review_id,r.request_id,r.review_number,r.decision,h.knowledge_id,
                 latest.revision_id
        ORDER BY array_position($3::uuid[],c.candidate_id)
        """,
        manifest.owner_user_id,
        manifest.project_id,
        project_ids,
        project_review_ids,
    )
    return {
        "preference": [dict(row) for row in preference_rows],
        "project": [dict(row) for row in project_rows],
    }


def _verify_source_rows(
    manifest: DurableApplyManifest,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    require_no_heads: bool,
) -> None:
    lanes = (
        ("preference", manifest.preference_applies),
        ("project", manifest.project_applies),
    )
    for lane, items in lanes:
        if len(rows[lane]) != len(items):
            raise PreferenceProjectDurableApplyError(
                f"{lane} accepted source set is incomplete"
            )
        for stored, item in zip(rows[lane], items):
            value = _normalize(stored)
            expected = {
                "owner_user_id": str(manifest.owner_user_id),
                "candidate_id": str(item.candidate_id),
                "candidate_hash": item.candidate_hash,
                "head_key": item.head_key,
                "accepted_review_id": str(item.accepted_review_id),
                "review_request_id": str(item.review_request_id),
                "review_number": 1,
                "decision": "accept",
                "latest_review": True,
                "evidence_links": 1,
                "active_links": 1,
            }
            if lane == "project":
                expected.update(
                    {
                        "project_id": str(manifest.project_id),
                        "knowledge_kind": item.knowledge_kind,
                    }
                )
                if value.get("document_state") == "unverified" or value.get(
                    "authority_level"
                ) == "unverified":
                    raise PreferenceProjectDurableApplyError(
                        "unverified project candidate reached durable apply"
                    )
            for field, expected_value in expected.items():
                if value.get(field) != expected_value:
                    raise PreferenceProjectDurableApplyError(
                        f"{lane} durable source changed: {field}"
                    )
            if require_no_heads:
                if value.get("existing_head_id") is not None:
                    raise PreferenceProjectDurableApplyError(
                        f"unexpected existing {lane} durable head"
                    )
                if value.get("current_revision_id") is not None:
                    raise PreferenceProjectDurableApplyError(
                        f"unexpected existing {lane} revision"
                    )
            elif value.get("current_revision_id") is None:
                raise PreferenceProjectDurableApplyError(
                    f"applied {lane} revision is missing"
                )


async def _fetch_apply_state(
    conn: asyncpg.Connection, manifest: DurableApplyManifest
) -> Dict[str, list[Dict[str, Any]]]:
    preference_requests = [item.apply_request_id for item in manifest.preference_applies]
    project_requests = [item.apply_request_id for item in manifest.project_applies]
    preferences = await conn.fetch(
        """
        SELECT e.event_id,e.owner_user_id,e.request_id,e.request_sha256,
               e.candidate_id,e.accepted_review_id,e.preference_id,
               e.prior_revision_id,e.resulting_revision_id,e.outcome,e.metadata,
               h.preference_key AS head_key,h.current_revision_id,
               h.revision_number AS head_revision_number,h.content_sha256 AS head_hash,
               h.accepted_review_id AS head_review_id,r.revision_number,
               r.candidate_id AS revision_candidate_id,
               r.accepted_review_id AS revision_review_id,
               r.preference_key AS revision_head_key,r.content_sha256 AS revision_hash,
               r.prior_revision_id AS revision_prior_revision_id,
               (SELECT count(*) FROM memory.preference_revision_evidence link
                WHERE link.owner_user_id=e.owner_user_id
                  AND link.revision_id=e.resulting_revision_id) AS revision_links
        FROM memory.preference_apply_event e
        JOIN memory.user_preference h
          ON h.owner_user_id=e.owner_user_id AND h.preference_id=e.preference_id
        JOIN memory.preference_revision r
          ON r.owner_user_id=e.owner_user_id AND r.revision_id=e.resulting_revision_id
        WHERE e.owner_user_id=$1 AND e.request_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[],e.request_id)
        """,
        manifest.owner_user_id,
        preference_requests,
    )
    projects = await conn.fetch(
        """
        SELECT e.event_id,e.owner_user_id,e.project_id,e.request_id,e.request_sha256,
               e.candidate_id,e.accepted_review_id,e.knowledge_id,
               e.prior_revision_id,e.resulting_revision_id,e.outcome,e.metadata,
               h.knowledge_key AS head_key,h.knowledge_kind,
               r.revision_number,r.content_sha256 AS revision_hash,
               r.accepted_review_id AS revision_review_id,
               r.supersedes_revision_id AS revision_prior_revision_id,
               (SELECT count(*) FROM memory.project_knowledge_revision_evidence link
                WHERE link.owner_user_id=e.owner_user_id AND link.project_id=e.project_id
                  AND link.revision_id=e.resulting_revision_id) AS revision_links,
               (SELECT latest.revision_id
                FROM memory.project_knowledge_revision latest
                WHERE latest.owner_user_id=e.owner_user_id
                  AND latest.project_id=e.project_id
                  AND latest.knowledge_id=e.knowledge_id
                ORDER BY latest.revision_number DESC LIMIT 1) AS current_revision_id
        FROM memory.project_knowledge_apply_event e
        JOIN memory.project_knowledge_head h
          ON h.owner_user_id=e.owner_user_id AND h.project_id=e.project_id
         AND h.knowledge_id=e.knowledge_id
        JOIN memory.project_knowledge_revision r
          ON r.owner_user_id=e.owner_user_id AND r.project_id=e.project_id
         AND r.revision_id=e.resulting_revision_id
        WHERE e.owner_user_id=$1 AND e.project_id=$2
          AND e.request_id=ANY($3::uuid[])
        ORDER BY array_position($3::uuid[],e.request_id)
        """,
        manifest.owner_user_id,
        manifest.project_id,
        project_requests,
    )
    return {
        "preference": [_stored_row(row, "metadata") for row in preferences],
        "project": [_stored_row(row, "metadata") for row in projects],
    }


def _verify_applied_state(
    manifest: DurableApplyManifest,
    state: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, str]:
    event_ids: Dict[str, str] = {}
    for lane, items in (
        ("preference", manifest.preference_applies),
        ("project", manifest.project_applies),
    ):
        if len(state[lane]) != len(items):
            raise PreferenceProjectDurableApplyError(
                f"stored {lane} durable apply set is incomplete"
            )
        for stored, item in zip(state[lane], items):
            value = _normalize(stored)
            expected = {
                "owner_user_id": str(manifest.owner_user_id),
                "request_id": str(item.apply_request_id),
                "candidate_id": str(item.candidate_id),
                "accepted_review_id": str(item.accepted_review_id),
                "prior_revision_id": None,
                "outcome": "applied",
                "metadata": item.event_metadata,
                "head_key": item.head_key,
                "revision_number": 1,
                "revision_hash": item.candidate_hash,
                "revision_review_id": str(item.accepted_review_id),
                "revision_prior_revision_id": None,
                "revision_links": 1,
            }
            if lane == "preference":
                expected.update(
                    {
                        "head_revision_number": 1,
                        "head_hash": item.candidate_hash,
                        "head_review_id": str(item.accepted_review_id),
                        "revision_candidate_id": str(item.candidate_id),
                        "revision_head_key": item.head_key,
                    }
                )
            else:
                expected.update(
                    {
                        "project_id": str(manifest.project_id),
                        "knowledge_kind": item.knowledge_kind,
                    }
                )
            for field, expected_value in expected.items():
                if value.get(field) != expected_value:
                    raise PreferenceProjectDurableApplyError(
                        f"stored {lane} durable apply differs: {field}"
                    )
            if value.get("current_revision_id") != value.get("resulting_revision_id"):
                raise PreferenceProjectDurableApplyError(
                    f"{lane} current revision does not match apply event"
                )
            if not SHA256_RE.fullmatch(str(value.get("request_sha256", ""))):
                raise PreferenceProjectDurableApplyError(
                    f"{lane} apply request hash is invalid"
                )
            event_ids[str(item.candidate_id)] = str(
                _uuid(value.get("event_id"), "event_id")
            )
    return event_ids


async def _call_apply_function(
    conn: asyncpg.Connection,
    item: DurableApplyItem,
    lane: str,
) -> uuid.UUID:
    function = (
        "memory.apply_preference_candidate"
        if lane == "preference"
        else "memory.apply_project_candidate"
    )
    row = await conn.fetchrow(
        f"""
        SELECT event_id
        FROM {function}($1,$2,$3,$4::jsonb)
        """,
        item.accepted_review_id,
        item.apply_request_id,
        item.expected_current_revision_id,
        _stable_json(item.event_metadata),
    )
    if row is None:
        raise PreferenceProjectDurableApplyError("controlled apply returned no event")
    return _uuid(row["event_id"], "event_id")


def _verify_initial_counts(
    manifest: DurableApplyManifest, counts: Mapping[str, int]
) -> None:
    durable_total = sum(counts[field] for field in DURABLE_COUNT_FIELDS)
    if durable_total != manifest.expected["existing_durable_records"]:
        raise PreferenceProjectDurableApplyError(
            f"initial durable state changed: {durable_total} records"
        )
    if counts["preference_reviews"] != manifest.expected["reviewed_preference_candidates"]:
        raise PreferenceProjectDurableApplyError("preference review total changed")
    if counts["project_reviews"] != manifest.expected["reviewed_project_candidates"]:
        raise PreferenceProjectDurableApplyError("project review total changed")
    if counts["preference_replacements"] or counts["project_replacements"]:
        raise PreferenceProjectDurableApplyError("review replacements are no longer zero")


def _expected_deltas(manifest: DurableApplyManifest) -> Dict[str, int]:
    return {
        "durable_preferences": manifest.expected["preference_head_inserts"],
        "preference_revisions": manifest.expected["preference_revision_inserts"],
        "preference_revision_links": manifest.expected[
            "preference_revision_evidence_inserts"
        ],
        "preference_apply_events": manifest.expected[
            "preference_apply_event_inserts"
        ],
        "durable_project_heads": manifest.expected["project_head_inserts"],
        "project_revisions": manifest.expected["project_revision_inserts"],
        "project_revision_links": manifest.expected[
            "project_revision_evidence_inserts"
        ],
        "project_apply_events": manifest.expected["project_apply_event_inserts"],
    }


async def run_controlled_preference_project_durable_apply(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
    review_manifest_path: str | Path,
    review_apply_report_path: str | Path,
    review_replay_report_path: str | Path,
    apply: bool = False,
    confirmation: str = "",
) -> Dict[str, Any]:
    manifest = load_durable_apply_manifest(manifest_path)
    _verify_source_review_reports(
        manifest,
        review_manifest_path,
        review_apply_report_path,
        review_replay_report_path,
    )
    if not apply and not manifest.dry_run_authorized:
        raise PreferenceProjectDurableApplyError("manifest does not authorize dry run")
    if apply:
        if not manifest.apply_authorized:
            raise PreferenceProjectDurableApplyError(
                "manifest is dry-run-only; durable apply is not authorized"
            )
        if confirmation != CONFIRMATION:
            raise PreferenceProjectDurableApplyError(
                f"confirmation must equal {CONFIRMATION}"
            )

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
            raise PreferenceProjectDurableApplyError(
                f"database safety controls failed: {controls}"
            )
        before = await _owner_counts(conn, manifest)
        source_rows = await _fetch_source_rows(conn, manifest)
        apply_state = await _fetch_apply_state(conn, manifest)
        applied_count = len(apply_state["preference"]) + len(apply_state["project"])
        expected_applied = len(manifest.preference_applies) + len(manifest.project_applies)
        if applied_count not in {0, expected_applied}:
            raise PreferenceProjectDurableApplyError("partial durable apply state detected")

        if applied_count == expected_applied:
            _verify_source_rows(manifest, source_rows, require_no_heads=False)
            event_ids = _verify_applied_state(manifest, apply_state)
            expected_totals = _expected_deltas(manifest)
            for field, expected in expected_totals.items():
                if before[field] != expected:
                    raise PreferenceProjectDurableApplyError(
                        f"applied durable count changed: {field}"
                    )
            if apply:
                returned_ids: Dict[str, str] = {}
                for item in manifest.preference_applies:
                    returned_ids[str(item.candidate_id)] = str(
                        await _call_apply_function(conn, item, "preference")
                    )
                for item in manifest.project_applies:
                    returned_ids[str(item.candidate_id)] = str(
                        await _call_apply_function(conn, item, "project")
                    )
                if returned_ids != event_ids:
                    raise PreferenceProjectDurableApplyError(
                        "durable replay event identities changed"
                    )
            after_replay = await _owner_counts(conn, manifest)
            if after_replay != before:
                raise PreferenceProjectDurableApplyError(
                    "durable replay changed database counts"
                )
            return {
                "status": "verified_replay",
                "owner_user_id": str(manifest.owner_user_id),
                "project_id": str(manifest.project_id),
                "project_key": manifest.project_key,
                "preference_apply_events": len(manifest.preference_applies),
                "project_apply_events": len(manifest.project_applies),
                "event_ids": event_ids,
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "retrieval_activation": False,
                "controls": controls,
            }

        _verify_initial_counts(manifest, before)
        _verify_source_rows(manifest, source_rows, require_no_heads=True)
        if not apply:
            return {
                "status": "dry_run_verified",
                "dry_run_authorized": manifest.dry_run_authorized,
                "apply_authorized": manifest.apply_authorized,
                "ready_for_separate_apply_authorization": True,
                "owner_user_id": str(manifest.owner_user_id),
                "project_id": str(manifest.project_id),
                "project_key": manifest.project_key,
                "preference_candidates_ready": len(manifest.preference_applies),
                "project_candidates_ready": len(manifest.project_applies),
                "expected_current_revision_ids": None,
                "projected_database_writes": manifest.expected[
                    "database_writes_if_authorized"
                ],
                "projected_rows": _expected_deltas(manifest),
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "retrieval_activation": False,
                "controls": controls,
            }

        returned_ids: Dict[str, str] = {}
        for item in manifest.preference_applies:
            returned_ids[str(item.candidate_id)] = str(
                await _call_apply_function(conn, item, "preference")
            )
        for item in manifest.project_applies:
            returned_ids[str(item.candidate_id)] = str(
                await _call_apply_function(conn, item, "project")
            )
        after_state = await _fetch_apply_state(conn, manifest)
        verified_ids = _verify_applied_state(manifest, after_state)
        if verified_ids != returned_ids:
            raise PreferenceProjectDurableApplyError(
                "stored apply events differ from function results"
            )
        after = await _owner_counts(conn, manifest)
        deltas = {field: after[field] - before[field] for field in after}
        expected_deltas = _expected_deltas(manifest)
        for field, expected in expected_deltas.items():
            if deltas[field] != expected:
                raise PreferenceProjectDurableApplyError(
                    f"authorized durable delta changed: {field}"
                )
        changed_elsewhere = {
            field: delta
            for field, delta in deltas.items()
            if field not in expected_deltas and delta != 0
        }
        if changed_elsewhere:
            raise PreferenceProjectDurableApplyError(
                f"unauthorized downstream write detected: {changed_elsewhere}"
            )
        return {
            "status": "committed",
            "owner_user_id": str(manifest.owner_user_id),
            "project_id": str(manifest.project_id),
            "project_key": manifest.project_key,
            "preference_heads_inserted": manifest.expected[
                "preference_head_inserts"
            ],
            "preference_revisions_inserted": manifest.expected[
                "preference_revision_inserts"
            ],
            "project_heads_inserted": manifest.expected["project_head_inserts"],
            "project_revisions_inserted": manifest.expected[
                "project_revision_inserts"
            ],
            "event_ids": verified_ids,
            "authorization_manifest_sha256": manifest.sha256,
            "database_writes": manifest.expected["database_writes_if_authorized"],
            "retrieval_activation": False,
            "before": before,
            "after": after,
            "controls": controls,
        }
