from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_store import actor_uuid


PLAN_VERSION = "memory_v1_candidate_extraction_review_20260713_v1"
MANIFEST_VERSION = "memory_v1_candidate_review_20260713_v1"
REVIEW_UNIT_NAMESPACE = uuid.UUID("bd619e38-72b6-50cb-af35-b421175b266a")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TARGETS = frozenset({"claim", "preference", "project_knowledge"})
ALLOWED_REVIEW_FLAGS = frozenset(
    {
        "belief_not_external_fact",
        "canonical_rewrite_required",
        "context_subject_resolution_required",
        "high_stakes_health_belief_review_required",
        "mixed_assertion_belief_review_required",
        "sensitivity_review_required",
        "uncertainty_qualifier_required",
        "voice_transcription_review",
    }
)
TARGET_DISPOSITIONS = {
    "claim": frozenset({"review_claim_span", "review_belief_span"}),
    "preference": frozenset({"review_preference_span"}),
    "project_knowledge": frozenset({"review_project_span"}),
}
TARGET_EPISTEMIC_ROLES = {
    "claim": frozenset(
        {
            "user_assertion",
            "user_belief_or_opinion",
            "mixed_user_assertion_and_belief",
        }
    ),
    "preference": frozenset({"response_or_life_preference"}),
    "project_knowledge": frozenset({"project_assertion"}),
}
LANE_NAMES = {
    "claim": "governed_claims",
    "preference": "response_and_life_preferences",
    "project_knowledge": "project_knowledge",
}
TARGET_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "claim": {
        "review_artifact": "governed_claim_candidate_v1",
        "persistence_path": "memory.candidate -> reviewed apply -> memory.claim",
        "persistence_readiness": "existing_reviewed_claim_path",
        "required_fields": [
            "subject",
            "predicate",
            "object_literal_or_entity",
            "canonical_text",
            "qualifiers",
            "epistemic_role",
            "evidence_stance",
            "extraction_confidence",
            "sensitivity",
            "retrieval_policy",
            "existing_claim_comparison",
            "evidence_ids",
        ],
        "prohibited_transformations": [
            "treat_user_belief_as_external_fact",
            "infer_truth_from_popularity_or_authority_alone",
            "discard_uncertainty_or_temporal_qualifiers",
            "merge_distinct_people_events_or_timeframes",
        ],
    },
    "preference": {
        "review_artifact": "user_preference_candidate_v1",
        "persistence_path": "new reviewed preference staging -> memory.user_preference",
        "persistence_readiness": "review_staging_schema_required",
        "required_fields": [
            "preference_class_response_or_life",
            "preference_domain",
            "preference_key",
            "value",
            "polarity",
            "scope",
            "explicitness",
            "stability",
            "surface_policy",
            "extraction_confidence",
            "evidence_ids",
        ],
        "prohibited_transformations": [
            "store_background_fact_as_preference",
            "store_project_requirement_as_preference",
            "directly_mutate_active_preference_without_review",
        ],
    },
    "project_knowledge": {
        "review_artifact": "project_knowledge_candidate_v1",
        "persistence_path": "new reviewed project-knowledge staging and durable store",
        "persistence_readiness": "durable_schema_required",
        "required_fields": [
            "project_key",
            "knowledge_kind",
            "canonical_text",
            "document_or_decision_state",
            "authority_source",
            "effective_at",
            "supersedes_or_conflicts_with",
            "extraction_confidence",
            "sensitivity",
            "evidence_ids",
        ],
        "allowed_knowledge_kinds": [
            "architecture",
            "constraint",
            "decision",
            "requirement",
            "roadmap",
            "status",
        ],
        "prohibited_transformations": [
            "store_project_knowledge_as_personal_claim",
            "store_project_knowledge_as_user_profile",
            "assume_newer_means_authoritative_or_better",
            "discard_supersession_or_document_state",
        ],
    },
}


class CandidateReviewError(RuntimeError):
    pass


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str, *, max_length: int = 1000) -> str:
    text = str(value or "").strip()
    if not text:
        raise CandidateReviewError(f"{field} is required")
    if len(text) > max_length:
        raise CandidateReviewError(f"{field} exceeds {max_length} characters")
    return text


def _required_sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(text):
        raise CandidateReviewError(f"{field} must be a lowercase SHA-256")
    return text


