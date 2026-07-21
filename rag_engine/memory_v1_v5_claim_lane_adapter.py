from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from uuid import UUID

from .memory_v1_governed_lane_adapter_common import (
    GovernedLaneAdapterError,
    SENSITIVITY_RANK,
    build_lane_result,
    canonical_sha256,
    parse_mapping,
    parse_optional_uuid,
    parse_score,
    parse_text,
    parse_utc,
    parse_uuid,
    parse_uuid_tuple,
    require_fields,
    validate_sha256,
    verify_governed_read_controls,
)
from .memory_v1_v5_shadow_candidate import VERSION as CANDIDATE_CONTRACT_VERSION
from .memory_v1_selection_envelope import (
    ClaimSelectionV1,
    EpistemicStatus,
    EvidenceByStanceV1,
    MemoryLane,
    MemoryLaneLimitV1,
    MemoryLaneSelectionResultV1,
    MemorySelectionRequestV1,
    MemorySelectionScoresV1,
    RejectionCode,
    Sensitivity,
    SourceContractVersionV1,
    SurfacePolicy,
    UseInstruction,
    estimate_memory_record_tokens_v1,
)


SOURCE_NAME = "memory.read_v5_shadow_claims"
REQUIRED_BATCH_FIELDS = (
    "owner_user_id",
    "claim_ids",
    "records",
    "controls",
    "database_writes",
)
REQUIRED_ROW_FIELDS = (
    "owner_user_id",
    "claim_id",
    "revision_id",
    "canonical_key",
    "canonical_text",
    "predicate",
    "status",
    "sensitivity",
    "importance",
    "salience",
    "valid_from",
    "valid_to",
    "superseded_by",
    "metadata",
    "retrieval_policy",
    "projection_review_decision",
    "projection_apply_outcome",
    "evidence_by_stance",
    "observation_ids",
    "project_key",
    "component_key",
    "source_content_sha256",
)
EVIDENCE_STANCES = frozenset({"context", "opposes", "qualifies", "supports"})
RETRIEVABLE_STATUSES = frozenset({item.value for item in EpistemicStatus})
CONTENT_SURFACES = frozenset(
    {
        SurfacePolicy.DIRECT_OR_RELEVANT,
        SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL,
        SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY,
    }
)
_PREDICATE_PERMISSION_RE = re.compile(
    r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*(?:\.)?$"
)


class CandidateDiscovererV1(Protocol):
    def __call__(
        self,
        owner_user_id: UUID,
        query_vector: tuple[float, ...],
        *,
        limit: int,
    ) -> Mapping[str, Any]: ...


ClaimRowLoaderV1 = Callable[
    [UUID, Sequence[UUID]], Awaitable[Mapping[str, Any]]
]
PredicatePrefixResolverV1 = Callable[[MemorySelectionRequestV1], Sequence[str]]


@dataclass(frozen=True)
class _Candidate:
    claim_id: UUID
    semantic_score: float


@dataclass
class _Evaluated:
    claim_id: UUID
    semantic_score: float
    importance: float
    salience: float
    record: ClaimSelectionV1 | None
    reasons: list[RejectionCode]


def _source_is_pinned(
    request: MemorySelectionRequestV1,
    source: SourceContractVersionV1,
) -> bool:
    return any(
        item.name == source.name and item.version == source.version
        for item in request.source_contract_versions
    )


def _surface_is_eligible(
    surface: SurfacePolicy,
    *,
    request: MemorySelectionRequestV1,
    record_project_key: str | None,
    record_component_key: str | None,
) -> bool:
    if surface == SurfacePolicy.DIRECT_OR_RELEVANT:
        return True
    if surface == SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL:
        return request.explicit_recall or request.memory_intent in {
            "life_preference_recall",
            "personal_recommendation",
            "recommendation",
        }
    if surface == SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY:
        return bool(
            request.project_key
            and record_project_key == request.project_key
            and record_component_key == request.component_key
        )
    return False


