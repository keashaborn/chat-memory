from __future__ import annotations

from typing import Any, Awaitable, Mapping, Protocol, Sequence
from uuid import UUID

from .memory_v1_governed_lane_adapter_common import (
    GovernedLaneAdapterError,
    SENSITIVITY_RANK,
    build_lane_result,
    canonical_sha256,
    parse_optional_uuid,
    parse_text,
    parse_utc,
    parse_uuid,
    parse_uuid_tuple,
    require_fields,
    validate_sha256,
    verify_governed_read_controls,
)
from .memory_v1_selection_envelope import (
    MemoryLane,
    MemoryLaneLimitV1,
    MemoryLaneSelectionResultV1,
    MemorySelectionRequestV1,
    ProjectAuthorityLevel,
    ProjectDocumentState,
    ProjectKnowledgeKind,
    ProjectKnowledgeSelectionV1,
    RejectionCode,
    Sensitivity,
    SourceContractVersionV1,
    SurfacePolicy,
    UseInstruction,
    estimate_memory_record_tokens_v1,
)


SOURCE_NAME = "memory.read_v5_shadow_project_knowledge"
REQUIRED_BATCH_FIELDS = (
    "owner_user_id",
    "thread_id",
    "project_key",
    "component_key",
    "records",
    "controls",
    "database_writes",
)
REQUIRED_ROW_FIELDS = (
    "owner_user_id",
    "thread_id",
    "project_id",
    "project_key",
    "component_id",
    "component_key",
    "knowledge_id",
    "knowledge_kind",
    "knowledge_key",
    "canonical_text",
    "status",
    "revision_id",
    "revision_number",
    "document_state",
    "authority_level",
    "surface_policy",
    "content_sha256",
    "sensitivity",
    "valid_from",
    "valid_to",
    "superseded_by",
    "effective_precision",
    "effective_instant_at",
    "effective_calendar_range",
    "effective_instant_range",
    "projection_review_decision",
    "projection_apply_outcome",
    "evidence_ids",
    "observation_ids",
    "source_rank",
)


class ProjectRowLoaderV1(Protocol):
    def __call__(
        self,
        owner_user_id: UUID,
        thread_id: UUID,
        *,
        limit: int,
    ) -> Awaitable[Mapping[str, Any]]: ...


def _source_is_pinned(
    request: MemorySelectionRequestV1,
    source: SourceContractVersionV1,
) -> bool:
    return any(
        item.name == source.name and item.version == source.version
        for item in request.source_contract_versions
    )


