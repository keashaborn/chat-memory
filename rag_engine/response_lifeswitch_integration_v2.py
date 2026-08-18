from __future__ import annotations

"""Additive integration plan joining response policy and LifeSwitch context."""

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_prompt_integration_v2 import (
    AssembledPromptV3,
    LifeSwitchPromptAugmentationRequestV2,
    assemble_prompt_with_lifeswitch_v2,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from rag_engine.prior_lifeswitch_provenance_v1 import (
    PriorLifeSwitchProvenanceEnvelopeV1,
)
from rag_engine.lifeswitch_prior_answer_provenance_runtime_v1 import (
    PriorLifeSwitchPreparedContextV1,
)
from seebx.capabilities.conversation.orchestration import TrustedResponsePlanV0_2
from rag_engine.response_source_awareness_v1 import (
    lifeswitch_source_status_v1,
)


TRUSTED_LIFESWITCH_RESPONSE_PLAN_V2_VERSION = "trusted_response_plan_v0_8"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class TrustedLifeSwitchResponsePlanV2(_StrictFrozenModel):
    contract_version: Literal[TRUSTED_LIFESWITCH_RESPONSE_PLAN_V2_VERSION] = (
        TRUSTED_LIFESWITCH_RESPONSE_PLAN_V2_VERSION
    )
    base_response_plan: TrustedResponsePlanV0_2 = Field(repr=False)
    lifeswitch_context: LifeSwitchPreparedContextV1 = Field(repr=False)
    prior_lifeswitch_provenance: PriorLifeSwitchProvenanceEnvelopeV1 | None = Field(
        default=None,
        repr=False,
    )
    prior_lifeswitch_status: Literal["OFF", "EMPTY", "SELECTED", "UNAVAILABLE"]
    prior_lifeswitch_database_accessed: bool
    assembled_prompt: AssembledPromptV3 = Field(repr=False)
    base_response_plan_sha256: str
    lifeswitch_context_manifest_sha256: str
    assembled_prompt_sha256: str
    plan_sha256: str

    @field_validator(
        "base_response_plan_sha256",
        "lifeswitch_context_manifest_sha256",
        "assembled_prompt_sha256",
        "plan_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_plan(self) -> "TrustedLifeSwitchResponsePlanV2":
        base = TrustedResponsePlanV0_2.model_validate_json(
            self.base_response_plan.model_dump_json()
        )
        assembled = AssembledPromptV3.from_wire_json(
            self.assembled_prompt.canonical_json_bytes()
        )
        if self.base_response_plan_sha256 != base.plan_sha256:
            raise ValueError("LifeSwitch plan differs from base response plan")
        if (
            self.lifeswitch_context_manifest_sha256
            != self.lifeswitch_context.manifest_sha256
        ):
            raise ValueError("LifeSwitch plan differs from prepared context")
        if self.assembled_prompt_sha256 != assembled.manifest.assembly_sha256:
            raise ValueError("LifeSwitch plan differs from assembled prompt")
        if (
            assembled.source_request.lifeswitch_source_status
            is not lifeswitch_source_status_v1(self.lifeswitch_context.status)
            or assembled.source_request.lifeswitch_database_accessed
            is not self.lifeswitch_context.database_accessed
        ):
            raise ValueError("LifeSwitch source status differs from prepared context")
        prior = self.prior_lifeswitch_provenance
        if self.prior_lifeswitch_status == "SELECTED":
            if prior is None or not self.prior_lifeswitch_database_accessed:
                raise ValueError("selected prior LifeSwitch provenance is incomplete")
        elif prior is not None:
            raise ValueError("non-selected prior LifeSwitch status carries provenance")
        if self.prior_lifeswitch_status == "OFF" and self.prior_lifeswitch_database_accessed:
            raise ValueError("OFF prior LifeSwitch status cannot access the database")
        if prior is not None:
            if assembled.manifest.prior_lifeswitch_provenance_manifest_sha256 != prior.manifest_sha256:
                raise ValueError("response plan differs from prior LifeSwitch provenance")
        elif assembled.manifest.prior_lifeswitch_provenance_manifest_sha256 is not None:
            raise ValueError("response plan omits exposed prior LifeSwitch provenance")
        if (
            assembled.source_request.base_assembly.manifest.assembly_sha256
            != base.assembled_prompt.manifest.assembly_sha256
        ):
            raise ValueError("LifeSwitch prompt does not extend the base plan")
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        if self.plan_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch response plan hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        base_response_plan: TrustedResponsePlanV0_2,
        lifeswitch_context: LifeSwitchPreparedContextV1,
        prior_lifeswitch_context: PriorLifeSwitchPreparedContextV1,
    ) -> "TrustedLifeSwitchResponsePlanV2":
        base = TrustedResponsePlanV0_2.model_validate_json(
            base_response_plan.model_dump_json()
        )
        envelope = lifeswitch_context.envelope
        if envelope is not None:
            if envelope.owner_user_id != base.authenticated_actor_user_id:
                raise ValueError("LifeSwitch owner differs from response plan")
            if envelope.thread_id != base.thread_id:
                raise ValueError("LifeSwitch thread differs from response plan")
            if envelope.request_id != base.policy_input.request_id:
                raise ValueError("LifeSwitch request differs from response plan")
            if (
                envelope.conversation_snapshot_sha256
                != base.conversation_snapshot_sha256
            ):
                raise ValueError("LifeSwitch snapshot differs from response plan")
        augmentation = LifeSwitchPromptAugmentationRequestV2.create(
            trusted_thread_id=base.thread_id,
            conversation_snapshot_sha256=base.conversation_snapshot_sha256,
            base_assembly=base.assembled_prompt,
            lifeswitch_source_status=lifeswitch_source_status_v1(
                lifeswitch_context.status
            ),
            lifeswitch_database_accessed=lifeswitch_context.database_accessed,
            lifeswitch_envelope=envelope,
            lifeswitch_rendered=lifeswitch_context.rendered,
            prior_lifeswitch_provenance=prior_lifeswitch_context.envelope,
        )
        assembled = assemble_prompt_with_lifeswitch_v2(augmentation)
        values = {
            "contract_version": TRUSTED_LIFESWITCH_RESPONSE_PLAN_V2_VERSION,
            "base_response_plan": base,
            "lifeswitch_context": lifeswitch_context,
            "prior_lifeswitch_provenance": prior_lifeswitch_context.envelope,
            "prior_lifeswitch_status": prior_lifeswitch_context.status,
            "prior_lifeswitch_database_accessed": prior_lifeswitch_context.database_accessed,
            "assembled_prompt": assembled,
            "base_response_plan_sha256": base.plan_sha256,
            "lifeswitch_context_manifest_sha256": (
                lifeswitch_context.manifest_sha256
            ),
            "assembled_prompt_sha256": assembled.manifest.assembly_sha256,
        }
        return cls(**values, plan_sha256=_sha256(values))


__all__ = ["TrustedLifeSwitchResponsePlanV2"]