def _use_instruction(status: EpistemicStatus, surface: SurfacePolicy) -> UseInstruction:
    if status in {EpistemicStatus.UNCERTAIN, EpistemicStatus.DISPUTED}:
        return UseInstruction.STATE_UNCERTAINTY_AND_MATERIAL_COUNTEREVIDENCE
    if surface == SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL:
        return UseInstruction.USE_ONLY_FOR_RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL
    if surface == SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY:
        return UseInstruction.USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE
    return UseInstruction.ANSWER_DIRECTLY_ONLY_WHEN_RELEVANT


def _predicate_allowed(predicate: str, prefixes: Sequence[str]) -> bool:
    # Registry values without a trailing dot are exact predicates.  Only an
    # explicit trailing dot grants a namespace.  Treating every value as a raw
    # string prefix would let ``identity.name`` authorize
    # ``identity.name_unreviewed``.
    return any(
        predicate.startswith(prefix)
        if prefix.endswith(".")
        else predicate == prefix
        for prefix in prefixes
    )


def _typed_record(
    *,
    row: Mapping[str, Any],
    candidate: _Candidate,
    request: MemorySelectionRequestV1,
    source_contract: SourceContractVersionV1,
    allowed_predicate_prefixes: Sequence[str],
    semantic_floor: float,
) -> _Evaluated:
    require_fields(row, REQUIRED_ROW_FIELDS, source=SOURCE_NAME)
    owner = parse_uuid(row["owner_user_id"], field="claim.owner_user_id")
    claim_id = parse_uuid(row["claim_id"], field="claim.claim_id")
    if owner != request.owner_user_id:
        raise GovernedLaneAdapterError("claim loader returned a cross-owner row")
    if claim_id != candidate.claim_id:
        raise GovernedLaneAdapterError("claim candidate/row identity mismatch")

    revision_id = parse_optional_uuid(row["revision_id"], field="claim.revision_id")
    superseded_by = parse_optional_uuid(
        row["superseded_by"], field="claim.superseded_by"
    )
    text = parse_text(row["canonical_text"], field="claim.canonical_text")
    predicate = parse_text(row["predicate"], field="claim.predicate", maximum=128).casefold()
    status_value = str(row["status"] or "").strip().casefold()
    sensitivity_value = str(row["sensitivity"] or "").strip().casefold()
    if sensitivity_value not in SENSITIVITY_RANK:
        raise GovernedLaneAdapterError("claim sensitivity is outside the closed enum")
    sensitivity = Sensitivity(sensitivity_value)
    importance = parse_score(row["importance"], field="claim.importance")
    salience = parse_score(row["salience"], field="claim.salience")
    valid_from = parse_utc(row["valid_from"], field="claim.valid_from")
    valid_to = parse_utc(row["valid_to"], field="claim.valid_to")
    metadata = parse_mapping(row["metadata"], field="claim.metadata")
    retrieval_policy = parse_mapping(
        row["retrieval_policy"], field="claim.retrieval_policy"
    )
    surface_value = str(retrieval_policy.get("surface_policy") or "").casefold()
    try:
        surface = SurfacePolicy(surface_value)
    except ValueError:
        surface = SurfacePolicy.NEVER

    evidence_raw = parse_mapping(
        row["evidence_by_stance"], field="claim.evidence_by_stance"
    )
    if set(evidence_raw) != EVIDENCE_STANCES:
        raise GovernedLaneAdapterError(
            "claim evidence_by_stance must contain the closed stance set"
        )
    evidence = EvidenceByStanceV1(
        context=parse_uuid_tuple(evidence_raw["context"], field="claim.evidence.context"),
        opposes=parse_uuid_tuple(evidence_raw["opposes"], field="claim.evidence.opposes"),
        qualifies=parse_uuid_tuple(
            evidence_raw["qualifies"], field="claim.evidence.qualifies"
        ),
        supports=parse_uuid_tuple(evidence_raw["supports"], field="claim.evidence.supports"),
    )
    observations = parse_uuid_tuple(
        row["observation_ids"], field="claim.observation_ids"
    )
    project_key = str(row["project_key"] or "").strip() or None
    component_key = str(row["component_key"] or "").strip() or None
    source_hash = validate_sha256(
        row["source_content_sha256"], field="claim.source_content_sha256"
    )

    reasons: list[RejectionCode] = []
    if metadata.get("memory_contract") != source_contract.version or not str(
        row["canonical_key"] or ""
    ).startswith("v5:"):
        reasons.append(RejectionCode.INVALID_SOURCE_CONTRACT)
    if not _source_is_pinned(request, source_contract):
        reasons.append(RejectionCode.SOURCE_VERSION)
    if row["projection_review_decision"] not in {
        "authorized",
        "auto_apply_eligible",
    }:
        reasons.append(RejectionCode.PROJECTION_NOT_AUTHORIZED)
    if row["projection_apply_outcome"] != "applied":
        reasons.append(RejectionCode.PROJECTION_NOT_APPLIED)
    if candidate.semantic_score < semantic_floor:
        reasons.append(RejectionCode.SEMANTIC_RELEVANCE)
    if status_value not in RETRIEVABLE_STATUSES:
        reasons.append(RejectionCode.EPISTEMIC_STATUS)
    if not evidence.all_evidence():
        reasons.append(RejectionCode.NO_ACTIVE_EVIDENCE)
    if status_value == EpistemicStatus.SUPPORTED.value and not evidence.supports:
        reasons.append(RejectionCode.NO_SUPPORTING_EVIDENCE)
    if not observations:
        reasons.append(RejectionCode.NO_OBSERVATION_PROVENANCE)
    if SENSITIVITY_RANK[sensitivity_value] > SENSITIVITY_RANK[
        request.max_sensitivity.value
    ]:
        reasons.append(RejectionCode.SENSITIVITY)
    if valid_from and valid_from > request.selected_at:
        reasons.append(RejectionCode.NOT_YET_VALID)
    if valid_to and valid_to <= request.selected_at:
        reasons.append(RejectionCode.EXPIRED)
    if superseded_by is not None:
        reasons.append(RejectionCode.SUPERSEDED)
    if not _predicate_allowed(predicate, allowed_predicate_prefixes):
        reasons.append(RejectionCode.PREDICATE_PERMISSION)
    if surface not in CONTENT_SURFACES or not _surface_is_eligible(
        surface,
        request=request,
        record_project_key=project_key,
        record_component_key=component_key,
    ):
        reasons.append(RejectionCode.SURFACE_POLICY)

    if reasons:
        return _Evaluated(
            claim_id=claim_id,
            semantic_score=candidate.semantic_score,
            importance=importance,
            salience=salience,
            record=None,
            reasons=reasons,
        )

    try:
        status = EpistemicStatus(status_value)
        record = ClaimSelectionV1(
            owner_user_id=owner,
            lane=MemoryLane.CLAIM,
            record_id=claim_id,
            revision_id=revision_id,
            source_contract=source_contract,
            source_content_sha256=source_hash,
            rank=1,
            surface_policy=surface,
            sensitivity=sensitivity,
            use_instruction=_use_instruction(status, surface),
            token_estimate=1,
            valid_from=valid_from,
            valid_to=valid_to,
            superseded_by=superseded_by,
            evidence_refs=evidence.all_evidence(),
            observation_refs=observations,
            scores=MemorySelectionScoresV1(
                semantic_relevance=candidate.semantic_score,
                importance=importance,
                aggregate_salience=salience,
            ),
            text=text,
            predicate=predicate,
            epistemic_status=status,
            evidence_by_stance=evidence,
            project_key=project_key,
            component_key=component_key,
        )
        raw = record.model_dump()
        raw["token_estimate"] = estimate_memory_record_tokens_v1(record)
        record = ClaimSelectionV1.model_validate(raw)
    except Exception as exc:
        raise GovernedLaneAdapterError(
            "claim row violates the typed Memory V1 selection contract"
        ) from exc
    return _Evaluated(
        claim_id=claim_id,
        semantic_score=candidate.semantic_score,
        importance=importance,
        salience=salience,
        record=record,
        reasons=[],
    )


