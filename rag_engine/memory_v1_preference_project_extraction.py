from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import asyncpg


MANIFEST_VERSION = "memory_v1_preference_project_extraction_20260713_v1"
REPORT_MODE = "preference_project_extraction_dry_run_no_writes"
EXTRACTOR = "memory_v1_preference_project_extraction"
EXTRACTOR_VERSION = "20260713_v1"
CANDIDATE_NAMESPACE = uuid.UUID("45301861-5310-58d7-90fe-c9e5cfbd52d5")
REQUEST_NAMESPACE = uuid.UUID("c6b5aca1-995b-5a7c-8df3-d963f22bd665")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NEW_TABLES = (
    "preference_apply_event",
    "preference_candidate",
    "preference_candidate_evidence",
    "preference_candidate_review",
    "preference_candidate_review_replacement",
    "preference_revision",
    "preference_revision_evidence",
    "project_knowledge_apply_event",
    "project_knowledge_candidate",
    "project_knowledge_candidate_evidence",
    "project_knowledge_candidate_review",
    "project_knowledge_candidate_review_replacement",
    "project_knowledge_head",
    "project_knowledge_relation",
    "project_knowledge_revision",
    "project_knowledge_revision_evidence",
    "project_space",
    "project_space_registration_event",
)


class PreferenceProjectExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractionManifest:
    path: Path
    sha256: str
    owner_user_id: uuid.UUID
    batch_id: uuid.UUID
    source_review: Dict[str, Any]
    project_registration: Dict[str, Any]
    expected: Dict[str, int]
    controls: Dict[str, Any]
    mappings: tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class EvidenceState:
    evidence_id: uuid.UUID
    ledger_content_sha256: str
    content_sha256: str
    observed_at: datetime
    sensitivity: str
    status: str


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_stable_json(value).encode("utf-8"))