def _count_map(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise CandidateReviewError(f"{field} must be an object")
    return dict(value)


def _string_count_map(value: Any, field: str) -> Dict[str, int]:
    raw = _json_object(value, field)
    result: Dict[str, int] = {}
    for key, count in raw.items():
        name = _required_text(key, f"{field} key", max_length=200)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CandidateReviewError(f"{field}.{name} must be a non-negative integer")
        result[name] = count
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class CandidateReviewManifest:
    path: Path
    sha256: str
    manifest_version: str
    plan_version: str
    owner_user_id: uuid.UUID
    batch_id: uuid.UUID
    batch_key: str
    evidence_plan_version: str
    input_fingerprint_sha256: str
    reviewed_report_sha256: str
    authorization_manifest_sha256: str
    expected_evidence_rows: int
    expected_operations: Dict[str, int]
    expected_candidate_targets: Dict[str, int]
    expected_dispositions: Dict[str, int]
    expected_epistemic_roles: Dict[str, int]
    expected_sensitivities: Dict[str, int]
    expected_span_origins: Dict[str, int]
    expected_review_flags: Dict[str, int]
    expected_existing_claim_candidates: int
    expected_existing_preferences: int
    controls: Dict[str, Any]


@dataclass(frozen=True)
class EvidenceReviewRow:
    owner_user_id: uuid.UUID
    batch_id: uuid.UUID
    evidence_id: uuid.UUID
    external_id: str
    ledger_content_sha256: str
    operation: str
    kind: str
    content: str
    content_sha256: str
    observed_at: datetime
    sensitivity: str
    status: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class ReviewUnit:
    review_unit_id: uuid.UUID
    owner_user_id: uuid.UUID
    batch_id: uuid.UUID
    evidence_id: uuid.UUID
    target: str
    lane: str
    input_lock_sha256: str
    content_sha256: str
    external_id: str
    observed_at: datetime
    sensitivity: str
    disposition: str
    epistemic_role: str
    span_origin: str
    review_flags: tuple[str, ...]
    primary_route: str
    review_requirements: tuple[str, ...]
    extraction_status: str
    preview: str


def load_candidate_review_manifest(path: str | Path) -> CandidateReviewManifest:
    manifest_path = Path(path)
    try:
        raw_bytes = manifest_path.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateReviewError(f"cannot read manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise CandidateReviewError("manifest root must be an object")

    manifest_version = _required_text(raw.get("manifest_version"), "manifest_version")
    if manifest_version != MANIFEST_VERSION:
        raise CandidateReviewError(f"unsupported manifest_version: {manifest_version}")
    plan_version = _required_text(raw.get("plan_version"), "plan_version")
    if plan_version != PLAN_VERSION:
        raise CandidateReviewError(f"unsupported plan_version: {plan_version}")
    owner = actor_uuid(raw.get("owner_user_id"))
    batch = _json_object(raw.get("evidence_batch"), "evidence_batch")
    expected = _json_object(raw.get("expected"), "expected")
    controls = _json_object(raw.get("controls"), "controls")
    if controls.get("apply_authorized") is not False:
        raise CandidateReviewError("manifest must explicitly prohibit apply")
    for field in (
        "candidate_writes",
        "claim_writes",
        "preference_writes",
        "project_knowledge_writes",
        "qdrant_writes",
    ):
        if controls.get(field) != 0:
            raise CandidateReviewError(f"controls.{field} must be zero")
    if controls.get("database_transaction") != "read_only":
        raise CandidateReviewError("database transaction must be read_only")
    if controls.get("network_model_calls") is not False:
        raise CandidateReviewError("network model calls must be disabled")

    expected_rows = batch.get("expected_evidence_rows")
    if isinstance(expected_rows, bool) or not isinstance(expected_rows, int) or expected_rows <= 0:
        raise CandidateReviewError("expected_evidence_rows must be a positive integer")

    return CandidateReviewManifest(
        path=manifest_path.resolve(),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        manifest_version=manifest_version,
        plan_version=plan_version,
        owner_user_id=owner,
        batch_id=uuid.UUID(_required_text(batch.get("batch_id"), "batch_id")),
        batch_key=_required_text(batch.get("batch_key"), "batch_key"),
        evidence_plan_version=_required_text(
            batch.get("evidence_plan_version"), "evidence_plan_version"
        ),
        input_fingerprint_sha256=_required_sha256(
            batch.get("input_fingerprint_sha256"), "input_fingerprint_sha256"
        ),
        reviewed_report_sha256=_required_sha256(
            batch.get("reviewed_report_sha256"), "reviewed_report_sha256"
        ),
        authorization_manifest_sha256=_required_sha256(
            batch.get("authorization_manifest_sha256"),
            "authorization_manifest_sha256",
        ),
        expected_evidence_rows=expected_rows,
        expected_operations=_string_count_map(
            batch.get("expected_operations"), "expected_operations"
        ),
        expected_candidate_targets=_string_count_map(
            expected.get("candidate_targets"), "expected.candidate_targets"
        ),
        expected_dispositions=_string_count_map(
            expected.get("dispositions"), "expected.dispositions"
        ),
        expected_epistemic_roles=_string_count_map(
            expected.get("epistemic_roles"), "expected.epistemic_roles"
        ),
        expected_sensitivities=_string_count_map(
            expected.get("sensitivities"), "expected.sensitivities"
        ),
        expected_span_origins=_string_count_map(
            expected.get("span_origins"), "expected.span_origins"
        ),
        expected_review_flags=_string_count_map(
            expected.get("review_flags"), "expected.review_flags"
        ),
        expected_existing_claim_candidates=int(
            expected.get("existing_claim_candidates_for_batch", -1)
        ),
        expected_existing_preferences=int(
            expected.get("existing_preferences_for_batch", -1)
        ),
        controls=controls,
    )


def _row_from_record(record: Mapping[str, Any]) -> EvidenceReviewRow:
    return EvidenceReviewRow(
        owner_user_id=actor_uuid(record["owner_user_id"]),
        batch_id=uuid.UUID(str(record["batch_id"])),
        evidence_id=uuid.UUID(str(record["evidence_id"])),
        external_id=str(record["external_id"]),
        ledger_content_sha256=str(record["ledger_content_sha256"]),
        operation=str(record["operation"]),
        kind=str(record["kind"]),
        content=str(record["content"]),
        content_sha256=str(record["content_sha256"]),
        observed_at=record["observed_at"],
        sensitivity=str(record["sensitivity"]),
        status=str(record["status"]),
        metadata=_json_object(record["metadata"], "evidence.metadata"),
    )


async def fetch_review_batch_readonly(
    conn: asyncpg.Connection,
    manifest: CandidateReviewManifest,
) -> tuple[Dict[str, Any], list[EvidenceReviewRow], Dict[str, Any]]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('lock_timeout', '5s', true)")
        await conn.execute("SELECT set_config('statement_timeout', '120s', true)")
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(manifest.owner_user_id)
        )
        controls = {
            "effective_role_brains_app": bool(
                await conn.fetchval("SELECT current_user = 'brains_app'")
            ),
            "transaction_read_only": bool(
                await conn.fetchval("SELECT current_setting('transaction_read_only') = 'on'")
            ),
            "batch_forced_rls": bool(
                await conn.fetchval(
                    """
                    SELECT count(*) = 2
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='memory'
                      AND c.relname IN (
                        'evidence_ingest_batch', 'evidence_ingest_batch_row'
                      )
                      AND c.relrowsecurity AND c.relforcerowsecurity
                    """
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
        }
        if not all(controls.values()):
            raise CandidateReviewError(f"database read controls failed: {controls}")

        batch = await conn.fetchrow(
            """
            SELECT batch_id, owner_user_id, batch_key, manifest_version,
                   plan_version, input_fingerprint_sha256,
                   reviewed_report_sha256, authorization_manifest_sha256,
                   expected_evidence_count, inserted_count, reused_count, status
            FROM memory.evidence_ingest_batch
            WHERE owner_user_id=$1 AND batch_id=$2
            """,
            manifest.owner_user_id,
            manifest.batch_id,
        )
        if batch is None:
            raise CandidateReviewError("authorized evidence batch was not visible under owner RLS")

        records = await conn.fetch(
            """
            SELECT r.owner_user_id, r.batch_id, r.evidence_id, r.external_id,
                   r.content_sha256 AS ledger_content_sha256, r.operation,
                   e.kind::text, e.content, e.content_sha256, e.observed_at,
                   e.sensitivity::text, e.status::text, e.metadata
            FROM memory.evidence_ingest_batch_row r
            JOIN memory.evidence e
              ON e.owner_user_id=r.owner_user_id AND e.evidence_id=r.evidence_id
            WHERE r.owner_user_id=$1 AND r.batch_id=$2
            ORDER BY e.observed_at, r.evidence_id
            """,
            manifest.owner_user_id,
            manifest.batch_id,
        )
        target_counts = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*)
               FROM memory.candidate c
               JOIN memory.evidence_ingest_batch_row r
                 ON r.owner_user_id=c.owner_user_id AND r.evidence_id=c.evidence_id
               WHERE r.owner_user_id=$1 AND r.batch_id=$2) AS claim_candidates,
              (SELECT count(*)
               FROM memory.user_preference p
               JOIN memory.evidence_ingest_batch_row r
                 ON r.owner_user_id=p.owner_user_id AND r.evidence_id=p.evidence_id
               WHERE r.owner_user_id=$1 AND r.batch_id=$2) AS preferences,
              to_regclass('memory.project_knowledge') IS NOT NULL
                AS project_knowledge_table_exists
            """,
            manifest.owner_user_id,
            manifest.batch_id,
        )

    return dict(batch), [_row_from_record(record) for record in records], {
        **controls,
        "existing_claim_candidates_for_batch": int(target_counts["claim_candidates"]),
        "existing_preferences_for_batch": int(target_counts["preferences"]),
        "project_knowledge_table_exists": bool(
            target_counts["project_knowledge_table_exists"]
        ),
    }


def _metadata_bool(metadata: Mapping[str, Any], field: str) -> bool:
    value = metadata.get(field)
    if not isinstance(value, bool):
        raise CandidateReviewError(f"evidence metadata {field} must be boolean")
    return value


def _review_flags(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("review_flags")
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise CandidateReviewError("evidence metadata review_flags must be a string array")
    flags = tuple(dict.fromkeys(raw))
    unknown = set(flags) - ALLOWED_REVIEW_FLAGS
    if unknown:
        raise CandidateReviewError(f"unknown review flags: {sorted(unknown)}")
    return flags


def _verify_expected(label: str, actual: Mapping[str, int], expected: Mapping[str, int]) -> None:
    if dict(sorted(actual.items())) != dict(sorted(expected.items())):
        raise CandidateReviewError(
            f"{label} changed: expected={dict(sorted(expected.items()))}, "
            f"actual={dict(sorted(actual.items()))}"
        )


def verify_review_batch(
    manifest: CandidateReviewManifest,
    batch: Mapping[str, Any],
    rows: Sequence[EvidenceReviewRow],
    database_controls: Mapping[str, Any],
) -> None:
    expected_batch = {
        "batch_id": manifest.batch_id,
        "owner_user_id": manifest.owner_user_id,
        "batch_key": manifest.batch_key,
        "plan_version": manifest.evidence_plan_version,
        "input_fingerprint_sha256": manifest.input_fingerprint_sha256,
        "reviewed_report_sha256": manifest.reviewed_report_sha256,
        "authorization_manifest_sha256": manifest.authorization_manifest_sha256,
        "expected_evidence_count": manifest.expected_evidence_rows,
        "inserted_count": manifest.expected_operations.get("inserted", 0),
        "reused_count": manifest.expected_operations.get("reused", 0),
        "status": "committed",
    }
    for field, expected in expected_batch.items():
        actual = batch.get(field)
        if str(actual) != str(expected):
            raise CandidateReviewError(
                f"batch header {field} changed: expected={expected}, actual={actual}"
            )
    if len(rows) != manifest.expected_evidence_rows:
        raise CandidateReviewError(
            f"batch row count changed: expected={manifest.expected_evidence_rows}, "
            f"actual={len(rows)}"
        )
    evidence_ids = [row.evidence_id for row in rows]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise CandidateReviewError("batch contains duplicate evidence IDs")

    targets: list[str] = []
    dispositions: list[str] = []
    epistemic_roles: list[str] = []
    sensitivities: list[str] = []
    origins: list[str] = []
    operations: list[str] = []
    flags: list[str] = []
    for row in rows:
        if row.owner_user_id != manifest.owner_user_id or row.batch_id != manifest.batch_id:
            raise CandidateReviewError("review batch contains a cross-owner or cross-batch row")
        if row.ledger_content_sha256 != row.content_sha256:
            raise CandidateReviewError(f"ledger/evidence hash mismatch for {row.evidence_id}")
        if not SHA256_RE.fullmatch(row.content_sha256):
            raise CandidateReviewError(f"invalid evidence hash for {row.evidence_id}")
        if _sha256_text(row.content) != row.content_sha256:
            raise CandidateReviewError(f"evidence content hash mismatch for {row.evidence_id}")
        if row.kind != "user_statement" or row.status != "active":
            raise CandidateReviewError(f"ineligible evidence state for {row.evidence_id}")
        metadata = row.metadata
        target = str(metadata.get("candidate_target") or "")
        disposition = str(metadata.get("disposition") or "")
        epistemic_role = str(metadata.get("epistemic_role") or "")
        span_origin = str(metadata.get("span_origin") or "")
        if target not in TARGETS:
            raise CandidateReviewError(f"invalid target for {row.evidence_id}: {target}")
        if disposition not in TARGET_DISPOSITIONS[target]:
            raise CandidateReviewError(
                f"target/disposition mismatch for {row.evidence_id}: {target}/{disposition}"
            )
        if epistemic_role not in TARGET_EPISTEMIC_ROLES[target]:
            raise CandidateReviewError(
                f"target/epistemic-role mismatch for {row.evidence_id}: "
                f"{target}/{epistemic_role}"
            )
        if _metadata_bool(metadata, "candidate_creation_authorized"):
            raise CandidateReviewError("source evidence unexpectedly authorizes candidates")
        if _metadata_bool(metadata, "retrieval_eligible"):
            raise CandidateReviewError("source evidence unexpectedly authorizes retrieval")
        if _metadata_bool(metadata, "prompt_eligible"):
            raise CandidateReviewError("source evidence unexpectedly authorizes prompt use")
        row_flags = _review_flags(metadata)
        if epistemic_role == "user_belief_or_opinion" and "belief_not_external_fact" not in row_flags:
            raise CandidateReviewError("belief evidence lacks the belief safety flag")
        if epistemic_role == "mixed_user_assertion_and_belief" and "mixed_assertion_belief_review_required" not in row_flags:
            raise CandidateReviewError("mixed assertion/belief evidence lacks decomposition review")
        targets.append(target)
        dispositions.append(disposition)
        epistemic_roles.append(epistemic_role)
        sensitivities.append(row.sensitivity)
        origins.append(span_origin)
        operations.append(row.operation)
        flags.extend(row_flags)

    _verify_expected("candidate targets", _count_map(targets), manifest.expected_candidate_targets)
    _verify_expected("dispositions", _count_map(dispositions), manifest.expected_dispositions)
    _verify_expected("epistemic roles", _count_map(epistemic_roles), manifest.expected_epistemic_roles)
    _verify_expected("sensitivities", _count_map(sensitivities), manifest.expected_sensitivities)
    _verify_expected("span origins", _count_map(origins), manifest.expected_span_origins)
    _verify_expected("review flags", _count_map(flags), manifest.expected_review_flags)
    _verify_expected("ledger operations", _count_map(operations), manifest.expected_operations)
    if database_controls.get("existing_claim_candidates_for_batch") != manifest.expected_existing_claim_candidates:
        raise CandidateReviewError("claim candidate count changed since review manifest")
    if database_controls.get("existing_preferences_for_batch") != manifest.expected_existing_preferences:
        raise CandidateReviewError("preference count changed since review manifest")


def _requirements(target: str, epistemic_role: str, flags: Sequence[str]) -> tuple[str, ...]:
    requirements = ["verify_hash_locked_evidence", "human_accept_rewrite_or_reject"]
    if target == "claim":
        requirements.extend(
            [
                "extract_one_atomic_claim_only",
                "compare_against_existing_governed_claims",
                "record_support_opposition_or_supersession_without_truth_certainty",
            ]
        )
    elif target == "preference":
        requirements.extend(
            [
                "classify_response_vs_life_preference",
                "normalize_scope_polarity_and_stability",
                "compare_against_active_preference_key",
            ]
        )
    else:
        requirements.extend(
            [
                "classify_project_knowledge_kind",
                "assign_project_key_and_authority_source",
                "compare_document_state_supersession_and_conflicts",
            ]
        )
    if epistemic_role == "user_belief_or_opinion":
        requirements.append("preserve_as_user_viewpoint_not_external_fact")
    if "mixed_assertion_belief_review_required" in flags:
        requirements.append("decompose_assertion_from_belief_before_extraction")
    if "high_stakes_health_belief_review_required" in flags:
        requirements.append("restricted_high_stakes_domain_review")
    if "sensitivity_review_required" in flags:
        requirements.append("confirm_restricted_sensitivity_and_surface_policy")
    if "voice_transcription_review" in flags:
        requirements.append("verify_voice_transcription_before_normalization")
    if "context_subject_resolution_required" in flags:
        requirements.append("resolve_subject_without_guessing")
    if "canonical_rewrite_required" in flags:
        requirements.append("rewrite_canonically_without_adding_information")
    if "uncertainty_qualifier_required" in flags:
        requirements.append("retain_approximation_and_uncertainty_qualifiers")
    return tuple(dict.fromkeys(requirements))


def _primary_route(epistemic_role: str, flags: Sequence[str]) -> str:
    precedence = (
        ("mixed_assertion_belief_review_required", "manual_decomposition"),
        ("high_stakes_health_belief_review_required", "restricted_domain_review"),
        ("sensitivity_review_required", "restricted_sensitivity_review"),
        ("voice_transcription_review", "transcription_review"),
        ("context_subject_resolution_required", "subject_resolution"),
        ("canonical_rewrite_required", "canonicalization_review"),
        ("uncertainty_qualifier_required", "uncertainty_qualification_review"),
    )
    for flag, route in precedence:
        if flag in flags:
            return route
    if epistemic_role == "user_belief_or_opinion":
        return "belief_classification_review"
    return "standard_reviewed_extraction"


def build_review_units(
    manifest: CandidateReviewManifest,
    rows: Sequence[EvidenceReviewRow],
) -> list[ReviewUnit]:
    units: list[ReviewUnit] = []
    for row in rows:
        target = str(row.metadata["candidate_target"])
        disposition = str(row.metadata["disposition"])
        epistemic_role = str(row.metadata["epistemic_role"])
        span_origin = str(row.metadata["span_origin"])
        flags = _review_flags(row.metadata)
        locked = {
            "batch_id": str(manifest.batch_id),
            "content_sha256": row.content_sha256,
            "disposition": disposition,
            "epistemic_role": epistemic_role,
            "evidence_id": str(row.evidence_id),
            "external_id": row.external_id,
            "owner_user_id": str(manifest.owner_user_id),
            "plan_version": manifest.plan_version,
            "review_flags": list(flags),
            "sensitivity": row.sensitivity,
            "span_origin": span_origin,
            "target": target,
        }
        input_lock = _sha256_text(_stable_json(locked))
        review_unit_id = uuid.uuid5(
            REVIEW_UNIT_NAMESPACE,
            f"{manifest.owner_user_id}:{manifest.batch_id}:{row.evidence_id}:"
            f"{target}:{manifest.plan_version}",
        )
        units.append(
            ReviewUnit(
                review_unit_id=review_unit_id,
                owner_user_id=manifest.owner_user_id,
                batch_id=manifest.batch_id,
                evidence_id=row.evidence_id,
                target=target,
                lane=LANE_NAMES[target],
                input_lock_sha256=input_lock,
                content_sha256=row.content_sha256,
                external_id=row.external_id,
                observed_at=row.observed_at,
                sensitivity=row.sensitivity,
                disposition=disposition,
                epistemic_role=epistemic_role,
                span_origin=span_origin,
                review_flags=flags,
                primary_route=_primary_route(epistemic_role, flags),
                review_requirements=_requirements(target, epistemic_role, flags),
                extraction_status="not_extracted_review_plan_only",
                preview=" ".join(row.content.split())[:240],
            )
        )
    units.sort(key=lambda unit: (unit.observed_at, str(unit.evidence_id)))
    if len({unit.review_unit_id for unit in units}) != len(units):
        raise CandidateReviewError("deterministic review unit IDs are not unique")
    return units


def _unit_output(unit: ReviewUnit) -> Dict[str, Any]:
    return {
        "review_unit_id": str(unit.review_unit_id),
        "evidence_id": str(unit.evidence_id),
        "input_lock_sha256": unit.input_lock_sha256,
        "content_sha256": unit.content_sha256,
        "external_id": unit.external_id,
        "observed_at": unit.observed_at.isoformat(),
        "sensitivity": unit.sensitivity,
        "disposition": unit.disposition,
        "epistemic_role": unit.epistemic_role,
        "span_origin": unit.span_origin,
        "review_flags": list(unit.review_flags),
        "primary_route": unit.primary_route,
        "review_requirements": list(unit.review_requirements),
        "extraction_status": unit.extraction_status,
        "preview": unit.preview,
    }


def candidate_review_dry_run_report(
    manifest: CandidateReviewManifest,
    batch: Mapping[str, Any],
    units: Sequence[ReviewUnit],
    database_controls: Mapping[str, Any],
) -> Dict[str, Any]:
    targets = _count_map(unit.target for unit in units)
    routes = _count_map(unit.primary_route for unit in units)
    input_fingerprint = _sha256_text(
        _stable_json(
            [
                {
                    "review_unit_id": str(unit.review_unit_id),
                    "input_lock_sha256": unit.input_lock_sha256,
                    "target": unit.target,
                }
                for unit in units
            ]
        )
    )
    lanes: Dict[str, Any] = {}
    for target, lane in LANE_NAMES.items():
        lane_units = [unit for unit in units if unit.target == target]
        contract = TARGET_CONTRACTS[target]
        lanes[lane] = {
            "target": target,
            "review_unit_count": len(lane_units),
            "primary_routes": _count_map(unit.primary_route for unit in lane_units),
            "epistemic_roles": _count_map(unit.epistemic_role for unit in lane_units),
            "contract": contract,
            "review_units": [_unit_output(unit) for unit in lane_units],
        }
    return {
        "mode": "candidate_extraction_review_plan_dry_run_no_writes",
        "manifest_version": manifest.manifest_version,
        "manifest_sha256": manifest.sha256,
        "plan_version": manifest.plan_version,
        "owner_user_id": str(manifest.owner_user_id),
        "source_batch": {
            "batch_id": str(manifest.batch_id),
            "batch_key": manifest.batch_key,
            "status": str(batch["status"]),
            "evidence_rows": len(units),
            "evidence_input_fingerprint_sha256": manifest.input_fingerprint_sha256,
            "review_plan_input_fingerprint_sha256": input_fingerprint,
        },
        "summary": {
            "review_unit_count": len(units),
            "candidate_targets": targets,
            "primary_routes": routes,
            "extracted_candidate_count": 0,
            "approved_candidate_count": 0,
            "promoted_record_count": 0,
        },
        "controls": {
            **manifest.controls,
            "database_controls": dict(database_controls),
            "apply_option_exposed": False,
            "evidence_mutation": False,
            "exact_evidence_content_field_in_report": False,
            "owner_rls_context_required": True,
            "deterministic_review_unit_ids": True,
            "deterministic_input_locks": True,
            "target_lane_overlap_allowed": False,
            "automatic_promotion": False,
        },
        "separation_contract": {
            "governed_claims": "facts, events, corrections, and explicitly typed user beliefs",
            "response_and_life_preferences": "how to respond or what the user prefers; never background facts",
            "project_knowledge": "architecture, requirements, decisions, status, and roadmap; never personal profile",
            "one_review_unit_one_target": True,
            "cross_target_copying": "forbidden; create a separately reviewed evidence relationship if later justified",
        },
        "review_decisions": {
            "allowed": ["accept", "rewrite", "reject", "defer", "split"],
            "hash_lock_rule": "rewrite or split creates a new reviewed artifact; source evidence remains immutable",
            "approval_scope": "one target-specific review artifact only",
            "promotion_scope": "none in this phase",
        },
        "known_schema_gaps": [
            "preference candidates need review staging before memory.user_preference mutation",
            "project knowledge needs a first-class reviewed staging and durable schema",
        ],
        "ready_for_joint_review": len(units) == manifest.expected_evidence_rows,
        "lanes": lanes,
    }


async def run_candidate_review_dry_run(
    conn: asyncpg.Connection,
    *,
    manifest_path: str | Path,
) -> Dict[str, Any]:
    manifest = load_candidate_review_manifest(manifest_path)
    batch, rows, database_controls = await fetch_review_batch_readonly(conn, manifest)
    verify_review_batch(manifest, batch, rows, database_controls)
    units = build_review_units(manifest, rows)
    return candidate_review_dry_run_report(manifest, batch, units, database_controls)
