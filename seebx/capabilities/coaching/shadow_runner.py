from __future__ import annotations

"""Runtime-neutral self-owned LifeSwitch Coaching Context V2 shadow runner.

The runner consumes already-selected V1 context and server-created transaction
authority. It performs no reads, writes, prompt rendering, model exposure,
persistence, network calls, or runtime registration.
"""

import datetime as dt
import re
import uuid
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.capabilities.coaching.contracts import (
    AuthorizationBindingV2,
    AuthorizationBudgetV2,
    AuthorizationRechecksV2,
    AuthorizationSnapshotV2,
    CoachingCapabilitiesV2,
    CoachingContextBindingV2,
    ProjectionAuthorizationDecisionV2,
    ProjectionEnvelopeV1,
    ProjectionSelectionResultV1,
    SelectedProjectionDecisionV1,
    StructuredCoachingContextV2,
    canonical_sha256,
)
from seebx.capabilities.coaching.shadow_adapter import (
    LifeSwitchSelfShadowAdaptationV1,
    SELF_S1_SHADOW_ADAPTER_MAP_V1,
    SELF_S1_SHADOW_FIELD_POLICY,
    adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2,
)
from seebx.capabilities.plans.domain_context import (
    LifeSwitchDomainContextEnvelopeV1,
    TrustedLifeSwitchContextRequestV1,
)


SELF_S1_SHADOW_RUNNER_CONTRACT = "lifeswitch_self_s1_shadow_runner_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LIFECYCLE_STATES = (
    "present",
    "explicit_zero",
    "missing",
    "absent",
    "in_progress",
    "incomplete",
    "corrected",
    "deleted",
)