def _required_text(value: Any, field: str, *, max_length: int = 1000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreferenceProjectExtractionError(f"{field} is required")
    if len(text) > max_length:
        raise PreferenceProjectExtractionError(f"{field} exceeds {max_length} characters")
    return text


def _required_sha256(value: Any, field: str) -> str:
    text = _required_text(value, field, max_length=64)
    if not SHA256_RE.fullmatch(text):
        raise PreferenceProjectExtractionError(f"{field} must be a lowercase SHA-256")
    return text


def _object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreferenceProjectExtractionError(f"{field} must be an object")
    return dict(value)


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise PreferenceProjectExtractionError(f"{field} must be an array")
    return value


def _integer_map(value: Any, field: str) -> Dict[str, int]:
    raw = _object(value, field)
    result: Dict[str, int] = {}
    for key, count in raw.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PreferenceProjectExtractionError(f"{field}.{key} must be a non-negative integer")
        result[str(key)] = count
    return result


def _confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreferenceProjectExtractionError(f"{field} must be numeric")
    result = float(value)
    if result < 0 or result > 1:
        raise PreferenceProjectExtractionError(f"{field} must be between zero and one")
    return result


def _validate_controls(controls: Mapping[str, Any]) -> None:
    if controls.get("apply_authorized") is not False:
        raise PreferenceProjectExtractionError("manifest must explicitly prohibit apply")
    if controls.get("database_transaction") != "read_only":
        raise PreferenceProjectExtractionError("database transaction must be read_only")
    for field in (
        "candidate_writes",
        "review_writes",
        "durable_preference_writes",
        "durable_project_writes",
        "qdrant_writes",
    ):
        if controls.get(field) != 0:
            raise PreferenceProjectExtractionError(f"controls.{field} must be zero")
    for field in ("prompt_injection", "network_model_calls"):
        if controls.get(field) is not False:
            raise PreferenceProjectExtractionError(f"controls.{field} must be false")
    if controls.get("full_evidence_text_in_report") is not False:
        raise PreferenceProjectExtractionError("full evidence text must remain excluded")
    if controls.get("human_review_required_before_persistence") is not True:
        raise PreferenceProjectExtractionError("human review must be required")


def _validate_mapping(raw: Mapping[str, Any], index: int) -> Dict[str, Any]:
    item = dict(raw)
    evidence_id = _required_text(item.get("evidence_id"), f"mappings[{index}].evidence_id")
    uuid.UUID(evidence_id)
    target = _required_text(item.get("target"), f"mappings[{index}].target")
    if target not in {"preference", "project_knowledge"}:
        raise PreferenceProjectExtractionError(f"unsupported mapping target: {target}")
    item["evidence_id"] = evidence_id
    item["target"] = target
    item["extraction_confidence"] = _confidence(
        item.get("extraction_confidence"), f"mappings[{index}].extraction_confidence"
    )
    if target == "preference":
        if item.get("preference_class") not in {"response", "life"}:
            raise PreferenceProjectExtractionError("invalid preference_class")
        for field in ("preference_domain", "preference_key"):
            item[field] = _required_text(item.get(field), f"mappings[{index}].{field}")
        item["value_attributes"] = _object(
            item.get("value_attributes"), f"mappings[{index}].value_attributes"
        )
        item["scope"] = _object(item.get("scope"), f"mappings[{index}].scope")
        if item.get("polarity") not in {"prefer", "avoid", "require"}:
            raise PreferenceProjectExtractionError("invalid preference polarity")
        if item.get("explicit") is not True:
            raise PreferenceProjectExtractionError("reviewed preferences must remain explicit")
        if item.get("stability") not in {"tentative", "contextual", "stable"}:
            raise PreferenceProjectExtractionError("invalid preference stability")
        if item.get("surface_policy") not in {
            "silent_style_influence",
            "mention_when_relevant",
            "explicit_recall_only",
            "never_surface_as_content",
        }:
            raise PreferenceProjectExtractionError("invalid preference surface_policy")
    else:
        if item.get("knowledge_kind") not in {
            "architecture",
            "constraint",
            "decision",
            "requirement",
            "roadmap",
            "status",
        }:
            raise PreferenceProjectExtractionError("invalid knowledge_kind")
        item["knowledge_key"] = _required_text(
            item.get("knowledge_key"), f"mappings[{index}].knowledge_key"
        )
        if item.get("document_state") not in {
            "unverified",
            "working",
            "proposed",
            "ratified",
            "historical",
            "superseded",
        }:
            raise PreferenceProjectExtractionError("invalid document_state")
        if item.get("authority_level") not in {
            "unverified",
            "user_reported",
            "user_ratified",
            "approved_spec",
            "system_observed",
            "external_reference",
        }:
            raise PreferenceProjectExtractionError("invalid authority_level")
        if item.get("effective_at_source") not in {None, "evidence_observed_at"}:
            raise PreferenceProjectExtractionError("invalid effective_at_source")
        if item["knowledge_kind"] == "status" and item.get("effective_at_source") is None:
            raise PreferenceProjectExtractionError("status mappings require effective time")
    return item


def load_extraction_manifest(path: str | Path) -> ExtractionManifest:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectExtractionError(f"cannot read manifest: {resolved}") from exc
    root = _object(raw, "manifest")
    if root.get("manifest_version") != MANIFEST_VERSION:
        raise PreferenceProjectExtractionError("unsupported manifest_version")
    owner = uuid.UUID(_required_text(root.get("owner_user_id"), "owner_user_id"))
    batch = uuid.UUID(_required_text(root.get("batch_id"), "batch_id"))
    source_review = _object(root.get("source_review"), "source_review")
    for field in ("report_sha256", "review_file_sha256", "reviewed_input_sha256"):
        source_review[field] = _required_sha256(source_review.get(field), f"source_review.{field}")
    project_registration = _object(root.get("project_registration"), "project_registration")
    for field in ("project_key", "display_name"):
        project_registration[field] = _required_text(
            project_registration.get(field), f"project_registration.{field}", max_length=500
        )
    project_registration["metadata"] = _object(
        project_registration.get("metadata"), "project_registration.metadata"
    )
    expected = _integer_map(root.get("expected"), "expected")
    controls = _object(root.get("controls"), "controls")
    _validate_controls(controls)
    mappings = tuple(
        _validate_mapping(_object(item, f"mappings[{index}]"), index)
        for index, item in enumerate(_array(root.get("mappings"), "mappings"))
    )
    evidence_ids = [item["evidence_id"] for item in mappings]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise PreferenceProjectExtractionError("manifest contains duplicate evidence mappings")
    preference_count = sum(item["target"] == "preference" for item in mappings)
    project_count = sum(item["target"] == "project_knowledge" for item in mappings)
    if len(mappings) != expected.get("selected_review_units"):
        raise PreferenceProjectExtractionError("selected review-unit count differs from manifest")
    if preference_count != expected.get("preference_candidates"):
        raise PreferenceProjectExtractionError("preference mapping count differs from manifest")
    if project_count != expected.get("project_candidates"):
        raise PreferenceProjectExtractionError("project mapping count differs from manifest")
    if preference_count != expected.get("preference_evidence_links"):
        raise PreferenceProjectExtractionError("preference evidence-link count differs")
    if project_count != expected.get("project_evidence_links"):
        raise PreferenceProjectExtractionError("project evidence-link count differs")
    return ExtractionManifest(
        path=resolved,
        sha256=_sha256_bytes(raw_bytes),
        owner_user_id=owner,
        batch_id=batch,
        source_review=source_review,
        project_registration=project_registration,
        expected=expected,
        controls=controls,
        mappings=mappings,
    )


def load_review_report(path: str | Path, manifest: ExtractionManifest) -> Dict[str, Any]:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceProjectExtractionError(f"cannot read source review: {resolved}") from exc
    report = _object(raw, "source review")
    locks = manifest.source_review
    if _sha256_bytes(raw_bytes) != locks["report_sha256"]:
        raise PreferenceProjectExtractionError("source review report SHA-256 changed")
    expected_fields = {
        "mode": locks.get("mode"),
        "review_version": locks.get("review_version"),
        "review_file_sha256": locks.get("review_file_sha256"),
        "reviewed_input_sha256": locks.get("reviewed_input_sha256"),
        "owner_user_id": str(manifest.owner_user_id),
        "batch_id": str(manifest.batch_id),
    }
    for field, expected in expected_fields.items():
        if report.get(field) != expected:
            raise PreferenceProjectExtractionError(
                f"source review {field} changed: expected={expected}, actual={report.get(field)}"
            )
    return report


def _review_decisions(report: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    decisions: Dict[str, Dict[str, Any]] = {}
    for packet_index, raw_packet in enumerate(_array(report.get("packets"), "packets")):
        packet = _object(raw_packet, f"packets[{packet_index}]")
        for decision_index, raw_decision in enumerate(
            _array(packet.get("decisions"), f"packets[{packet_index}].decisions")
        ):
            decision = _object(raw_decision, f"decision[{decision_index}]")
            evidence_id = _required_text(decision.get("evidence_id"), "decision.evidence_id")
            if evidence_id in decisions:
                raise PreferenceProjectExtractionError("source review contains duplicate evidence IDs")
            decisions[evidence_id] = decision
    return decisions


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _candidate_identity(owner: uuid.UUID, target: str, candidate_hash: str) -> str:
    return str(uuid.uuid5(CANDIDATE_NAMESPACE, f"{owner}:{target}:{candidate_hash}"))


def _common_metadata(
    manifest: ExtractionManifest,
    decision: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "batch_id": str(manifest.batch_id),
        "extraction_manifest_sha256": manifest.sha256,
        "input_lock_sha256": decision["input_lock_sha256"],
        "reason_codes": list(decision["reason_codes"]),
        "review_unit_id": decision["review_unit_id"],
        "source_content_sha256": decision["content_sha256"],
        "source_review_report_sha256": manifest.source_review["report_sha256"],
    }
    if mapping.get("overlap_group"):
        metadata["overlap_group"] = mapping["overlap_group"]
        metadata["overlap_review_required"] = True
    return metadata


def _verify_database_controls(
    manifest: ExtractionManifest,
    database_controls: Mapping[str, Any],
) -> None:
    for field in (
        "effective_role_brains_app",
        "transaction_read_only",
        "specialized_tables_forced_rls",
        "evidence_forced_rls",
        "preference_candidate_insert_allowed",
        "project_candidate_insert_allowed",
        "direct_preference_review_insert_denied",
        "direct_project_review_insert_denied",
        "direct_user_preference_insert_denied",
        "direct_project_head_insert_denied",
        "project_registration_execute_allowed",
    ):
        if database_controls.get(field) is not True:
            raise PreferenceProjectExtractionError(f"database control failed: {field}")
    for field in (
        "existing_preference_candidates",
        "existing_project_candidates",
        "existing_project_spaces",
        "existing_preference_reviews",
        "existing_project_reviews",
        "existing_durable_preferences",
        "existing_durable_project_heads",
    ):
        if database_controls.get(field) != manifest.expected.get(field):
            raise PreferenceProjectExtractionError(
                f"database {field} changed: expected={manifest.expected.get(field)}, "
                f"actual={database_controls.get(field)}"
            )


def build_extraction_plan(
    manifest: ExtractionManifest,
    review_report: Mapping[str, Any],
    evidence_states: Sequence[EvidenceState],
    database_controls: Mapping[str, Any],
) -> Dict[str, Any]:
    _verify_database_controls(manifest, database_controls)
    decisions = _review_decisions(review_report)
    selected_ids = {item["evidence_id"] for item in manifest.mappings}
    evidence_by_id = {str(item.evidence_id): item for item in evidence_states}
    if set(evidence_by_id) != selected_ids:
        raise PreferenceProjectExtractionError("database evidence set differs from selected mappings")
    deferred_projects = sum(
        decision.get("target") == "project_knowledge"
        and decision.get("recommended_action") == "reextract_expanded_context"
        for decision in decisions.values()
    )
    if deferred_projects != manifest.expected.get("deferred_project_units"):
        raise PreferenceProjectExtractionError("deferred project count differs from review lock")

    preference_candidates: list[Dict[str, Any]] = []
    preference_links: list[Dict[str, Any]] = []
    project_candidates: list[Dict[str, Any]] = []
    project_links: list[Dict[str, Any]] = []
    for mapping in manifest.mappings:
        evidence_id = mapping["evidence_id"]
        decision = decisions.get(evidence_id)
        if decision is None:
            raise PreferenceProjectExtractionError(f"selected evidence missing from review: {evidence_id}")
        target = mapping["target"]
        expected_action = (
            "preference_candidate_after_schema"
            if target == "preference"
            else "project_candidate_after_schema"
        )
        if decision.get("target") != target or decision.get("decision") != "rewrite":
            raise PreferenceProjectExtractionError("selected review decision or target changed")
        if decision.get("recommended_action") != expected_action:
            raise PreferenceProjectExtractionError("selected review action changed")
        drafts = _array(decision.get("canonical_drafts"), "canonical_drafts")
        if len(drafts) != 1 or not isinstance(drafts[0], str) or not drafts[0].strip():
            raise PreferenceProjectExtractionError("selected decision must have one canonical draft")
        canonical_text = drafts[0].strip()
        state = evidence_by_id[evidence_id]
        if state.status != "active":
            raise PreferenceProjectExtractionError("selected evidence is not active")
        if state.ledger_content_sha256 != state.content_sha256:
            raise PreferenceProjectExtractionError("batch ledger/evidence hash mismatch")
        if state.content_sha256 != decision.get("content_sha256"):
            raise PreferenceProjectExtractionError("selected evidence hash differs from review")
        if state.sensitivity not in {"low", "medium", "high", "restricted"}:
            raise PreferenceProjectExtractionError("selected evidence has invalid sensitivity")
        metadata = _common_metadata(manifest, decision, mapping)
        if target == "preference":
            value = {
                "canonical_text": canonical_text,
                **mapping["value_attributes"],
            }
            candidate_body = {
                "owner_user_id": str(manifest.owner_user_id),
                "preference_class": mapping["preference_class"],
                "preference_domain": mapping["preference_domain"],
                "preference_key": mapping["preference_key"],
                "value": value,
                "polarity": mapping["polarity"],
                "scope": mapping["scope"],
                "explicit": mapping["explicit"],
                "stability": mapping["stability"],
                "surface_policy": mapping["surface_policy"],
                "extraction_confidence": mapping["extraction_confidence"],
                "sensitivity": state.sensitivity,
                "extractor": EXTRACTOR,
                "extractor_version": EXTRACTOR_VERSION,
                "metadata": metadata,
            }
            candidate_hash = _sha256_json(
                {"candidate_contract": MANIFEST_VERSION, "target": target, **candidate_body}
            )
            candidate = {
                "candidate_id": _candidate_identity(
                    manifest.owner_user_id, target, candidate_hash
                ),
                **candidate_body,
                "candidate_hash": candidate_hash,
            }
            preference_candidates.append(candidate)
            preference_links.append(
                {
                    "owner_user_id": str(manifest.owner_user_id),
                    "candidate_id": candidate["candidate_id"],
                    "evidence_id": evidence_id,
                    "stance": "supports",
                    "relevance": 1.0,
                    "rationale": "Hash-locked reviewed source evidence.",
                }
            )
        else:
            effective_at = (
                _iso(state.observed_at)
                if mapping.get("effective_at_source") == "evidence_observed_at"
                else None
            )
            authority_source = {
                "kind": "user_reported_statement",
                "evidence_id": evidence_id,
                "review_unit_id": decision["review_unit_id"],
            }
            candidate_body = {
                "owner_user_id": str(manifest.owner_user_id),
                "project_key": manifest.project_registration["project_key"],
                "knowledge_kind": mapping["knowledge_kind"],
                "knowledge_key": mapping["knowledge_key"],
                "canonical_text": canonical_text,
                "document_state": mapping["document_state"],
                "authority_level": mapping["authority_level"],
                "authority_source": authority_source,
                "effective_at": effective_at,
                "expires_at": None,
                "extraction_confidence": mapping["extraction_confidence"],
                "sensitivity": state.sensitivity,
                "extractor": EXTRACTOR,
                "extractor_version": EXTRACTOR_VERSION,
                "metadata": metadata,
            }
            candidate_hash = _sha256_json(
                {"candidate_contract": MANIFEST_VERSION, "target": target, **candidate_body}
            )
            candidate = {
                "candidate_id": _candidate_identity(
                    manifest.owner_user_id, target, candidate_hash
                ),
                **candidate_body,
                "candidate_hash": candidate_hash,
            }
            project_candidates.append(candidate)
            project_links.append(
                {
                    "owner_user_id": str(manifest.owner_user_id),
                    "project_key": manifest.project_registration["project_key"],
                    "candidate_id": candidate["candidate_id"],
                    "evidence_id": evidence_id,
                    "stance": "supports",
                    "relevance": 1.0,
                    "rationale": "Hash-locked reviewed source evidence.",
                }
            )

    summary = {
        "selected_review_units": len(manifest.mappings),
        "preference_candidates_proposed": len(preference_candidates),
        "preference_evidence_links_proposed": len(preference_links),
        "project_candidates_proposed": len(project_candidates),
        "project_evidence_links_proposed": len(project_links),
        "project_units_deferred": deferred_projects,
        "candidate_records_created": 0,
        "review_records_created": 0,
        "durable_records_created": 0,
    }
    expected_summary = {
        "selected_review_units": manifest.expected["selected_review_units"],
        "preference_candidates_proposed": manifest.expected["preference_candidates"],
        "preference_evidence_links_proposed": manifest.expected[
            "preference_evidence_links"
        ],
        "project_candidates_proposed": manifest.expected["project_candidates"],
        "project_evidence_links_proposed": manifest.expected["project_evidence_links"],
        "project_units_deferred": manifest.expected["deferred_project_units"],
    }
    for field, expected in expected_summary.items():
        if summary[field] != expected:
            raise PreferenceProjectExtractionError(f"summary mismatch for {field}")
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            f"{manifest.owner_user_id}:{manifest.project_registration['project_key']}:register",
        )
    )
    return {
        "mode": REPORT_MODE,
        "manifest_version": MANIFEST_VERSION,
        "manifest_sha256": manifest.sha256,
        "owner_user_id": str(manifest.owner_user_id),
        "batch_id": str(manifest.batch_id),
        "source_review": {
            **manifest.source_review,
            "hash_verified": True,
        },
        "summary": summary,
        "project_registration": {
            "request_id": request_id,
            **manifest.project_registration,
            "operation": "proposed_not_executed",
        },
        "preference_lane": {
            "candidates": preference_candidates,
            "evidence_links": preference_links,
        },
        "project_lane": {
            "candidates": project_candidates,
            "evidence_links": project_links,
        },
        "database_controls": dict(database_controls),
        "controls": {
            **manifest.controls,
            "source_review_hash_verified": True,
            "source_evidence_hashes_verified": True,
            "owner_rls_context_required": True,
            "project_id_resolved_only_during_later_persistence": True,
            "apply_option_exposed": False,
            "persistence_authorized": False,
        },
        "ready_for_persistence_review": True,
    }


