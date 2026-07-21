from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID

from .memory_v1_governed_lane_adapter_common import (
    GovernedLaneAdapterError,
    SENSITIVITY_RANK,
    build_lane_result,
    canonical_json,
    canonical_sha256,
    parse_mapping,
    parse_uuid,
    parse_uuid_tuple,
    reject_legacy_prompt_payload,
    require_fields,
    validate_sha256,
    verify_governed_read_controls,
)
from .memory_v1_selection_envelope import (
    LifePreferencePolarity,
    LifePreferenceSelectionV1,
    LifePreferenceStability,
    MemoryLane,
    MemoryLaneLimitV1,
    MemoryLaneSelectionResultV1,
    MemorySelectionRequestV1,
    RejectionCode,
    Sensitivity,
    SourceContractVersionV1,
    SurfacePolicy,
    UseInstruction,
    estimate_memory_record_tokens_v1,
)


SOURCE_NAME = "governed preference snapshot"
MAX_PREFERENCE_SCAN = 200
REQUIRED_BATCH_FIELDS = (
    "owner_user_id",
    "preferences",
    "controls",
    "database_writes",
)
REQUIRED_ROW_FIELDS = (
    "preference_id",
    "status",
    "preference_class",
    "preference_domain",
    "preference_key",
    "value",
    "polarity",
    "scope",
    "stability",
    "surface_policy",
    "sensitivity",
    "current_revision_id",
    "head_revision_number",
    "head_content_sha256",
    "head_accepted_review_id",
    "revision_id",
    "revision_number",
    "content_sha256",
    "accepted_review_id",
    "evidence_ids",
    "evidence_link_count",
    "active_evidence_count",
)
CONTENT_INTENTS = frozenset(
    {
        "life_preference_recall",
        "personal_recommendation",
        "preference_recall",
        "recommendation",
        "specific_recall",
    }
)


PreferenceSnapshotLoaderV1 = Callable[
    [UUID], Awaitable[Mapping[str, Any]]
]


def _source_is_pinned(
    request: MemorySelectionRequestV1,
    source: SourceContractVersionV1,
) -> bool:
    return any(
        item.name == source.name and item.version == source.version
        for item in request.source_contract_versions
    )


def _chain_is_current(row: Mapping[str, Any]) -> bool:
    current_revision = parse_uuid(
        row["current_revision_id"], field="preference.current_revision_id"
    )
    revision = parse_uuid(row["revision_id"], field="preference.revision_id")
    head_review = parse_uuid(
        row["head_accepted_review_id"],
        field="preference.head_accepted_review_id",
    )
    accepted_review = parse_uuid(
        row["accepted_review_id"], field="preference.accepted_review_id"
    )
    if (
        type(row["head_revision_number"]) is not int
        or type(row["revision_number"]) is not int
        or row["head_revision_number"] < 1
        or row["revision_number"] < 1
    ):
        raise GovernedLaneAdapterError(
            "preference revision numbers must be positive integers"
        )
    head_hash = validate_sha256(
        row["head_content_sha256"], field="preference.head_content_sha256"
    )
    revision_hash = validate_sha256(
        row["content_sha256"], field="preference.content_sha256"
    )
    return (
        current_revision == revision
        and row["head_revision_number"] == row["revision_number"]
        and head_hash == revision_hash
        and head_review == accepted_review
    )


def _active_evidence(row: Mapping[str, Any]) -> tuple[UUID, ...]:
    evidence = parse_uuid_tuple(row["evidence_ids"], field="preference.evidence_ids")
    if (
        type(row["evidence_link_count"]) is not int
        or type(row["active_evidence_count"]) is not int
    ):
        raise GovernedLaneAdapterError(
            "preference evidence counts must be integers"
        )
    linked = row["evidence_link_count"]
    active = row["active_evidence_count"]
    if linked < 0 or active < 0 or active > linked or linked != len(evidence):
        raise GovernedLaneAdapterError(
            "preference evidence handles and counts do not reconcile"
        )
    return evidence if active == linked else ()


def _surface_instruction(
    surface: SurfacePolicy,
) -> UseInstruction | None:
    return {
        SurfacePolicy.MENTION_WHEN_RELEVANT: UseInstruction.USE_ONLY_WHEN_RELEVANT,
        SurfacePolicy.EXPLICIT_RECALL_ONLY: UseInstruction.USE_ONLY_FOR_EXPLICIT_RECALL,
        SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL: (
            UseInstruction.USE_ONLY_FOR_RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL
        ),
    }.get(surface)


