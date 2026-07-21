from __future__ import annotations

"""Default-off, zero-response-influence runtime wrapper for Phase 2 shadowing.

The wrapper performs one safety moderation call and local deterministic policy,
FM selection, and prompt assembly.  It never calls a generation model, selects
Memory, changes the live response, writes state, or emits private prompt text.
The live router does not import this module yet.
"""

import asyncio
import hashlib
import json
import os
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from rag_engine.openai_moderation_adapter_v0_2 import (
    OpenAIModerationAdapterV0_2,
)
from rag_engine.response_conversation_snapshot_v1 import (
    ConversationSnapshotV1,
)
from rag_engine.response_orchestration_v0_2 import (
    SanitizedResponseShadowTraceV0_2,
    TrustedResponseOrchestratorV0_2,
    TrustedResponseRequestV0_2,
)


SHADOW_RUNTIME_VERSION = "resse_response_shadow_runtime_v0_2"
SHADOW_FLAG = "RESSE_RESPONSE_SHADOW_V0_2"
SHADOW_ALLOWLIST = "RESSE_RESPONSE_SHADOW_USER_IDS"
SHADOW_MODERATION_WALL_TIMEOUT_SECONDS = 12.0


class ShadowRuntimeStatus(str, Enum):
    DISABLED = "disabled"
    ACTOR_NOT_ALLOWLISTED = "actor_not_allowlisted"
    EVALUATED = "evaluated"
    ERROR = "error"