class V5ClaimLaneAdapterV1:
    """Qdrant handles candidates; Postgres rows remain the only content authority."""

    def __init__(
        self,
        *,
        source_contract: SourceContractVersionV1,
        candidate_discoverer: CandidateDiscovererV1,
        row_loader: ClaimRowLoaderV1,
        predicate_prefix_resolver: PredicatePrefixResolverV1,
        candidate_limit: int = 24,
        minimum_semantic_score: float = 0.20,
        relative_semantic_ratio: float = 0.40,
    ) -> None:
        if source_contract.name != "claim_projection":
            raise ValueError("claim adapter requires the claim_projection source")
        if not 1 <= candidate_limit <= 100:
            raise ValueError("candidate_limit must be between 1 and 100")
        if not 0.0 <= minimum_semantic_score <= 1.0:
            raise ValueError("minimum_semantic_score must be between zero and one")
        if not 0.0 <= relative_semantic_ratio <= 1.0:
            raise ValueError("relative_semantic_ratio must be between zero and one")
        self._source_contract = source_contract
        self._candidate_discoverer = candidate_discoverer
        self._row_loader = row_loader
        self._prefix_resolver = predicate_prefix_resolver
        self._candidate_limit = candidate_limit
        self._minimum_semantic_score = minimum_semantic_score
        self._relative_semantic_ratio = relative_semantic_ratio

    async def select(
        self,
        request: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1:
        try:
            request = request.strict_revalidated()
        except Exception as exc:
            raise GovernedLaneAdapterError("claim adapter received an invalid request") from exc
        if lane_limit.lane != MemoryLane.CLAIM:
            raise GovernedLaneAdapterError("claim adapter received another lane's budget")
        if MemoryLane.CLAIM not in request.requested_lanes:
            raise GovernedLaneAdapterError("claim adapter was called for an unrequested lane")

        raw_discovery = self._candidate_discoverer(
            request.owner_user_id,
            request.query_vector,
            limit=self._candidate_limit,
        )
        if not isinstance(raw_discovery, Mapping):
            raise GovernedLaneAdapterError(
                "V5 claim candidate discovery must return an object"
            )
        require_fields(
            raw_discovery,
            (
                "version",
                "owner_user_id",
                "collection",
                "query_vector_dimension",
                "query_vector_sha256",
                "candidate_limit",
                "candidate_hits",
                "candidate_count",
                "candidate_set_sha256",
                "database_writes",
                "qdrant_writes",
                "prompt_injection",
                "answer_model_exposure",
                "retrieval_activation",
            ),
            source="V5 claim candidate discovery",
        )
        if (
            type(raw_discovery["database_writes"]) is not int
            or raw_discovery["database_writes"] != 0
            or type(raw_discovery["qdrant_writes"]) is not int
            or raw_discovery["qdrant_writes"] != 0
            or raw_discovery["prompt_injection"] is not False
            or raw_discovery["answer_model_exposure"] is not False
            or raw_discovery["retrieval_activation"] is not False
        ):
            raise GovernedLaneAdapterError("claim discovery violated its read-only role")
        if raw_discovery["version"] != CANDIDATE_CONTRACT_VERSION:
            raise GovernedLaneAdapterError("claim candidate contract version changed")
        discovery_owner = parse_uuid(
            raw_discovery["owner_user_id"], field="candidate.owner_user_id"
        )
        if discovery_owner != request.owner_user_id:
            raise GovernedLaneAdapterError(
                "claim discovery returned a cross-owner artifact"
            )
        collection = parse_text(
            raw_discovery["collection"], field="candidate.collection", maximum=255
        )
        if collection != collection.strip():
            raise GovernedLaneAdapterError("claim candidate collection is not canonical")
        vector_bytes = struct.pack(
            f"!{len(request.query_vector)}d", *request.query_vector
        )
        expected_vector_sha256 = hashlib.sha256(vector_bytes).hexdigest()
        if (
            type(raw_discovery["query_vector_dimension"]) is not int
            or raw_discovery["query_vector_dimension"] != len(request.query_vector)
            or validate_sha256(
                raw_discovery["query_vector_sha256"],
                field="candidate.query_vector_sha256",
            )
            != expected_vector_sha256
            or type(raw_discovery["candidate_limit"]) is not int
            or raw_discovery["candidate_limit"] != self._candidate_limit
        ):
            raise GovernedLaneAdapterError(
                "claim candidate artifact differs from the trusted query"
            )
        hits = raw_discovery["candidate_hits"]
        if not isinstance(hits, list):
            raise GovernedLaneAdapterError("claim candidate_hits must be an array")
        candidates: list[_Candidate] = []
        seen: set[UUID] = set()
        prior_score: float | None = None
        for expected_rank, hit in enumerate(hits, start=1):
            if not isinstance(hit, Mapping):
                raise GovernedLaneAdapterError("claim candidate hit must be an object")
            require_fields(
                hit,
                ("rank", "claim_id", "semantic_score"),
                source="V5 claim candidate hit",
            )
            if type(hit["rank"]) is not int or hit["rank"] != expected_rank:
                raise GovernedLaneAdapterError("claim candidates are not contiguously ranked")
            candidate = _Candidate(
                claim_id=parse_uuid(hit["claim_id"], field="candidate.claim_id"),
                semantic_score=parse_score(
                    hit["semantic_score"], field="candidate.semantic_score"
                ),
            )
            if candidate.claim_id in seen:
                raise GovernedLaneAdapterError("claim discovery returned duplicate handles")
            if prior_score is not None and candidate.semantic_score > prior_score:
                raise GovernedLaneAdapterError("claim candidates are not score ordered")
            candidates.append(candidate)
            seen.add(candidate.claim_id)
            prior_score = candidate.semantic_score
        if (
            type(raw_discovery["candidate_count"]) is not int
            or raw_discovery["candidate_count"] != len(candidates)
            or len(candidates) > self._candidate_limit
        ):
            raise GovernedLaneAdapterError("claim candidate count does not reconcile")
        candidate_hash = validate_sha256(
            raw_discovery["candidate_set_sha256"], field="candidate_set_sha256"
        )
        candidate_material = {
            "version": CANDIDATE_CONTRACT_VERSION,
            "owner_user_id": str(request.owner_user_id),
            "collection": collection,
            "query_vector_dimension": len(request.query_vector),
            "query_vector_sha256": expected_vector_sha256,
            "candidate_limit": self._candidate_limit,
            "candidate_hits": [
                {
                    "rank": rank,
                    "claim_id": str(candidate.claim_id),
                    "semantic_score": candidate.semantic_score,
                }
                for rank, candidate in enumerate(candidates, start=1)
            ],
        }
        if candidate_hash != canonical_sha256(candidate_material):
            raise GovernedLaneAdapterError(
                "claim candidate artifact manifest does not reconcile"
            )
        if not candidates:
            return build_lane_result(
                owner_user_id=request.owner_user_id,
                lane=MemoryLane.CLAIM,
                records=(),
                candidate_count=0,
                visible_candidate_count=0,
                eligible_count=0,
                candidate_set_sha256=candidate_hash,
                rejected_reason_sets=(),
                qdrant_role="candidate_ids_only",
            )

        prefixes = tuple(
            sorted(
                {
                    str(value).strip().casefold()
                    for value in self._prefix_resolver(request)
                    if str(value).strip()
                }
            )
        )
        if (
            not prefixes
            or len(prefixes) > 20
            or any(
                len(prefix) > 128
                or _PREDICATE_PERMISSION_RE.fullmatch(prefix) is None
                for prefix in prefixes
            )
        ):
            raise GovernedLaneAdapterError(
                "claim predicate resolver must return 1 to 20 closed prefixes"
            )
        batch = await self._row_loader(
            request.owner_user_id, tuple(item.claim_id for item in candidates)
        )
        if not isinstance(batch, Mapping):
            raise GovernedLaneAdapterError(
                "claim row loader must return a governed read batch"
            )
        require_fields(batch, REQUIRED_BATCH_FIELDS, source=SOURCE_NAME)
        batch_owner = parse_uuid(
            batch["owner_user_id"], field="claim_batch.owner_user_id"
        )
        if batch_owner != request.owner_user_id:
            raise GovernedLaneAdapterError(
                "claim row loader returned a cross-owner batch"
            )
        requested_ids = parse_uuid_tuple(
            batch["claim_ids"], field="claim_batch.claim_ids"
        )
        expected_ids = tuple(sorted((item.claim_id for item in candidates), key=str))
        if requested_ids != expected_ids:
            raise GovernedLaneAdapterError(
                "claim row batch differs from the candidate handles"
            )
        if (
            type(batch["database_writes"]) is not int
            or batch["database_writes"] != 0
        ):
            raise GovernedLaneAdapterError("claim row batch reported a database write")
        verify_governed_read_controls(
            batch["controls"],
            source=SOURCE_NAME,
            require_restricted_read_contract=True,
        )
        rows = batch["records"]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise GovernedLaneAdapterError("claim row loader must return a sequence")
        by_id: dict[UUID, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise GovernedLaneAdapterError("claim row loader returned a non-object")
            require_fields(row, ("claim_id",), source=SOURCE_NAME)
            claim_id = parse_uuid(row["claim_id"], field="claim.claim_id")
            if claim_id not in seen or claim_id in by_id:
                raise GovernedLaneAdapterError(
                    "claim row loader returned an unexpected or duplicate handle"
                )
            by_id[claim_id] = row

        semantic_floor = max(
            self._minimum_semantic_score,
            max(item.semantic_score for item in candidates)
            * self._relative_semantic_ratio,
        )
        evaluated: list[_Evaluated] = []
        rejected_reason_sets: list[list[RejectionCode]] = []
        for candidate in candidates:
            row = by_id.get(candidate.claim_id)
            if row is None:
                rejected_reason_sets.append([RejectionCode.NOT_VISIBLE])
                continue
            item = _typed_record(
                row=row,
                candidate=candidate,
                request=request,
                source_contract=self._source_contract,
                allowed_predicate_prefixes=prefixes,
                semantic_floor=semantic_floor,
            )
            evaluated.append(item)
            if item.reasons:
                rejected_reason_sets.append(item.reasons)

        eligible = [item for item in evaluated if not item.reasons and item.record]
        eligible.sort(
            key=lambda item: (
                -item.semantic_score,
                -item.importance,
                -item.salience,
                str(item.claim_id),
            )
        )
        selected: list[ClaimSelectionV1] = []
        used_tokens = 0
        for item in eligible:
            assert item.record is not None
            if len(selected) >= lane_limit.max_records:
                rejected_reason_sets.append([RejectionCode.LANE_RECORD_BUDGET])
                continue
            if used_tokens + item.record.token_estimate > lane_limit.max_tokens:
                rejected_reason_sets.append([RejectionCode.LANE_TOKEN_BUDGET])
                continue
            raw = item.record.model_dump()
            raw["rank"] = len(selected) + 1
            try:
                selected.append(ClaimSelectionV1.model_validate(raw))
            except Exception as exc:
                raise GovernedLaneAdapterError(
                    "claim rank reconstruction violated the typed contract"
                ) from exc
            used_tokens += item.record.token_estimate

        return build_lane_result(
            owner_user_id=request.owner_user_id,
            lane=MemoryLane.CLAIM,
            records=selected,
            candidate_count=len(candidates),
            visible_candidate_count=len(by_id),
            eligible_count=len(eligible),
            candidate_set_sha256=candidate_hash,
            rejected_reason_sets=rejected_reason_sets,
            qdrant_role="candidate_ids_only",
        )


__all__ = [
    "REQUIRED_BATCH_FIELDS",
    "REQUIRED_ROW_FIELDS",
    "SOURCE_NAME",
    "V5ClaimLaneAdapterV1",
]