async def fetch_extraction_state_readonly(
    conn: asyncpg.Connection,
    manifest: ExtractionManifest,
) -> tuple[list[EvidenceState], Dict[str, Any]]:
    evidence_ids = [uuid.UUID(item["evidence_id"]) for item in manifest.mappings]
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('lock_timeout', '5s', true)")
        await conn.execute("SELECT set_config('statement_timeout', '120s', true)")
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(manifest.owner_user_id)
        )
        controls: Dict[str, Any] = {
            "effective_role_brains_app": bool(
                await conn.fetchval("SELECT current_user = 'brains_app'")
            ),
            "transaction_read_only": bool(
                await conn.fetchval("SELECT current_setting('transaction_read_only') = 'on'")
            ),
            "specialized_tables_forced_rls": bool(
                await conn.fetchval(
                    """
                    SELECT count(*) = $1
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='memory'
                      AND c.relname = ANY($2::text[])
                      AND c.relrowsecurity AND c.relforcerowsecurity
                    """,
                    len(NEW_TABLES),
                    list(NEW_TABLES),
                )
            ),
            "evidence_forced_rls": bool(
                await conn.fetchval(
                    """
                    SELECT c.relrowsecurity AND c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='memory' AND c.relname='evidence'
                    """
                )
            ),
            "preference_candidate_insert_allowed": bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.preference_candidate', 'INSERT')"
                )
            ),
            "project_candidate_insert_allowed": bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.project_knowledge_candidate', 'INSERT')"
                )
            ),
            "direct_preference_review_insert_denied": not bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.preference_candidate_review', 'INSERT')"
                )
            ),
            "direct_project_review_insert_denied": not bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.project_knowledge_candidate_review', 'INSERT')"
                )
            ),
            "direct_user_preference_insert_denied": not bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.user_preference', 'INSERT')"
                )
            ),
            "direct_project_head_insert_denied": not bool(
                await conn.fetchval(
                    "SELECT has_table_privilege('memory.project_knowledge_head', 'INSERT')"
                )
            ),
            "project_registration_execute_allowed": bool(
                await conn.fetchval(
                    """
                    SELECT has_function_privilege(
                      'memory.register_project_space(uuid,text,text,jsonb)', 'EXECUTE'
                    )
                    """
                )
            ),
        }
        records = await conn.fetch(
            """
            SELECT e.evidence_id, r.content_sha256 AS ledger_content_sha256,
                   e.content_sha256, e.observed_at,
                   e.sensitivity::text, e.status::text
            FROM memory.evidence e
            JOIN memory.evidence_ingest_batch_row r
              ON r.owner_user_id=e.owner_user_id AND r.evidence_id=e.evidence_id
            WHERE e.owner_user_id=$1 AND r.batch_id=$2
              AND e.evidence_id=ANY($3::uuid[])
            ORDER BY e.evidence_id
            """,
            manifest.owner_user_id,
            manifest.batch_id,
            evidence_ids,
        )
        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.preference_candidate
               WHERE owner_user_id=$1) AS existing_preference_candidates,
              (SELECT count(*) FROM memory.project_knowledge_candidate
               WHERE owner_user_id=$1) AS existing_project_candidates,
              (SELECT count(*) FROM memory.project_space
               WHERE owner_user_id=$1 AND project_key=$2) AS existing_project_spaces,
              (SELECT count(*) FROM memory.preference_candidate_review
               WHERE owner_user_id=$1) AS existing_preference_reviews,
              (SELECT count(*) FROM memory.project_knowledge_candidate_review
               WHERE owner_user_id=$1) AS existing_project_reviews,
              (SELECT count(*) FROM memory.user_preference
               WHERE owner_user_id=$1) AS existing_durable_preferences,
              (SELECT count(*) FROM memory.project_knowledge_head
               WHERE owner_user_id=$1) AS existing_durable_project_heads
            """,
            manifest.owner_user_id,
            manifest.project_registration["project_key"],
        )
    states = [
        EvidenceState(
            evidence_id=record["evidence_id"],
            ledger_content_sha256=str(record["ledger_content_sha256"]),
            content_sha256=str(record["content_sha256"]),
            observed_at=record["observed_at"],
            sensitivity=str(record["sensitivity"]),
            status=str(record["status"]),
        )
        for record in records
    ]
    return states, {**controls, **{key: int(value) for key, value in dict(counts).items()}}


async def run_extraction_dry_run(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
    source_review_path: str | Path,
) -> Dict[str, Any]:
    manifest = load_extraction_manifest(manifest_path)
    source_review = load_review_report(source_review_path, manifest)
    evidence_states, database_controls = await fetch_extraction_state_readonly(conn, manifest)
    return build_extraction_plan(
        manifest,
        source_review,
        evidence_states,
        database_controls,
    )