class GovernedPreferenceLaneAdapterV1:
    """Adapts governed life preferences without accepting rendered prompt strings.

    Response-preference controls remain closed with POLICY_CONTROL until the request
    contract carries a trusted entity/scope match. Selecting them globally would
    silently change their semantics.
    """

    def __init__(
        self,
        *,
        source_contract: SourceContractVersionV1,
        snapshot_loader: PreferenceSnapshotLoaderV1,
    ) -> None:
        if source_contract.name != "preference_projection":
            raise ValueError(
                "preference adapter requires the preference_projection source"
            )
        self._source_contract = source_contract
        self._snapshot_loader = snapshot_loader

    async def select(
        self,
        request: MemorySelectionRequestV1,
        lane_limit: MemoryLaneLimitV1,
    ) -> MemoryLaneSelectionResultV1:
        try:
            request = request.strict_revalidated()
        except Exception as exc:
            raise GovernedLaneAdapterError(
                "preference adapter received an invalid request"
            ) from exc
        if lane_limit.lane != MemoryLane.PREFERENCE:
            raise GovernedLaneAdapterError(
                "preference adapter received another lane's budget"
            )
        if MemoryLane.PREFERENCE not in request.requested_lanes:
            raise GovernedLaneAdapterError(
                "preference adapter was called for an unrequested lane"
            )

        batch = await self._snapshot_loader(request.owner_user_id)
        if not isinstance(batch, Mapping):
            raise GovernedLaneAdapterError(
                "preference snapshot loader must return an object"
            )
        require_fields(batch, REQUIRED_BATCH_FIELDS, source=SOURCE_NAME)
        reject_legacy_prompt_payload(batch, source=SOURCE_NAME)
        owner = parse_uuid(batch["owner_user_id"], field="preference.owner_user_id")
        if owner != request.owner_user_id:
            raise GovernedLaneAdapterError(
                "preference snapshot returned a cross-owner result"
            )
        if (
            type(batch["database_writes"]) is not int
            or batch["database_writes"] != 0
        ):
            raise GovernedLaneAdapterError(
                "preference snapshot reported a database write"
            )
        verify_governed_read_controls(
            batch["controls"],
            source=SOURCE_NAME,
            require_forced_rls=True,
        )
        rows = batch["preferences"]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise GovernedLaneAdapterError("preferences must be an array")
        if len(rows) > MAX_PREFERENCE_SCAN:
            raise GovernedLaneAdapterError(
                "preference snapshot exceeded the governed scan limit"
            )

        candidate_material: list[dict[str, str]] = []
        content_rows: list[Mapping[str, Any]] = []
        response_rows: list[Mapping[str, Any]] = []
        seen: set[UUID] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise GovernedLaneAdapterError("preference row must be an object")
            require_fields(row, REQUIRED_ROW_FIELDS, source=SOURCE_NAME)
            preference_id = parse_uuid(
                row["preference_id"], field="preference.preference_id"
            )
            if preference_id in seen:
                raise GovernedLaneAdapterError(
                    "preference snapshot returned a duplicate head"
                )
            seen.add(preference_id)
            content_hash = validate_sha256(
                row["content_sha256"], field="preference.content_sha256"
            )
            candidate_material.append(
                {"record_id": str(preference_id), "content_sha256": content_hash}
            )
            preference_class = str(row["preference_class"] or "").casefold()
            if preference_class == "life":
                content_rows.append(row)
            elif preference_class == "response":
                response_rows.append(row)
            else:
                raise GovernedLaneAdapterError(
                    "preference class is outside the governed closed enum"
                )

        candidate_material.sort(key=lambda item: item["record_id"])
        source_pinned = _source_is_pinned(request, self._source_contract)
        rejected_reason_sets: list[list[RejectionCode]] = []
        eligible_records: list[LifePreferenceSelectionV1] = []

        for row in content_rows:
            evidence = _active_evidence(row)
            sensitivity_value = str(row["sensitivity"] or "").casefold()
            if sensitivity_value not in SENSITIVITY_RANK:
                raise GovernedLaneAdapterError(
                    "preference sensitivity is outside the closed enum"
                )
            sensitivity = Sensitivity(sensitivity_value)
            surface_value = str(row["surface_policy"] or "").casefold()
            try:
                surface = SurfacePolicy(surface_value)
            except ValueError:
                surface = SurfacePolicy.NEVER
            instruction = _surface_instruction(surface)
            domain = str(row["preference_domain"] or "").casefold()
            scope = parse_mapping(row["scope"], field="preference.scope")
            reasons: list[RejectionCode] = []
            if not source_pinned:
                reasons.append(RejectionCode.SOURCE_VERSION)
            if str(row["status"] or "").casefold() != "active":
                reasons.append(RejectionCode.INACTIVE)
            if not _chain_is_current(row):
                reasons.append(RejectionCode.SUPERSEDED)
            if not evidence:
                reasons.append(RejectionCode.NO_ACTIVE_EVIDENCE)
            if SENSITIVITY_RANK[sensitivity_value] > SENSITIVITY_RANK[
                request.max_sensitivity.value
            ]:
                reasons.append(RejectionCode.SENSITIVITY)
            if request.memory_intent not in CONTENT_INTENTS:
                reasons.append(RejectionCode.MEMORY_INTENT)
            if request.domains and domain not in request.domains:
                reasons.append(RejectionCode.MEMORY_INTENT)
            context = str(scope.get("context") or "").strip().casefold()
            if (
                request.memory_intent
                in {"recommendation", "personal_recommendation"}
                and context
                and context != f"{domain}_recommendation"
            ):
                reasons.append(RejectionCode.MEMORY_INTENT)
            if instruction is None:
                reasons.append(RejectionCode.SURFACE_POLICY)
            elif surface == SurfacePolicy.EXPLICIT_RECALL_ONLY and not request.explicit_recall:
                reasons.append(RejectionCode.MEMORY_INTENT)
            elif (
                surface
                == SurfacePolicy.RELEVANT_RECOMMENDATION_OR_EXPLICIT_RECALL
                and not request.explicit_recall
                and request.memory_intent
                not in {"recommendation", "personal_recommendation"}
            ):
                reasons.append(RejectionCode.MEMORY_INTENT)

            if reasons:
                rejected_reason_sets.append(reasons)
                continue
            try:
                polarity = LifePreferencePolarity(
                    str(row["polarity"] or "").casefold()
                )
                stability = LifePreferenceStability(
                    str(row["stability"] or "").casefold()
                )
                assert instruction is not None
                record = LifePreferenceSelectionV1(
                    owner_user_id=owner,
                    lane=MemoryLane.PREFERENCE,
                    record_id=parse_uuid(
                        row["preference_id"], field="preference.preference_id"
                    ),
                    revision_id=parse_uuid(
                        row["revision_id"], field="preference.revision_id"
                    ),
                    source_contract=self._source_contract,
                    source_content_sha256=validate_sha256(
                        row["content_sha256"],
                        field="preference.content_sha256",
                    ),
                    rank=1,
                    surface_policy=surface,
                    sensitivity=sensitivity,
                    use_instruction=instruction,
                    token_estimate=1,
                    valid_from=None,
                    valid_to=None,
                    superseded_by=None,
                    evidence_refs=evidence,
                    observation_refs=(),
                    preference_key=str(row["preference_key"] or "").casefold(),
                    preference_class="life",
                    preference_domain=domain,
                    canonical_value_json=canonical_json(
                        row["value"], field="preference.value"
                    ),
                    polarity=polarity,
                    stability=stability,
                )
                raw = record.model_dump()
                raw["token_estimate"] = estimate_memory_record_tokens_v1(record)
                record = LifePreferenceSelectionV1.model_validate(raw)
            except Exception as exc:
                raise GovernedLaneAdapterError(
                    "preference row violates the typed Memory V1 selection contract"
                ) from exc
            eligible_records.append(record)

        eligible_records.sort(key=lambda item: (item.preference_key, str(item.record_id)))
        selected: list[LifePreferenceSelectionV1] = []
        used_tokens = 0
        for record in eligible_records:
            if len(selected) >= lane_limit.max_records:
                rejected_reason_sets.append([RejectionCode.LANE_RECORD_BUDGET])
                continue
            if used_tokens + record.token_estimate > lane_limit.max_tokens:
                rejected_reason_sets.append([RejectionCode.LANE_TOKEN_BUDGET])
                continue
            raw = record.model_dump()
            raw["rank"] = len(selected) + 1
            try:
                selected.append(LifePreferenceSelectionV1.model_validate(raw))
            except Exception as exc:
                raise GovernedLaneAdapterError(
                    "preference rank reconstruction violated the typed contract"
                ) from exc
            used_tokens += record.token_estimate

        # The frozen request has no trusted entity set or direct-relevance result.
        # Every response control therefore remains visible but ineligible.
        rejected_control_reason_sets = [
            [RejectionCode.POLICY_CONTROL] for _ in response_rows
        ]
        return build_lane_result(
            owner_user_id=owner,
            lane=MemoryLane.PREFERENCE,
            records=selected,
            candidate_count=len(content_rows),
            visible_candidate_count=len(content_rows),
            eligible_count=len(eligible_records),
            candidate_set_sha256=canonical_sha256(candidate_material),
            rejected_reason_sets=rejected_reason_sets,
            qdrant_role="not_used",
            controls=(),
            control_candidate_count=len(response_rows),
            rejected_control_reason_sets=rejected_control_reason_sets,
        )


__all__ = [
    "MAX_PREFERENCE_SCAN",
    "REQUIRED_BATCH_FIELDS",
    "REQUIRED_ROW_FIELDS",
    "SOURCE_NAME",
    "GovernedPreferenceLaneAdapterV1",
]