class LifeSwitchSelfShadowRunnerError(ValueError):
    """The supplied V1 selection is not eligible for the self-owned runner."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
        validate_default=True,
    )


class SelfShadowExecutionAuthorityV1(_StrictFrozenModel):
    """Server-created authority from one restricted read-only transaction."""

    context_snapshot_id: str = Field(min_length=36, max_length=36)
    transaction_snapshot_digest: str
    authorization_epoch: str = Field(min_length=1, max_length=256)
    evaluated_at: str
    transaction_isolation: Literal["repeatable_read"] = "repeatable_read"
    transaction_access: Literal["read_only"] = "read_only"
    pre_retrieval_recheck: Literal["pass"] = "pass"
    pre_prompt_recheck: Literal["pass"] = "pass"
    delivery_recheck: Literal["not_run"] = "not_run"

    @field_validator("context_snapshot_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError("context snapshot must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError("context snapshot UUID must be canonical")
        return value

    @field_validator("transaction_snapshot_digest")
    @classmethod
    def snapshot_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("transaction snapshot digest must be a SHA-256")
        return value

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware(cls, value: str) -> str:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("evaluated_at must be RFC 3339") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("evaluated_at must include an offset")
        return value


class SelfShadowInspectionV1(_StrictFrozenModel):
    """Content-free evaluation surface; never contains user values or IDs."""

    contract_version: Literal["lifeswitch_self_s1_shadow_inspection_v1"] = (
        "lifeswitch_self_s1_shadow_inspection_v1"
    )
    outcome: Literal["selected"] = "selected"
    perspective: Literal["self"] = "self"
    source_status: Literal["SELECTED", "PARTIAL"]
    source_projection: str = Field(min_length=1, max_length=80)
    target_projection_id: str = Field(min_length=1, max_length=80)
    target_status: Literal[
        "available",
        "partial",
        "unavailable",
        "not_captured",
        "decision_blocked",
        "error",
    ]
    serialized_projection_bytes: int = Field(ge=0, le=32_768)
    source_record_count: int = Field(ge=0)
    present_field_count: int = Field(ge=0)
    explicit_zero_field_count: int = Field(ge=0)
    missing_field_count: int = Field(ge=0)
    absent_field_count: int = Field(ge=0)
    in_progress_field_count: int = Field(ge=0)
    incomplete_field_count: int = Field(ge=0)
    corrected_field_count: int = Field(ge=0)
    deleted_field_count: int = Field(ge=0)
    decision_blocks: tuple[str, ...] = ()
    additional_database_reads: Literal[0] = 0
    prompt_influence: Literal[False] = False
    model_exposure: Literal[False] = False
    persistence: Literal[False] = False
    delivery_recheck: Literal["not_run"] = "not_run"

    @field_validator("decision_blocks")
    @classmethod
    def unique_decision_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate decision block")
        return value


class LifeSwitchSelfShadowRunV1(_StrictFrozenModel):
    contract_version: Literal[SELF_S1_SHADOW_RUNNER_CONTRACT] = (
        SELF_S1_SHADOW_RUNNER_CONTRACT
    )
    source_envelope_sha256: str
    context_bridge_sha256: str
    authorization_snapshot_sha256: str
    adaptation_sha256: str
    structured_context_sha256: str
    structured_context: StructuredCoachingContextV2 = Field(repr=False)
    adaptation: LifeSwitchSelfShadowAdaptationV1 = Field(repr=False)
    inspection: SelfShadowInspectionV1
    capabilities: CoachingCapabilitiesV2 = Field(default_factory=CoachingCapabilitiesV2)
    run_sha256: str

    @field_validator(
        "source_envelope_sha256",
        "context_bridge_sha256",
        "authorization_snapshot_sha256",
        "adaptation_sha256",
        "structured_context_sha256",
        "run_sha256",
    )
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_run(self) -> "LifeSwitchSelfShadowRunV1":
        if self.source_envelope_sha256 != self.adaptation.source_envelope_sha256:
            raise ValueError("source envelope hash mismatch")
        if self.context_bridge_sha256 != self.adaptation.context_bridge_sha256:
            raise ValueError("context bridge hash mismatch")
        if (
            self.authorization_snapshot_sha256
            != self.adaptation.authorization_snapshot_sha256
        ):
            raise ValueError("authorization snapshot hash mismatch")
        if self.adaptation_sha256 != self.adaptation.adaptation_sha256:
            raise ValueError("adaptation hash mismatch")
        if self.structured_context_sha256 != canonical_sha256(
            self.structured_context
        ):
            raise ValueError("structured context hash mismatch")
        if self.structured_context.projections != (self.adaptation.result,):
            raise ValueError("structured context differs from adapted projection")
        if (
            self.inspection.target_projection_id
            != self.adaptation.target_projection_id
        ):
            raise ValueError("inspection target differs from adaptation")
        payload = self.model_dump(
            mode="json",
            exclude={"structured_context", "adaptation", "run_sha256"},
        )
        if self.run_sha256 != canonical_sha256(payload):
            raise ValueError("shadow run hash mismatch")
        return self


def _fail(message: str) -> None:
    raise LifeSwitchSelfShadowRunnerError(message)


def _authorization(
    *,
    request: TrustedLifeSwitchContextRequestV1,
    authority: SelfShadowExecutionAuthorityV1,
    target_projection_id: str,
    required_scopes: tuple[str, ...],
    serialized_projection_bytes: int,
) -> AuthorizationSnapshotV2:
    owner = str(request.owner_user_id)
    return AuthorizationSnapshotV2(
        binding=AuthorizationBindingV2(
            request_id=request.request_id,
            thread_id=str(request.thread_id),
            context_snapshot_id=authority.context_snapshot_id,
            actor_user_id=owner,
            subject_user_id=owner,
            perspective="self",
            account_timezone=request.owner_timezone,
            evaluated_at=authority.evaluated_at,
            transaction_snapshot_digest=authority.transaction_snapshot_digest,
        ),
        decision="allow",
        authorization_epoch=authority.authorization_epoch,
        sensitivity_ceiling="S1",
        relationship_basis="self",
        relationship_binding_digest=None,
        effective_grants=(),
        projection_decisions=(
            ProjectionAuthorizationDecisionV2(
                projection_id=target_projection_id,
                outcome="authorized",
                executed=True,
                required_scopes=required_scopes,
                sensitivity_ceiling="S1",
                field_policy_version=SELF_S1_SHADOW_FIELD_POLICY,
                internal_reason_code=None,
            ),
        ),
        rechecks=AuthorizationRechecksV2(
            pre_retrieval=authority.pre_retrieval_recheck,
            pre_prompt=authority.pre_prompt_recheck,
            delivery=authority.delivery_recheck,
        ),
        budget=AuthorizationBudgetV2(
            selected_projection_count=1,
            serialized_projection_bytes=serialized_projection_bytes,
            truncation_reasons=(),
        ),
    )


def _inspection(
    envelope: LifeSwitchDomainContextEnvelopeV1,
    adaptation: LifeSwitchSelfShadowAdaptationV1,
) -> SelfShadowInspectionV1:
    section = envelope.sections[0]
    result = adaptation.result
    states: Counter[str] = Counter()
    decision_blocks: tuple[str, ...] = ()
    if isinstance(result, ProjectionEnvelopeV1):
        states.update(item.state for item in result.field_states)
        decision_blocks = tuple(result.decision_blocks)
    return SelfShadowInspectionV1(
        source_status=envelope.status,
        source_projection=section.projection,
        target_projection_id=adaptation.target_projection_id,
        target_status=result.status,
        serialized_projection_bytes=adaptation.serialized_projection_bytes,
        source_record_count=section.record_count,
        present_field_count=states["present"],
        explicit_zero_field_count=states["explicit_zero"],
        missing_field_count=states["missing"],
        absent_field_count=states["absent"],
        in_progress_field_count=states["in_progress"],
        incomplete_field_count=states["incomplete"],
        corrected_field_count=states["corrected"],
        deleted_field_count=states["deleted"],
        decision_blocks=decision_blocks,
    )


def run_lifeswitch_self_s1_shadow_v2(
    *,
    request: TrustedLifeSwitchContextRequestV1,
    envelope: LifeSwitchDomainContextEnvelopeV1,
    authority: SelfShadowExecutionAuthorityV1,
) -> LifeSwitchSelfShadowRunV1:
    """Create one exact V2 context without rendering or exposing it to a model."""

    if request.authenticated_actor_user_id != request.owner_user_id:
        _fail("self shadow request requires actor and owner equality")
    if envelope.status not in {"SELECTED", "PARTIAL"}:
        _fail("self shadow runner requires selected V1 context")
    if len(envelope.sections) != 1:
        _fail("self shadow runner requires exactly one V1 section")
    section = envelope.sections[0]
    mapping = SELF_S1_SHADOW_ADAPTER_MAP_V1.get(section.projection)
    if mapping is None:
        _fail("V1 projection is not approved for the self shadow runner")
    target_projection_id = mapping["target_projection_id"]
    required_scopes = mapping["required_scopes"]
    context_bridge_sha256 = canonical_sha256(
        {
            "context_snapshot_id": authority.context_snapshot_id,
            "conversation_snapshot_sha256": request.conversation_snapshot_sha256,
        }
    )

    sizing_authorization = _authorization(
        request=request,
        authority=authority,
        target_projection_id=target_projection_id,
        required_scopes=required_scopes,
        serialized_projection_bytes=0,
    )
    sizing_adaptation = adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
        request=request,
        envelope=envelope,
        authorization=sizing_authorization,
        context_bridge_sha256=context_bridge_sha256,
    )
    authorization = _authorization(
        request=request,
        authority=authority,
        target_projection_id=target_projection_id,
        required_scopes=required_scopes,
        serialized_projection_bytes=sizing_adaptation.serialized_projection_bytes,
    )
    adaptation = adapt_lifeswitch_v1_envelope_to_self_shadow_projection_v2(
        request=request,
        envelope=envelope,
        authorization=authorization,
        context_bridge_sha256=context_bridge_sha256,
    )
    if adaptation.result != sizing_adaptation.result:
        _fail("projection changed while binding exact authorization bytes")

    context = StructuredCoachingContextV2(
        binding=CoachingContextBindingV2(
            request_id=request.request_id,
            thread_id=str(request.thread_id),
            context_snapshot_id=authority.context_snapshot_id,
            actor_user_id=str(request.owner_user_id),
            subject_user_id=str(request.owner_user_id),
            account_timezone=request.owner_timezone,
            as_of_utc=authority.evaluated_at,
            as_of_local_date=envelope.as_of_local_date.isoformat(),
            authorization_epoch=authority.authorization_epoch,
        ),
        authorization=authorization,
        selection=ProjectionSelectionResultV1(
            requested_count=1,
            selected_count=1,
            decisions=(
                SelectedProjectionDecisionV1(
                    projection_id=target_projection_id,
                    execution_ordinal=1,
                ),
            ),
            truncated=False,
        ),
        projections=(adaptation.result,),
        unavailable_domains=(),
        serialized_projection_bytes=adaptation.serialized_projection_bytes,
        capabilities=CoachingCapabilitiesV2(),
    )
    inspection = _inspection(envelope, adaptation)
    payload = {
        "contract_version": SELF_S1_SHADOW_RUNNER_CONTRACT,
        "source_envelope_sha256": envelope.envelope_sha256,
        "context_bridge_sha256": context_bridge_sha256,
        "authorization_snapshot_sha256": canonical_sha256(authorization),
        "adaptation_sha256": adaptation.adaptation_sha256,
        "structured_context_sha256": canonical_sha256(context),
        "inspection": inspection.model_dump(mode="json"),
        "capabilities": CoachingCapabilitiesV2().model_dump(mode="json"),
    }
    return LifeSwitchSelfShadowRunV1(
        source_envelope_sha256=envelope.envelope_sha256,
        context_bridge_sha256=context_bridge_sha256,
        authorization_snapshot_sha256=canonical_sha256(authorization),
        adaptation_sha256=adaptation.adaptation_sha256,
        structured_context_sha256=canonical_sha256(context),
        structured_context=context,
        adaptation=adaptation,
        inspection=inspection,
        capabilities=CoachingCapabilitiesV2(),
        run_sha256=canonical_sha256(payload),
    )


__all__ = [
    "LifeSwitchSelfShadowRunV1",
    "LifeSwitchSelfShadowRunnerError",
    "SELF_S1_SHADOW_RUNNER_CONTRACT",
    "SelfShadowExecutionAuthorityV1",
    "SelfShadowInspectionV1",
    "run_lifeswitch_self_s1_shadow_v2",
]
