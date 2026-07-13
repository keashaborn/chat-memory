from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_atomic_spans import AtomicSpanPlan
from .memory_v1_compound_spans import CompoundSpanResolution
from .memory_v1_store import actor_uuid


PLAN_VERSION = "memory_v1_evidence_persistence_20260713_v1"
SOURCE_SYSTEM = "public.chat_log"
SOURCE_ROLE = "frontend/chat:user"
EVIDENCE_ID_NAMESPACE = uuid.UUID("db246970-1f8e-5dd6-8e05-479a060ebf98")
REVIEW_DISPOSITIONS = frozenset(
    {
        "review_belief_span",
        "review_claim_span",
        "review_preference_span",
        "review_project_span",
    }
)


class EvidencePersistencePlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceSpanPlan:
    evidence_id: uuid.UUID
    owner_user_id: uuid.UUID
    kind: str
    source_system: str
    external_id: str
    content: str
    content_sha256: str
    observed_at: datetime
    directness: float
    source_reliability: Optional[float]
    independence_key: str
    sensitivity: str
    status: str
    metadata: Dict[str, Any]
    source_id: uuid.UUID
    source_content: str
    source_content_sha256: str
    span_id: uuid.UUID
    parent_span_id: Optional[uuid.UUID]
    span_origin: str
    char_start: int
    char_end: int
    disposition: str
    candidate_target: str
    epistemic_role: str
    review_flags: tuple[str, ...]
    preview: str


@dataclass(frozen=True)
class ExistingEvidenceRow:
    evidence_id: uuid.UUID
    owner_user_id: uuid.UUID
    kind: str
    source_system: str
    external_id: str
    content: Optional[str]
    content_sha256: Optional[str]
    observed_at: Optional[datetime]
    directness: Optional[float]
    source_reliability: Optional[float]
    independence_key: Optional[str]
    sensitivity: str
    status: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class EvidencePersistenceDecision:
    plan: EvidenceSpanPlan
    action: str
    existing_evidence_id: Optional[uuid.UUID]
    covering_source_evidence_ids: tuple[uuid.UUID, ...]
    conflict_reasons: tuple[str, ...]
    duplicate_content_group: Optional[str]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _planned_evidence_id(
    owner_user_id: uuid.UUID,
    source_system: str,
    external_id: str,
) -> uuid.UUID:
    return uuid.uuid5(
        EVIDENCE_ID_NAMESPACE,
        f"{owner_user_id}:{source_system}:{external_id}",
    )


def _external_id(source_id: uuid.UUID, span_id: uuid.UUID) -> str:
    return f"chat_log:{source_id}:span:{span_id}"


def _sensitivity(review_flags: Sequence[str]) -> str:
    if "sensitivity_review_required" in review_flags:
        return "restricted"
    return "high"


def _epistemic_role(disposition: str) -> str:
    return {
        "review_belief_span": "user_belief_or_opinion",
        "review_claim_span": "user_assertion",
        "review_preference_span": "response_or_life_preference",
        "review_project_span": "project_assertion",
    }[disposition]


def _metadata(
    *,
    source,
    source_content_sha256: str,
    span_id: uuid.UUID,
    parent_span_id: Optional[uuid.UUID],
    span_origin: str,
    char_start: int,
    char_end: int,
    primary_lane: str,
    lanes: Sequence[str],
    reason_codes: Sequence[str],
    disposition: str,
    candidate_target: str,
    review_flags: Sequence[str],
) -> Dict[str, Any]:
    return {
        "candidate_creation_authorized": False,
        "candidate_target": candidate_target,
        "disposition": disposition,
        "epistemic_role": _epistemic_role(disposition),
        "parent_span_id": str(parent_span_id) if parent_span_id else None,
        "persistence_plan_version": PLAN_VERSION,
        "primary_lane": primary_lane,
        "prompt_eligible": False,
        "reason_codes": list(reason_codes),
        "request_id": source.request_id,
        "retrieval_eligible": False,
        "review_flags": list(review_flags),
        "source_char_end": char_end,
        "source_char_start": char_start,
        "source_content_sha256": source_content_sha256,
        "source_id": str(source.source_id),
        "source_role": source.source,
        "span_id": str(span_id),
        "span_origin": span_origin,
        "thread_id": str(source.thread_id) if source.thread_id else None,
        "vantage_id_at_capture": source.vantage_id,
        "vantage_is_owner": False,
    }