class ShadowRuntimeErrorCode(str, Enum):
    INVALID_ACTOR = "invalid_actor"
    INVALID_ALLOWLIST = "invalid_allowlist"
    INVALID_TRUSTED_INPUT = "invalid_trusted_input"
    ORCHESTRATION_FAILED = "orchestration_failed"


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
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class ResponseShadowRuntimeResultV0_2(_StrictFrozenModel):
    contract_version: Literal[SHADOW_RUNTIME_VERSION] = SHADOW_RUNTIME_VERSION
    status: ShadowRuntimeStatus
    error_code: ShadowRuntimeErrorCode | None = None
    trace: SanitizedResponseShadowTraceV0_2 | None = None
    conversation_snapshot_outcome: str | None = None
    response_influence: Literal[False] = False
    generation_model_calls: Literal[0] = 0
    governed_memory_selection_calls: Literal[0] = 0
    database_writes: Literal[0] = 0
    qdrant_writes: Literal[0] = 0
    result_sha256: str

    @field_validator("result_sha256")
    @classmethod
    def hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("result_sha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def exact_result(self) -> "ResponseShadowRuntimeResultV0_2":
        if self.status is ShadowRuntimeStatus.EVALUATED:
            if (
                self.trace is None
                or self.error_code is not None
                or self.conversation_snapshot_outcome is None
            ):
                raise ValueError(
                    "evaluated shadow requires a trace and conversation snapshot binding"
                )
        elif self.status is ShadowRuntimeStatus.ERROR:
            if self.error_code is None or self.trace is not None:
                raise ValueError("shadow error requires only an error code")
        elif self.trace is not None or self.error_code is not None:
            raise ValueError("skipped shadow results cannot contain trace or error")
        if self.status is not ShadowRuntimeStatus.EVALUATED and (
            self.conversation_snapshot_outcome is not None
        ):
            raise ValueError("skipped shadow results cannot bind a snapshot")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != _sha256(payload):
            raise ValueError("shadow runtime result hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        status: ShadowRuntimeStatus,
        error_code: ShadowRuntimeErrorCode | None = None,
        trace: SanitizedResponseShadowTraceV0_2 | None = None,
        conversation_snapshot_outcome: str | None = None,
    ) -> "ResponseShadowRuntimeResultV0_2":
        payload = {
            "contract_version": SHADOW_RUNTIME_VERSION,
            "status": status.value,
            "error_code": error_code.value if error_code is not None else None,
            "trace": trace.model_dump(mode="json") if trace is not None else None,
            "conversation_snapshot_outcome": conversation_snapshot_outcome,
            "response_influence": False,
            "generation_model_calls": 0,
            "governed_memory_selection_calls": 0,
            "database_writes": 0,
            "qdrant_writes": 0,
        }
        return cls.model_validate_json(
            _canonical_json_bytes(
                {**payload, "result_sha256": _sha256(payload)}
            )
        )


def _enabled(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _canonical_actor(value: str | UUID) -> UUID:
    try:
        actor = UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid actor") from None
    if str(actor) != str(value):
        raise ValueError("actor must be a canonical UUID")
    return actor


def _allowlist(value: Any) -> frozenset[UUID]:
    raw = str(value or "").strip()
    if not raw:
        return frozenset()
    values: set[UUID] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            raise ValueError("allowlist contains a blank value")
        actor = _canonical_actor(item)
        if actor in values:
            raise ValueError("allowlist contains a duplicate actor")
        values.add(actor)
    return frozenset(values)


class _AsyncShadowModerationProvider:
    def __init__(self, openai_client: Any) -> None:
        self._adapter = OpenAIModerationAdapterV0_2(openai_client)

    async def assess(self, request: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(self._adapter.assess, request),
            timeout=SHADOW_MODERATION_WALL_TIMEOUT_SECONDS,
        )


async def evaluate_response_shadow_v0_2(
    *,
    authenticated_actor_user_id: str | UUID,
    conversation_snapshot: ConversationSnapshotV1 | None,
    request_field_names: tuple[str, ...],
    openai_client: Any,
    environ: Mapping[str, str] | None = None,
) -> ResponseShadowRuntimeResultV0_2:
    """Evaluate a trusted snapshot without affecting the live answer."""

    env = os.environ if environ is None else environ
    if not _enabled(env.get(SHADOW_FLAG, "0")):
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.DISABLED
        )
    try:
        actor = _canonical_actor(authenticated_actor_user_id)
    except ValueError:
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.ERROR,
            error_code=ShadowRuntimeErrorCode.INVALID_ACTOR,
        )
    try:
        allowlist = _allowlist(env.get(SHADOW_ALLOWLIST, ""))
    except ValueError:
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.ERROR,
            error_code=ShadowRuntimeErrorCode.INVALID_ALLOWLIST,
        )
    if actor not in allowlist:
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.ACTOR_NOT_ALLOWLISTED
        )
    try:
        if not isinstance(conversation_snapshot, ConversationSnapshotV1):
            raise ValueError("conversation snapshot is required")
        snapshot = ConversationSnapshotV1.model_validate_json(
            conversation_snapshot.model_dump_json()
        )
        if snapshot.authenticated_actor_user_id != actor:
            raise ValueError("conversation snapshot actor mismatch")
        trusted_request = TrustedResponseRequestV0_2.create_from_snapshot(
            authenticated_actor_user_id=actor,
            conversation_snapshot=snapshot,
            request_field_names=request_field_names,
        )
    except Exception:
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.ERROR,
            error_code=ShadowRuntimeErrorCode.INVALID_TRUSTED_INPUT,
        )
    try:
        plan = await TrustedResponseOrchestratorV0_2(
            _AsyncShadowModerationProvider(openai_client)
        ).build_plan(trusted_request)
    except Exception:
        return ResponseShadowRuntimeResultV0_2.create(
            status=ShadowRuntimeStatus.ERROR,
            error_code=ShadowRuntimeErrorCode.ORCHESTRATION_FAILED,
        )
    return ResponseShadowRuntimeResultV0_2.create(
        status=ShadowRuntimeStatus.EVALUATED,
        trace=plan.shadow_trace,
        conversation_snapshot_outcome=snapshot.outcome.value,
    )


def run_response_shadow_v0_2(**kwargs: Any) -> ResponseShadowRuntimeResultV0_2:
    """Synchronous compatibility entry point for the current sync route."""

    return asyncio.run(evaluate_response_shadow_v0_2(**kwargs))


__all__ = [
    "ResponseShadowRuntimeResultV0_2",
    "SHADOW_ALLOWLIST",
    "SHADOW_FLAG",
    "SHADOW_RUNTIME_VERSION",
    "SHADOW_MODERATION_WALL_TIMEOUT_SECONDS",
    "ShadowRuntimeErrorCode",
    "ShadowRuntimeStatus",
    "evaluate_response_shadow_v0_2",
    "run_response_shadow_v0_2",
]