class V5ProjectLaneAdapterV1:
    """Adapts owner/thread-scoped V5 project rows into typed project records."""

    def __init__(
        self,
        *,
        source_contract: SourceContractVersionV1,
        row_loader: ProjectRowLoaderV1,
        scan_limit: int = 8,
    ) -> None:
        if source_contract.name != "project_projection":
            raise ValueError("project adapter requires the project_projection source")
        if not 1 <= scan_limit <= 8:
            raise ValueError("scan_limit must be between 1 and 8")
        self._source_contract = source_contract
        self._row_loader = row_loader
        self._scan_limit = scan_limit

    async def select(
        self,
        request: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1:
        try:
            request = request.strict_revalidated()
        except Exception as exc:
            raise GovernedLaneAdapterError(
                "project adapter received an invalid request"
            ) from exc
        if lane_limit.lane != MemoryLane.PROJECT_KNOWLEDGE:
            raise GovernedLaneAdapterError(
                "project adapter received another lane's budget"
            )
        if MemoryLane.PROJECT_KNOWLEDGE not in request.requested_lanes:
            raise GovernedLaneAdapterError(
                "project adapter was called for an unrequested lane"
            )
        if request.thread_id is None or request.project_key is None:
            raise GovernedLaneAdapterError(
                "project selection requires trusted thread and project scope"
            )

        batch = await self._row_loader(
            request.owner_user_id, request.thread_id, limit=self._scan_limit
        )
        if not isinstance(batch, Mapping):
            raise GovernedLaneAdapterError(
                "project row loader must return a governed read batch"
            )
        require_fields(batch, REQUIRED_BATCH_FIELDS, source=SOURCE_NAME)
        if parse_uuid(
            batch["owner_user_id"], field="project_batch.owner_user_id"
        ) != request.owner_user_id:
            raise GovernedLaneAdapterError(
                "project row loader returned a cross-owner batch"
            )
        if parse_uuid(
            batch["thread_id"], field="project_batch.thread_id"
        ) != request.thread_id:
            raise GovernedLaneAdapterError(
                "project row loader returned a cross-thread batch"
            )
        if (
            batch["project_key"] != request.project_key
            or batch["component_key"] != request.component_key
        ):
            raise GovernedLaneAdapterError(
                "project row batch differs from the trusted project scope"
            )
        if (
            type(batch["database_writes"]) is not int
            or batch["database_writes"] != 0
        ):
            raise GovernedLaneAdapterError("project row batch reported a database write")
        verify_governed_read_controls(
            batch["controls"],
            source=SOURCE_NAME,
            require_restricted_read_contract=True,
        )
        rows = batch["records"]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise GovernedLaneAdapterError("project row loader must return an array")
        if len(rows) > self._scan_limit:
            raise GovernedLaneAdapterError(
                "project row loader exceeded the governed scan limit"
            )

        source_pinned = _source_is_pinned(request, self._source_contract)
        candidate_material: list[dict[str, str]] = []
        eligible: list[ProjectKnowledgeSelectionV1] = []
        rejected_reason_sets: list[list[RejectionCode]] = []
        seen: set[UUID] = set()

        for expected_rank, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise GovernedLaneAdapterError("project row must be an object")
            require_fields(row, REQUIRED_ROW_FIELDS, source=SOURCE_NAME)
            if type(row["source_rank"]) is not int or row["source_rank"] != expected_rank:
                raise GovernedLaneAdapterError(
                    "project rows are not contiguously source-ranked"
                )
            owner = parse_uuid(row["owner_user_id"], field="project.owner_user_id")
            thread_id = parse_uuid(row["thread_id"], field="project.thread_id")
            if owner != request.owner_user_id:
                raise GovernedLaneAdapterError("project loader returned a cross-owner row")
            if thread_id != request.thread_id:
                raise GovernedLaneAdapterError("project loader returned a cross-thread row")
            knowledge_id = parse_uuid(
                row["knowledge_id"], field="project.knowledge_id"
            )
            if knowledge_id in seen:
                raise GovernedLaneAdapterError(
                    "project loader returned a duplicate knowledge head"
                )
            seen.add(knowledge_id)
            content_hash = validate_sha256(
                row["content_sha256"], field="project.content_sha256"
            )
            candidate_material.append(
                {
                    "source_rank": str(expected_rank),
                    "record_id": str(knowledge_id),
                    "content_sha256": content_hash,
                }
            )

            project_id = parse_uuid(row["project_id"], field="project.project_id")
            project_key = str(row["project_key"] or "").strip()
            component_id = parse_optional_uuid(
                row["component_id"], field="project.component_id"
            )
            component_key = str(row["component_key"] or "").strip() or None
            revision_id = parse_uuid(row["revision_id"], field="project.revision_id")
            if (
                type(row["revision_number"]) is not int
                or row["revision_number"] < 1
            ):
                raise GovernedLaneAdapterError(
                    "project revision number must be a positive integer"
                )
            valid_from = parse_utc(row["valid_from"], field="project.valid_from")
            valid_to = parse_utc(row["valid_to"], field="project.valid_to")
            superseded_by = parse_optional_uuid(
                row["superseded_by"], field="project.superseded_by"
            )
            evidence = parse_uuid_tuple(
                row["evidence_ids"], field="project.evidence_ids"
            )
            observations = parse_uuid_tuple(
                row["observation_ids"], field="project.observation_ids"
            )
            if any(
                row[field] is not None
                for field in (
                    "effective_precision",
                    "effective_instant_at",
                    "effective_calendar_range",
                    "effective_instant_range",
                )
            ):
                raise GovernedLaneAdapterError(
                    "project temporal normalization contract is not frozen"
                )
            if valid_from is not None or valid_to is not None:
                raise GovernedLaneAdapterError(
                    "atemporal project rows cannot claim a normalized interval"
                )
            sensitivity_value = str(row["sensitivity"] or "").casefold()
            if sensitivity_value not in SENSITIVITY_RANK:
                raise GovernedLaneAdapterError(
                    "project sensitivity is outside the closed enum"
                )
            sensitivity = Sensitivity(sensitivity_value)
            surface_value = str(row["surface_policy"] or "").casefold()
            try:
                surface = SurfacePolicy(surface_value)
            except ValueError:
                surface = SurfacePolicy.NEVER
            state_value = str(row["document_state"] or "").casefold()
            authority_value = str(row["authority_level"] or "").casefold()
            kind_value = str(row["knowledge_kind"] or "").casefold()

            reasons: list[RejectionCode] = []
            if not source_pinned:
                reasons.append(RejectionCode.SOURCE_VERSION)
            if row["projection_review_decision"] not in {
                "authorized",
                "auto_apply_eligible",
            }:
                reasons.append(RejectionCode.PROJECTION_NOT_AUTHORIZED)
            if row["projection_apply_outcome"] != "applied":
                reasons.append(RejectionCode.PROJECTION_NOT_APPLIED)
            if str(row["status"] or "").casefold() != "active":
                reasons.append(RejectionCode.INACTIVE)
            if not evidence:
                reasons.append(RejectionCode.NO_ACTIVE_EVIDENCE)
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
            if superseded_by is not None or state_value == "superseded":
                reasons.append(RejectionCode.SUPERSEDED)
            if (
                project_key != request.project_key
                or component_key != request.component_key
            ):
                reasons.append(RejectionCode.PROJECT_SCOPE)
            if surface != SurfacePolicy.EXACT_PROJECT_SCOPE_ONLY:
                reasons.append(RejectionCode.SURFACE_POLICY)

            if reasons:
                rejected_reason_sets.append(reasons)
                continue
            try:
                record = ProjectKnowledgeSelectionV1(
                    owner_user_id=owner,
                    lane=MemoryLane.PROJECT_KNOWLEDGE,
                    record_id=knowledge_id,
                    revision_id=revision_id,
                    source_contract=self._source_contract,
                    source_content_sha256=content_hash,
                    rank=1,
                    surface_policy=surface,
                    sensitivity=sensitivity,
                    use_instruction=UseInstruction.USE_ONLY_INSIDE_EXACT_PROJECT_SCOPE,
                    token_estimate=1,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    superseded_by=superseded_by,
                    evidence_refs=evidence,
                    observation_refs=observations,
                    text=parse_text(
                        row["canonical_text"], field="project.canonical_text"
                    ),
                    project_id=project_id,
                    project_key=project_key,
                    component_id=component_id,
                    component_key=component_key,
                    knowledge_key=str(row["knowledge_key"] or "").casefold(),
                    knowledge_kind=ProjectKnowledgeKind(kind_value),
                    document_state=ProjectDocumentState(state_value),
                    authority_level=ProjectAuthorityLevel(authority_value),
                )
                raw = record.model_dump()
                raw["token_estimate"] = estimate_memory_record_tokens_v1(record)
                record = ProjectKnowledgeSelectionV1.model_validate(raw)
            except Exception as exc:
                raise GovernedLaneAdapterError(
                    "project row violates the typed Memory V1 selection contract"
                ) from exc
            eligible.append(record)

        selected: list[ProjectKnowledgeSelectionV1] = []
        used_tokens = 0
        for record in eligible:
            if len(selected) >= lane_limit.max_records:
                rejected_reason_sets.append([RejectionCode.LANE_RECORD_BUDGET])
                continue
            if used_tokens + record.token_estimate > lane_limit.max_tokens:
                rejected_reason_sets.append([RejectionCode.LANE_TOKEN_BUDGET])
                continue
            raw = record.model_dump()
            raw["rank"] = len(selected) + 1
            try:
                selected.append(ProjectKnowledgeSelectionV1.model_validate(raw))
            except Exception as exc:
                raise GovernedLaneAdapterError(
                    "project rank reconstruction violated the typed contract"
                ) from exc
            used_tokens += record.token_estimate

        return build_lane_result(
            owner_user_id=request.owner_user_id,
            lane=MemoryLane.PROJECT_KNOWLEDGE,
            records=selected,
            candidate_count=len(rows),
            visible_candidate_count=len(rows),
            eligible_count=len(eligible),
            candidate_set_sha256=canonical_sha256(candidate_material),
            rejected_reason_sets=rejected_reason_sets,
            qdrant_role="not_used",
        )


__all__ = [
    "REQUIRED_BATCH_FIELDS",
    "REQUIRED_ROW_FIELDS",
    "SOURCE_NAME",
    "V5ProjectLaneAdapterV1",
]