def _build_plan(
    *,
    owner_user_id: uuid.UUID,
    source_decision,
    span_id: uuid.UUID,
    parent_span_id: Optional[uuid.UUID],
    span_origin: str,
    char_start: int,
    char_end: int,
    content: str,
    content_sha256: str,
    primary_lane: str,
    lanes: Sequence[str],
    reason_codes: Sequence[str],
    disposition: str,
    candidate_target: Optional[str],
    review_flags: Sequence[str],
) -> EvidenceSpanPlan:
    source = source_decision.source
    if source.owner_user_id != owner_user_id:
        raise EvidencePersistencePlanError("source owner does not match plan owner")
    if source.source != SOURCE_ROLE:
        raise EvidencePersistencePlanError(f"unsupported source role: {source.source}")
    if disposition not in REVIEW_DISPOSITIONS or not candidate_target:
        raise EvidencePersistencePlanError("non-review span entered evidence plan")
    if source.text[char_start:char_end] != content:
        raise EvidencePersistencePlanError(f"source/span mismatch for {span_id}")
    if _sha256_text(content) != content_sha256:
        raise EvidencePersistencePlanError(f"span hash mismatch for {span_id}")
    if _sha256_text(source.text) != source_decision.content_sha256:
        raise EvidencePersistencePlanError(f"source hash mismatch for {source.source_id}")

    external_id = _external_id(source.source_id, span_id)
    flags = tuple(review_flags)
    metadata = _metadata(
        source=source,
        source_content_sha256=source_decision.content_sha256,
        span_id=span_id,
        parent_span_id=parent_span_id,
        span_origin=span_origin,
        char_start=char_start,
        char_end=char_end,
        primary_lane=primary_lane,
        lanes=lanes,
        reason_codes=reason_codes,
        disposition=disposition,
        candidate_target=candidate_target,
        review_flags=flags,
    )
    metadata["lanes"] = list(lanes)
    return EvidenceSpanPlan(
        evidence_id=_planned_evidence_id(owner_user_id, SOURCE_SYSTEM, external_id),
        owner_user_id=owner_user_id,
        kind="user_statement",
        source_system=SOURCE_SYSTEM,
        external_id=external_id,
        content=content,
        content_sha256=content_sha256,
        observed_at=source.created_at,
        directness=1.0,
        source_reliability=None,
        independence_key=f"user_statement_sha256:{content_sha256}",
        sensitivity=_sensitivity(flags),
        status="active",
        metadata=metadata,
        source_id=source.source_id,
        source_content=source.text,
        source_content_sha256=source_decision.content_sha256,
        span_id=span_id,
        parent_span_id=parent_span_id,
        span_origin=span_origin,
        char_start=char_start,
        char_end=char_end,
        disposition=disposition,
        candidate_target=candidate_target,
        epistemic_role=_epistemic_role(disposition),
        review_flags=flags,
        preview=" ".join(content.split())[:320],
    )


