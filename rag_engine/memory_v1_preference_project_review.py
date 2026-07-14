from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import asyncpg

from .memory_v1_preference_project_apply import (
    PreferenceProjectApplyManifest,
    _fetch_stored_state,
    _load_locked_inputs,
    _normalize,
    _owner_counts,
    _stored_row,
    _verify_source_evidence,
    _verify_stored_state,
    load_preference_project_apply_manifest,
)
from .memory_v1_preference_project_extraction import NEW_TABLES, ExtractionManifest


MANIFEST_VERSION = "memory_v1_preference_project_review_authorization_20260714_v1"
CONFIRMATION = "REVIEW_MEMORY_V1_8_PREFERENCE_PROJECT_CANDIDATES"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CONTRACT = {
    "allowed_writes": [
        "memory.preference_candidate_review:INSERT_VIA_CONTROLLED_FUNCTION",
        "memory.project_knowledge_candidate_review:INSERT_VIA_CONTROLLED_FUNCTION",
    ],
    "review_records": 8,
    "review_replacement_records": 0,
    "candidate_writes": 0,
    "durable_preference_writes": 0,
    "durable_project_writes": 0,
    "claim_writes": 0,
    "evidence_writes": 0,
    "projection_outbox_writes": 0,
    "qdrant_writes": 0,
    "prompt_injection": False,
}


class PreferenceProjectReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewDecision:
    candidate_id: uuid.UUID
    candidate_hash: str
    request_id: uuid.UUID
    decision: str
    rationale: str
    reason_codes: tuple[str, ...]
    replacement_candidate_ids: tuple[uuid.UUID, ...]
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class PreferenceProjectReviewManifest:
    path: Path
    sha256: str
    owner_user_id: uuid.UUID
    project_id: uuid.UUID
    project_key: str
    review_authorized: bool
    candidate_apply_manifest_sha256: str
    candidate_apply_report_sha256: str
    reviewer_type: str
    reviewer_ref: str
    expected: Dict[str, int]
    preference_reviews: tuple[ReviewDecision, ...]
    project_reviews: tuple[ReviewDecision, ...]
    controls: Dict[str, Any]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _required_text(value: Any, field: str, *, max_length: int = 16000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreferenceProjectReviewError(f"{field} is required")
    if len(text) > max_length:
        raise PreferenceProjectReviewError(f"{field} exceeds {max_length} characters")
    return text


def _required_sha(value: Any, field: str) -> str:
    text = _required_text(value, field, max_length=64)
    if not SHA256_RE.fullmatch(text):
        raise PreferenceProjectReviewError(f"{field} must be lowercase SHA-256 hex")
    return text


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        result = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PreferenceProjectReviewError(f"{field} must be a UUID") from exc
    if result.int == 0:
        raise PreferenceProjectReviewError(f"{field} must be non-nil")
    return result


def _object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreferenceProjectReviewError(f"{field} must be an object")
    return dict(value)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PreferenceProjectReviewError(f"{field} must be a non-negative integer")
    return value


def _load_review_decisions(value: Any, field: str) -> tuple[ReviewDecision, ...]:
    if not isinstance(value, list):
        raise PreferenceProjectReviewError(f"{field} must be an array")
    decisions: list[ReviewDecision] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{field}[{index}]")
        required_fields = {
            "candidate_id",
            "candidate_hash",
            "request_id",
            "decision",
            "rationale",
            "reason_codes",
            "replacement_candidate_ids",
            "metadata",
        }
        if set(item) != required_fields:
            raise PreferenceProjectReviewError(f"{field}[{index}] field contract changed")
        if item.get("decision") != "accept":
            raise PreferenceProjectReviewError("this authorization permits accept decisions only")
        reason_codes_raw = item.get("reason_codes")
        if not isinstance(reason_codes_raw, list) or not reason_codes_raw:
            raise PreferenceProjectReviewError(f"{field}[{index}].reason_codes is required")
        reason_codes = tuple(
            _required_text(code, f"{field}[{index}].reason_codes", max_length=500)
            for code in reason_codes_raw
        )
        if tuple(sorted(set(reason_codes))) != reason_codes:
            raise PreferenceProjectReviewError("reason_codes must be sorted and unique")
        replacements_raw = item.get("replacement_candidate_ids")
        if replacements_raw != []:
            raise PreferenceProjectReviewError("accept decisions cannot include replacements")
        metadata = _object(item.get("metadata"), f"{field}[{index}].metadata")
        expected_metadata = {
            "authorization_source": "user_instruction_continue_controlled_review_20260713",
            "durable_apply_authorized": False,
            "review_contract": "memory_v1_controlled_review_v1",
        }
        if metadata != expected_metadata:
            raise PreferenceProjectReviewError("review metadata contract changed")
        decisions.append(
            ReviewDecision(
                candidate_id=_uuid(item.get("candidate_id"), "candidate_id"),
                candidate_hash=_required_sha(item.get("candidate_hash"), "candidate_hash"),
                request_id=_uuid(item.get("request_id"), "request_id"),
                decision="accept",
                rationale=_required_text(item.get("rationale"), "rationale"),
                reason_codes=reason_codes,
                replacement_candidate_ids=(),
                metadata=metadata,
            )
        )
    candidate_ids = [item.candidate_id for item in decisions]
    request_ids = [item.request_id for item in decisions]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise PreferenceProjectReviewError(f"{field} contains duplicate candidates")
    if len(request_ids) != len(set(request_ids)):
        raise PreferenceProjectReviewError(f"{field} contains duplicate request IDs")
    return tuple(decisions)


def load_preference_project_review_manifest(
    path: str | Path,
) -> PreferenceProjectReviewManifest:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        root = _object(json.loads(raw_bytes), "review authorization manifest")
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectReviewError(f"cannot read review manifest: {resolved}") from exc
    if root.get("manifest_version") != MANIFEST_VERSION:
        raise PreferenceProjectReviewError("unsupported review manifest_version")
    if not isinstance(root.get("review_authorized"), bool):
        raise PreferenceProjectReviewError("review_authorized must be boolean")
    if root.get("reviewer_type") != "user":
        raise PreferenceProjectReviewError("controlled accept review must be attributed to user")
    controls = _object(root.get("controls"), "controls")
    if controls != CONTROL_CONTRACT:
        raise PreferenceProjectReviewError("review control contract changed")
    expected_raw = _object(root.get("expected"), "expected")
    expected_fields = {
        "preference_reviews",
        "project_reviews",
        "review_replacements",
        "durable_preferences",
        "durable_project_heads",
        "durable_preference_revisions",
        "durable_project_revisions",
        "preference_apply_events",
        "project_apply_events",
    }
    if set(expected_raw) != expected_fields:
        raise PreferenceProjectReviewError("review expected-count contract changed")
    expected = {
        field: _nonnegative_int(expected_raw[field], f"expected.{field}")
        for field in expected_fields
    }
    preferences = _load_review_decisions(root.get("preference_reviews"), "preference_reviews")
    projects = _load_review_decisions(root.get("project_reviews"), "project_reviews")
    if len(preferences) != expected["preference_reviews"] or len(projects) != expected["project_reviews"]:
        raise PreferenceProjectReviewError("review decision counts changed")
    if len(preferences) + len(projects) != controls["review_records"]:
        raise PreferenceProjectReviewError("authorized review total changed")
    zero_fields = expected_fields - {"preference_reviews", "project_reviews"}
    if any(expected[field] != 0 for field in zero_fields):
        raise PreferenceProjectReviewError("replacement and durable counts must remain zero")
    all_requests = [item.request_id for item in [*preferences, *projects]]
    if len(all_requests) != len(set(all_requests)):
        raise PreferenceProjectReviewError("request IDs must be unique across both lanes")
    owner = _uuid(root.get("owner_user_id"), "owner_user_id")
    reviewer_ref = _required_text(root.get("reviewer_ref"), "reviewer_ref", max_length=500)
    if reviewer_ref != str(owner):
        raise PreferenceProjectReviewError("user reviewer_ref must equal owner_user_id")
    return PreferenceProjectReviewManifest(
        path=resolved,
        sha256=_sha256_bytes(raw_bytes),
        owner_user_id=owner,
        project_id=_uuid(root.get("project_id"), "project_id"),
        project_key=_required_text(root.get("project_key"), "project_key", max_length=500),
        review_authorized=root["review_authorized"],
        candidate_apply_manifest_sha256=_required_sha(
            root.get("candidate_apply_manifest_sha256"), "candidate_apply_manifest_sha256"
        ),
        candidate_apply_report_sha256=_required_sha(
            root.get("candidate_apply_report_sha256"), "candidate_apply_report_sha256"
        ),
        reviewer_type="user",
        reviewer_ref=reviewer_ref,
        expected=expected,
        preference_reviews=preferences,
        project_reviews=projects,
        controls=controls,
    )


def _load_candidate_inputs(
    manifest: PreferenceProjectReviewManifest,
    candidate_apply_manifest_path: str | Path,
    candidate_apply_report_path: str | Path,
    extraction_manifest_path: str | Path,
    extraction_report_path: str | Path,
) -> tuple[PreferenceProjectApplyManifest, ExtractionManifest, Dict[str, Any]]:
    candidate_apply_path = Path(candidate_apply_manifest_path).resolve()
    if _sha256_bytes(candidate_apply_path.read_bytes()) != manifest.candidate_apply_manifest_sha256:
        raise PreferenceProjectReviewError("candidate apply manifest SHA-256 changed")
    candidate_apply = load_preference_project_apply_manifest(candidate_apply_path)
    extraction, extraction_report = _load_locked_inputs(
        candidate_apply, extraction_manifest_path, extraction_report_path
    )
    if candidate_apply.owner_user_id != manifest.owner_user_id:
        raise PreferenceProjectReviewError("candidate apply owner changed")
    if candidate_apply.project_key != manifest.project_key:
        raise PreferenceProjectReviewError("candidate apply project key changed")

    apply_report_path = Path(candidate_apply_report_path).resolve()
    try:
        apply_report_bytes = apply_report_path.read_bytes()
        apply_report = _object(json.loads(apply_report_bytes), "candidate apply report")
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectReviewError("cannot read candidate apply report") from exc
    if _sha256_bytes(apply_report_bytes) != manifest.candidate_apply_report_sha256:
        raise PreferenceProjectReviewError("candidate apply report SHA-256 changed")
    expected_apply_report = {
        "status": "committed",
        "authorization_manifest_sha256": manifest.candidate_apply_manifest_sha256,
        "owner_user_id": str(manifest.owner_user_id),
        "project_id": str(manifest.project_id),
        "project_key": manifest.project_key,
        "preference_candidates_inserted": len(manifest.preference_reviews),
        "project_candidates_inserted": len(manifest.project_reviews),
        "candidate_evidence_links_inserted": len(manifest.preference_reviews)
        + len(manifest.project_reviews),
        "reviews": 0,
        "durable_records": 0,
        "database_writes": 18,
    }
    for field, expected in expected_apply_report.items():
        if apply_report.get(field) != expected:
            raise PreferenceProjectReviewError(f"candidate apply report changed: {field}")
    locked_preferences = [
        (str(item.candidate_id), item.candidate_hash) for item in candidate_apply.preference_candidates
    ]
    review_preferences = [
        (str(item.candidate_id), item.candidate_hash) for item in manifest.preference_reviews
    ]
    locked_projects = [
        (str(item.candidate_id), item.candidate_hash) for item in candidate_apply.project_candidates
    ]
    review_projects = [
        (str(item.candidate_id), item.candidate_hash) for item in manifest.project_reviews
    ]
    if review_preferences != locked_preferences or review_projects != locked_projects:
        raise PreferenceProjectReviewError("review candidate locks differ from candidate apply")
    return candidate_apply, extraction, extraction_report


async def _audit_database_controls(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectReviewManifest,
    *,
    read_only: bool,
) -> Dict[str, bool]:
    controls = {
        "effective_role_brains_app": bool(await conn.fetchval("SELECT current_user='brains_app'")),
        "actor_context_exact": bool(
            await conn.fetchval("SELECT memory.current_actor_user_id()=$1", manifest.owner_user_id)
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
        "direct_review_insert_denied": bool(
            await conn.fetchval(
                """
                SELECT bool_and(NOT has_table_privilege(
                  current_user, format('memory.%I', name), 'INSERT'
                ))
                FROM unnest(ARRAY[
                  'preference_candidate_review','preference_candidate_review_replacement',
                  'project_knowledge_candidate_review',
                  'project_knowledge_candidate_review_replacement'
                ]) AS name
                """
            )
        ),
        "direct_durable_insert_denied": bool(
            await conn.fetchval(
                """
                SELECT bool_and(NOT has_table_privilege(
                  current_user, format('memory.%I', name), 'INSERT'
                ))
                FROM unnest(ARRAY[
                  'user_preference','preference_revision','preference_revision_evidence',
                  'preference_apply_event','project_knowledge_head',
                  'project_knowledge_revision','project_knowledge_revision_evidence',
                  'project_knowledge_apply_event'
                ]) AS name
                """
            )
        ),
        "review_execute_allowed": bool(
            await conn.fetchval(
                """
                SELECT has_function_privilege(current_user,
                  'memory.review_preference_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)',
                  'EXECUTE')
                  AND has_function_privilege(current_user,
                  'memory.review_project_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)',
                  'EXECUTE')
                """
            )
        ),
        "review_functions_hardened": bool(
            await conn.fetchval(
                """
                SELECT count(*)=2 AND bool_and(
                  p.prosecdef AND r.rolname='memory_review_maintainer'
                  AND p.proconfig @> ARRAY['search_path=pg_catalog','row_security=on']::text[]
                )
                FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                WHERE p.oid=ANY(ARRAY[
                  'memory.review_preference_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'::regprocedure,
                  'memory.review_project_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'::regprocedure
                ])
                """
            )
        ),
        "review_append_only_guards_live": bool(
            await conn.fetchval(
                """
                SELECT count(*)=4
                FROM pg_trigger
                WHERE tgrelid=ANY(ARRAY[
                  'memory.preference_candidate_review'::regclass,
                  'memory.preference_candidate_review_replacement'::regclass,
                  'memory.project_knowledge_candidate_review'::regclass,
                  'memory.project_knowledge_candidate_review_replacement'::regclass
                ]) AND tgname='specialized_append_only_guard'
                  AND NOT tgisinternal AND tgenabled <> 'D'
                """
            )
        ),
    }
    return controls


async def _fetch_target_reviews(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectReviewManifest,
) -> Dict[str, Any]:
    preference_ids = [item.candidate_id for item in manifest.preference_reviews]
    project_ids = [item.candidate_id for item in manifest.project_reviews]
    preference_rows = await conn.fetch(
        """
        SELECT review_id, owner_user_id, candidate_id, review_number, decision,
               expected_candidate_hash, reviewer_type, reviewer_ref, rationale,
               reason_codes, metadata, request_id, request_sha256
        FROM memory.preference_candidate_review
        WHERE owner_user_id=$1 AND candidate_id=ANY($2::uuid[])
        ORDER BY array_position($2::uuid[], candidate_id), review_number
        """,
        manifest.owner_user_id,
        preference_ids,
    )
    project_rows = await conn.fetch(
        """
        SELECT review_id, owner_user_id, project_id, candidate_id, review_number,
               decision, expected_candidate_hash, reviewer_type, reviewer_ref,
               rationale, reason_codes, metadata, request_id, request_sha256
        FROM memory.project_knowledge_candidate_review
        WHERE owner_user_id=$1 AND project_id=$2 AND candidate_id=ANY($3::uuid[])
        ORDER BY array_position($3::uuid[], candidate_id), review_number
        """,
        manifest.owner_user_id,
        manifest.project_id,
        project_ids,
    )
    return {
        "preference": [_stored_row(row, "metadata") for row in preference_rows],
        "project": [_stored_row(row, "metadata") for row in project_rows],
    }


def _verify_review_rows(
    manifest: PreferenceProjectReviewManifest,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, str]:
    if len(rows["preference"]) != len(manifest.preference_reviews):
        raise PreferenceProjectReviewError("stored preference review set is incomplete")
    if len(rows["project"]) != len(manifest.project_reviews):
        raise PreferenceProjectReviewError("stored project review set is incomplete")
    review_ids: Dict[str, str] = {}
    for lane, decisions in (
        ("preference", manifest.preference_reviews),
        ("project", manifest.project_reviews),
    ):
        for stored, decision in zip(rows[lane], decisions):
            normalized = _normalize(stored)
            expected = {
                "owner_user_id": str(manifest.owner_user_id),
                "candidate_id": str(decision.candidate_id),
                "review_number": 1,
                "decision": decision.decision,
                "expected_candidate_hash": decision.candidate_hash,
                "reviewer_type": manifest.reviewer_type,
                "reviewer_ref": manifest.reviewer_ref,
                "rationale": decision.rationale,
                "reason_codes": list(decision.reason_codes),
                "metadata": decision.metadata,
                "request_id": str(decision.request_id),
            }
            if lane == "project":
                expected["project_id"] = str(manifest.project_id)
            for field, expected_value in expected.items():
                if normalized.get(field) != expected_value:
                    raise PreferenceProjectReviewError(
                        f"stored {lane} review differs from authorization: {field}"
                    )
            if not SHA256_RE.fullmatch(str(normalized.get("request_sha256", ""))):
                raise PreferenceProjectReviewError("stored review request hash is invalid")
            review_id = str(_uuid(normalized.get("review_id"), "review_id"))
            review_ids[str(decision.candidate_id)] = review_id
    return review_ids


async def _call_review_function(
    conn: asyncpg.Connection,
    manifest: PreferenceProjectReviewManifest,
    decision: ReviewDecision,
    lane: str,
) -> uuid.UUID:
    function = (
        "memory.review_preference_candidate"
        if lane == "preference"
        else "memory.review_project_candidate"
    )
    row = await conn.fetchrow(
        f"""
        SELECT review_id
        FROM {function}(
          $1,$2,$3,$4,$5,$6,$7,$8::text[],$9::uuid[],$10::jsonb
        )
        """,
        decision.candidate_id,
        decision.request_id,
        decision.candidate_hash,
        decision.decision,
        manifest.reviewer_type,
        manifest.reviewer_ref,
        decision.rationale,
        list(decision.reason_codes),
        list(decision.replacement_candidate_ids),
        _stable_json(decision.metadata),
    )
    if row is None:
        raise PreferenceProjectReviewError("controlled review returned no row")
    return _uuid(row["review_id"], "review_id")


def _verify_zero_durable(counts: Mapping[str, int]) -> None:
    zero_fields = (
        "preference_replacements",
        "durable_preferences",
        "preference_revisions",
        "preference_apply_events",
        "project_replacements",
        "durable_project_heads",
        "project_revisions",
        "project_apply_events",
    )
    changed = {field: counts[field] for field in zero_fields if counts[field] != 0}
    if changed:
        raise PreferenceProjectReviewError(f"replacement or durable lanes are not zero: {changed}")


async def run_controlled_preference_project_review(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
    candidate_apply_manifest_path: str | Path,
    candidate_apply_report_path: str | Path,
    extraction_manifest_path: str | Path,
    extraction_report_path: str | Path,
    apply: bool = False,
    confirmation: str = "",
) -> Dict[str, Any]:
    manifest = load_preference_project_review_manifest(manifest_path)
    candidate_apply, extraction, extraction_report = _load_candidate_inputs(
        manifest,
        candidate_apply_manifest_path,
        candidate_apply_report_path,
        extraction_manifest_path,
        extraction_report_path,
    )
    if apply:
        if not manifest.review_authorized:
            raise PreferenceProjectReviewError("review manifest does not authorize review")
        if confirmation != CONFIRMATION:
            raise PreferenceProjectReviewError(f"confirmation must equal {CONFIRMATION}")

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
        controls = await _audit_database_controls(conn, manifest, read_only=not apply)
        if not all(controls.values()):
            raise PreferenceProjectReviewError(f"database safety controls failed: {controls}")
        await _verify_source_evidence(conn, candidate_apply, extraction_report)
        candidate_state = await _fetch_stored_state(conn, candidate_apply)
        project_id = _verify_stored_state(
            candidate_apply, extraction, extraction_report, candidate_state
        )
        if project_id != manifest.project_id:
            raise PreferenceProjectReviewError("stored project ID changed")
        before = await _owner_counts(conn, candidate_apply)
        _verify_zero_durable(before)
        target_rows = await _fetch_target_reviews(conn, manifest)
        target_count = len(target_rows["preference"]) + len(target_rows["project"])
        if target_count not in {0, manifest.controls["review_records"]}:
            raise PreferenceProjectReviewError("partial controlled review state detected")
        if target_count:
            review_ids = _verify_review_rows(manifest, target_rows)
            if before["preference_reviews"] != manifest.expected["preference_reviews"]:
                raise PreferenceProjectReviewError("preference review count changed")
            if before["project_reviews"] != manifest.expected["project_reviews"]:
                raise PreferenceProjectReviewError("project review count changed")
            if apply:
                for decision in manifest.preference_reviews:
                    returned = await _call_review_function(conn, manifest, decision, "preference")
                    if str(returned) != review_ids[str(decision.candidate_id)]:
                        raise PreferenceProjectReviewError("preference review replay identity changed")
                for decision in manifest.project_reviews:
                    returned = await _call_review_function(conn, manifest, decision, "project")
                    if str(returned) != review_ids[str(decision.candidate_id)]:
                        raise PreferenceProjectReviewError("project review replay identity changed")
            after_replay = await _owner_counts(conn, candidate_apply)
            if after_replay != before:
                raise PreferenceProjectReviewError("review replay changed database counts")
            return {
                "status": "verified_replay",
                "owner_user_id": str(manifest.owner_user_id),
                "project_id": str(manifest.project_id),
                "project_key": manifest.project_key,
                "preference_reviews": manifest.expected["preference_reviews"],
                "project_reviews": manifest.expected["project_reviews"],
                "review_ids": review_ids,
                "review_replacements": 0,
                "durable_records": 0,
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "controls": controls,
            }

        if before["preference_reviews"] != 0 or before["project_reviews"] != 0:
            raise PreferenceProjectReviewError("unexpected reviews exist outside target set")
        if not apply:
            return {
                "status": "preflight_verified",
                "review_authorized": manifest.review_authorized,
                "ready_for_review": manifest.review_authorized,
                "owner_user_id": str(manifest.owner_user_id),
                "project_id": str(manifest.project_id),
                "project_key": manifest.project_key,
                "preference_reviews": manifest.expected["preference_reviews"],
                "project_reviews": manifest.expected["project_reviews"],
                "review_replacements": 0,
                "durable_records": 0,
                "authorization_manifest_sha256": manifest.sha256,
                "database_writes": 0,
                "controls": controls,
            }

        returned_ids: Dict[str, str] = {}
        for decision in manifest.preference_reviews:
            review_id = await _call_review_function(conn, manifest, decision, "preference")
            returned_ids[str(decision.candidate_id)] = str(review_id)
        for decision in manifest.project_reviews:
            review_id = await _call_review_function(conn, manifest, decision, "project")
            returned_ids[str(decision.candidate_id)] = str(review_id)
        stored_reviews = await _fetch_target_reviews(conn, manifest)
        verified_ids = _verify_review_rows(manifest, stored_reviews)
        if verified_ids != returned_ids:
            raise PreferenceProjectReviewError("stored review identities differ from function results")
        after = await _owner_counts(conn, candidate_apply)
        _verify_zero_durable(after)
        deltas = {field: after[field] - before[field] for field in after}
        expected_deltas = {
            "preference_reviews": manifest.expected["preference_reviews"],
            "project_reviews": manifest.expected["project_reviews"],
        }
        for field, expected in expected_deltas.items():
            if deltas[field] != expected:
                raise PreferenceProjectReviewError(f"authorized review delta changed: {field}")
        changed_elsewhere = {
            field: delta
            for field, delta in deltas.items()
            if field not in expected_deltas and delta != 0
        }
        if changed_elsewhere:
            raise PreferenceProjectReviewError(
                f"unauthorized downstream write detected: {changed_elsewhere}"
            )
        return {
            "status": "committed",
            "owner_user_id": str(manifest.owner_user_id),
            "project_id": str(manifest.project_id),
            "project_key": manifest.project_key,
            "preference_reviews_inserted": manifest.expected["preference_reviews"],
            "project_reviews_inserted": manifest.expected["project_reviews"],
            "review_ids": verified_ids,
            "review_replacements": 0,
            "durable_records": 0,
            "authorization_manifest_sha256": manifest.sha256,
            "database_writes": manifest.controls["review_records"],
            "before": before,
            "after": after,
            "controls": controls,
        }