def build_evidence_span_plans(
    atomic_plans: Sequence[AtomicSpanPlan],
    compound_resolutions: Sequence[CompoundSpanResolution],
    *,
    owner_user_id: str | uuid.UUID,
) -> list[EvidenceSpanPlan]:
    owner = actor_uuid(owner_user_id)
    resolutions = {
        resolution.parent_span.span_id: resolution
        for resolution in compound_resolutions
    }
    if len(resolutions) != len(compound_resolutions):
        raise EvidencePersistencePlanError("duplicate compound parent resolution")

    expected_manual = {
        span.span_id
        for plan in atomic_plans
        for span in plan.spans
        if span.disposition == "manual_split_required"
    }
    if expected_manual != set(resolutions):
        missing = sorted(str(value) for value in expected_manual - set(resolutions))
        extra = sorted(str(value) for value in set(resolutions) - expected_manual)
        raise EvidencePersistencePlanError(
            f"compound resolution mismatch: missing={missing}, extra={extra}"
        )

    plans: list[EvidenceSpanPlan] = []
    for atomic_plan in atomic_plans:
        decision = atomic_plan.source_decision
        if decision.source.owner_user_id != owner:
            raise EvidencePersistencePlanError("atomic plan contains a cross-owner source")
        for span in atomic_plan.spans:
            if span.disposition in REVIEW_DISPOSITIONS:
                flags = (
                    ("belief_not_external_fact",)
                    if span.disposition == "review_belief_span"
                    else ()
                )
                plans.append(
                    _build_plan(
                        owner_user_id=owner,
                        source_decision=decision,
                        span_id=span.span_id,
                        parent_span_id=None,
                        span_origin="atomic",
                        char_start=span.char_start,
                        char_end=span.char_end,
                        content=span.content,
                        content_sha256=span.content_sha256,
                        primary_lane=span.primary_lane,
                        lanes=span.lanes,
                        reason_codes=span.reason_codes,
                        disposition=span.disposition,
                        candidate_target=span.candidate_target,
                        review_flags=flags,
                    )
                )
                continue
            if span.disposition != "manual_split_required":
                continue
            resolution = resolutions[span.span_id]
            if not resolution.coverage_verified or resolution.status not in {
                "resolved",
                "resolved_without_split",
            }:
                raise EvidencePersistencePlanError(
                    f"unresolved compound parent entered persistence plan: {span.span_id}"
                )
            for child in resolution.children:
                if child.disposition not in REVIEW_DISPOSITIONS:
                    continue
                plans.append(
                    _build_plan(
                        owner_user_id=owner,
                        source_decision=decision,
                        span_id=child.child_span_id,
                        parent_span_id=span.span_id,
                        span_origin="compound_child",
                        char_start=child.char_start,
                        char_end=child.char_end,
                        content=child.content,
                        content_sha256=child.content_sha256,
                        primary_lane=child.primary_lane,
                        lanes=child.lanes,
                        reason_codes=child.reason_codes,
                        disposition=child.disposition,
                        candidate_target=child.candidate_target,
                        review_flags=child.review_flags,
                    )
                )

    plans.sort(key=lambda item: (item.observed_at, str(item.source_id), item.char_start, item.external_id))
    external_ids = [plan.external_id for plan in plans]
    evidence_ids = [plan.evidence_id for plan in plans]
    if len(external_ids) != len(set(external_ids)):
        raise EvidencePersistencePlanError("duplicate planned external_id")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidencePersistencePlanError("duplicate deterministic evidence_id")
    return plans


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _json_object(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidencePersistencePlanError("stored evidence metadata is not an object")
    return dict(value)


async def fetch_existing_evidence_readonly(
    conn: asyncpg.Connection,
    owner_user_id: str | uuid.UUID,
    plans: Sequence[EvidenceSpanPlan],
) -> list[ExistingEvidenceRow]:
    owner = actor_uuid(owner_user_id)
    if any(plan.owner_user_id != owner for plan in plans):
        raise EvidencePersistencePlanError("planned row owner mismatch")
    identities = {plan.external_id for plan in plans}
    for plan in plans:
        identities.add(str(plan.source_id))
        identities.add(f"chat_log:{plan.source_id}")

    async with conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
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
            owner,
            SOURCE_SYSTEM,
            sorted(identities),
        )

    result: list[ExistingEvidenceRow] = []
    for row in rows:
        row_owner = actor_uuid(row["owner_user_id"])
        if row_owner != owner:
            raise EvidencePersistencePlanError("read returned cross-owner evidence")
        result.append(
            ExistingEvidenceRow(
                evidence_id=uuid.UUID(str(row["evidence_id"])),
                owner_user_id=row_owner,
                kind=str(row["kind"]),
                source_system=str(row["source_system"]),
                external_id=str(row["external_id"]),
                content=str(row["content"]) if row["content"] is not None else None,
                content_sha256=(
                    str(row["content_sha256"])
                    if row["content_sha256"] is not None
                    else None
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


def _exact_conflicts(
    plan: EvidenceSpanPlan,
    existing: ExistingEvidenceRow,
) -> tuple[str, ...]:
    comparisons = {
        "evidence_id": (plan.evidence_id, existing.evidence_id),
        "owner_user_id": (plan.owner_user_id, existing.owner_user_id),
        "kind": (plan.kind, existing.kind),
        "source_system": (plan.source_system, existing.source_system),
        "external_id": (plan.external_id, existing.external_id),
        "content": (plan.content, existing.content),
        "content_sha256": (plan.content_sha256, existing.content_sha256),
        "observed_at": (plan.observed_at, existing.observed_at),
        "directness": (plan.directness, existing.directness),
        "source_reliability": (
            plan.source_reliability,
            existing.source_reliability,
        ),
        "independence_key": (plan.independence_key, existing.independence_key),
        "sensitivity": (plan.sensitivity, existing.sensitivity),
        "status": (plan.status, existing.status),
        "metadata": (_stable_json(plan.metadata), _stable_json(existing.metadata)),
    }
    return tuple(
        f"immutable_mismatch:{field}"
        for field, (expected, actual) in comparisons.items()
        if expected != actual
    )


def build_persistence_decisions(
    plans: Sequence[EvidenceSpanPlan],
    existing_rows: Sequence[ExistingEvidenceRow],
) -> list[EvidencePersistenceDecision]:
    exact_by_external: Dict[str, ExistingEvidenceRow] = {}
    for row in existing_rows:
        if row.external_id in exact_by_external:
            raise EvidencePersistencePlanError("duplicate stored evidence identity")
        exact_by_external[row.external_id] = row

    content_counts = Counter(plan.content_sha256 for plan in plans)
    decisions: list[EvidencePersistenceDecision] = []
    for plan in plans:
        conflicts: list[str] = []
        existing = exact_by_external.get(plan.external_id)
        if existing:
            conflicts.extend(_exact_conflicts(plan, existing))

        covering: list[uuid.UUID] = []
        for covering_external in (str(plan.source_id), f"chat_log:{plan.source_id}"):
            row = exact_by_external.get(covering_external)
            if not row:
                continue
            covering.append(row.evidence_id)
            if row.content_sha256 != plan.source_content_sha256:
                conflicts.append(
                    f"covering_source_hash_mismatch:{covering_external}"
                )
            if row.content != plan.source_content:
                conflicts.append(
                    f"covering_source_content_mismatch:{covering_external}"
                )

        if conflicts:
            action = "conflict_fail_closed"
        elif existing:
            action = "reuse_exact"
        else:
            action = "plan_insert"
        duplicate_group = (
            f"span_content_sha256:{plan.content_sha256}"
            if content_counts[plan.content_sha256] > 1
            else None
        )
        decisions.append(
            EvidencePersistenceDecision(
                plan=plan,
                action=action,
                existing_evidence_id=existing.evidence_id if existing else None,
                covering_source_evidence_ids=tuple(sorted(covering, key=str)),
                conflict_reasons=tuple(sorted(set(conflicts))),
                duplicate_content_group=duplicate_group,
            )
        )
    return decisions


def _counter_dict(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _input_fingerprint(plans: Sequence[EvidenceSpanPlan]) -> str:
    locked = [
        {
            "candidate_target": plan.candidate_target,
            "char_end": plan.char_end,
            "char_start": plan.char_start,
            "content_sha256": plan.content_sha256,
            "disposition": plan.disposition,
            "evidence_id": str(plan.evidence_id),
            "external_id": plan.external_id,
            "metadata": plan.metadata,
            "owner_user_id": str(plan.owner_user_id),
            "source_content_sha256": plan.source_content_sha256,
        }
        for plan in plans
    ]
    return _sha256_text(_stable_json(locked))


def evidence_persistence_dry_run_report(
    decisions: Sequence[EvidencePersistenceDecision],
    *,
    owner_user_id: str | uuid.UUID,
    atomic_review_span_count: int,
    compound_review_span_count: int,
) -> Dict[str, Any]:
    owner = actor_uuid(owner_user_id)
    if any(decision.plan.owner_user_id != owner for decision in decisions):
        raise EvidencePersistencePlanError("decision contains a cross-owner row")
    plans = [decision.plan for decision in decisions]
    duplicate_groups: Mapping[str, list[EvidencePersistenceDecision]] = defaultdict(list)
    for decision in decisions:
        if decision.duplicate_content_group:
            duplicate_groups[decision.duplicate_content_group].append(decision)
    conflicts = [
        decision
        for decision in decisions
        if decision.action == "conflict_fail_closed"
    ]
    return {
        "mode": "evidence_persistence_plan_dry_run_no_writes",
        "plan_version": PLAN_VERSION,
        "owner_user_id": str(owner),
        "source_system": SOURCE_SYSTEM,
        "source_role": SOURCE_ROLE,
        "input_fingerprint_sha256": _input_fingerprint(plans),
        "summary": {
            "planned_row_count": len(decisions),
            "atomic_review_span_count": atomic_review_span_count,
            "compound_review_span_count": compound_review_span_count,
            "actions": _counter_dict(decision.action for decision in decisions),
            "origins": _counter_dict(plan.span_origin for plan in plans),
            "dispositions": _counter_dict(plan.disposition for plan in plans),
            "epistemic_roles": _counter_dict(
                plan.epistemic_role for plan in plans
            ),
            "candidate_targets": _counter_dict(
                plan.candidate_target for plan in plans
            ),
            "sensitivities": _counter_dict(plan.sensitivity for plan in plans),
            "covering_source_row_count": sum(
                bool(decision.covering_source_evidence_ids)
                for decision in decisions
            ),
            "covering_source_evidence_ids": len(
                {
                    evidence_id
                    for decision in decisions
                    for evidence_id in decision.covering_source_evidence_ids
                }
            ),
            "unique_span_content_hashes": len(
                {plan.content_sha256 for plan in plans}
            ),
            "duplicate_content_group_count": len(duplicate_groups),
            "duplicate_content_row_count": sum(
                len(group) for group in duplicate_groups.values()
            ),
            "conflict_count": len(conflicts),
        },
        "controls": {
            "database_transaction": "read_only",
            "database_writes": 0,
            "evidence_writes": 0,
            "candidate_writes": 0,
            "claim_writes": 0,
            "preference_writes": 0,
            "qdrant_writes": 0,
            "prompt_injection": False,
            "apply_option_exposed": False,
            "full_source_text_in_report": False,
            "exact_span_content_field_in_report": False,
            "owner_rls_context_required": True,
            "exact_source_offsets": True,
            "source_hash_verification": True,
            "span_hash_verification": True,
            "deterministic_evidence_ids": True,
            "review_required": True,
        },
        "append_only_contract": {
            "apply_authorized": False,
            "allowed_future_mutation": "INSERT only",
            "forbidden_future_mutations": [
                "UPDATE",
                "DELETE",
                "INSERT ON CONFLICT DO UPDATE",
            ],
            "future_replay_algorithm": (
                "INSERT ON CONFLICT DO NOTHING; SELECT under owner RLS; "
                "verify every immutable field; fail closed on mismatch"
            ),
            "identity_constraint": (
                "UNIQUE(owner_user_id, source_system, external_id)"
            ),
            "known_schema_gap": (
                "brains_app currently has UPDATE and DELETE on memory.evidence; "
                "a future apply path requires narrower database enforcement"
            ),
        },
        "deduplication_contract": {
            "same_external_identity": "reuse only after exact immutable verification",
            "same_content_different_source": (
                "preserve each source occurrence; share an exact-content "
                "independence_key so repetition is not independent corroboration"
            ),
            "existing_full_source_evidence": (
                "retain as covering provenance; do not count the span and its "
                "source row as independent evidence"
            ),
        },
        "ready_for_joint_review": not conflicts,
        "rows": [
            {
                "action": decision.action,
                "candidate_target": plan.candidate_target,
                "char_end": plan.char_end,
                "char_start": plan.char_start,
                "conflict_reasons": list(decision.conflict_reasons),
                "content_sha256": plan.content_sha256,
                "covering_source_evidence_ids": [
                    str(value) for value in decision.covering_source_evidence_ids
                ],
                "deterministic_evidence_id": str(plan.evidence_id),
                "directness": plan.directness,
                "disposition": plan.disposition,
                "epistemic_role": plan.epistemic_role,
                "duplicate_content_group": decision.duplicate_content_group,
                "existing_evidence_id": (
                    str(decision.existing_evidence_id)
                    if decision.existing_evidence_id
                    else None
                ),
                "external_id": plan.external_id,
                "independence_key": plan.independence_key,
                "kind": plan.kind,
                "metadata": plan.metadata,
                "observed_at": plan.observed_at.isoformat(),
                "owner_user_id": str(plan.owner_user_id),
                "preview": plan.preview,
                "sensitivity": plan.sensitivity,
                "source_reliability": plan.source_reliability,
                "source_system": plan.source_system,
                "status": plan.status,
            }
            for decision, plan in (
                (decision, decision.plan) for decision in decisions
            )
        ],
    }
