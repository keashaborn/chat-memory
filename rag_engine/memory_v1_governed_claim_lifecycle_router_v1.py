from __future__ import annotations

"""Owner-authenticated inspect and semantic-delete API for governed claims."""

import json
import os
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from rag_engine.memory_actor_auth_v1 import require_memory_actor_v1


router = APIRouter()
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def _canonical_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is not str:
        raise ValueError("UUID must be a canonical JSON string")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("UUID must be canonical lowercase hyphenated text") from exc
    if str(parsed) != value:
        raise ValueError("UUID must be canonical lowercase hyphenated text")
    return parsed


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]


class GovernedClaimRetractionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: CanonicalUUID
    expected_revision_number: int = Field(ge=1)
    expected_claim_state_sha256: str
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("expected_claim_state_sha256")
    @classmethod
    def exact_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("expected claim-state hash is invalid")
        return value

    @field_validator("reason")
    @classmethod
    def normalized_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("reason is empty")
        return reason


class GovernedClaimCorrectionV1(GovernedClaimRetractionV1):
    replacement_text: str = Field(min_length=1, max_length=32_768)

    @field_validator("replacement_text")
    @classmethod
    def bounded_replacement_text(cls, value: str) -> str:
        replacement = value.strip()
        if not replacement:
            raise ValueError("replacement text is empty")
        if len(replacement.encode("utf-8")) > 32_768:
            raise ValueError("replacement text exceeds the UTF-8 byte limit")
        return replacement


async def _bind_owner(conn: Any, owner_user_id: UUID) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner_user_id),
    )
    bound = await conn.fetchval("SELECT memory.current_actor_user_id()")
    if str(bound) != str(owner_user_id):
        raise HTTPException(status_code=403, detail="memory_owner_binding_failed")


def _operation_ids(owner_user_id: UUID, claim_id: UUID, operation_id: UUID) -> tuple[UUID, UUID]:
    base = f"memory-governed-claim-lifecycle-v1|{owner_user_id}|{claim_id}|{operation_id}"
    return (
        uuid5(NAMESPACE_URL, base + "|review"),
        uuid5(NAMESPACE_URL, base + "|apply"),
    )


def _correction_operation_ids(
    owner_user_id: UUID,
    claim_id: UUID,
    operation_id: UUID,
) -> tuple[UUID, UUID]:
    base = f"memory-governed-claim-correction-v1|{owner_user_id}|{claim_id}|{operation_id}"
    return (
        uuid5(NAMESPACE_URL, base + "|review"),
        uuid5(NAMESPACE_URL, base + "|apply"),
    )


@router.get("/{owner_user_id}")
async def list_owner_governed_claims_v1(
    owner_user_id: CanonicalUUID,
    req: Request,
    limit: int = 50,
) -> JSONResponse:
    if not DSN:
        raise HTTPException(status_code=503, detail="governed_claim_lifecycle_unconfigured")
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="invalid_limit")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))
    conn = await asyncpg.connect(DSN, command_timeout=10)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await _bind_owner(conn, owner)
            rows = await conn.fetch(
                "SELECT * FROM memory.read_owner_governed_claim_lifecycle_v1($1)",
                limit,
            )
        return JSONResponse(
            jsonable_encoder({
                "contract_version": "memory_v1_governed_claim_lifecycle_response_v1",
                "owner_user_id": str(owner),
                "count": len(rows),
                "claims": [dict(row) for row in rows],
            }),
            headers=NO_STORE_HEADERS,
        )
    finally:
        await conn.close()


async def _retract(
    owner_user_id: CanonicalUUID,
    claim_id: CanonicalUUID,
    value: GovernedClaimRetractionV1,
    req: Request,
) -> JSONResponse:
    if not DSN:
        raise HTTPException(status_code=503, detail="governed_claim_lifecycle_unconfigured")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))
    review_request_id, apply_request_id = _operation_ids(
        owner,
        claim_id,
        value.operation_id,
    )
    conn = await asyncpg.connect(DSN, command_timeout=15)
    try:
        try:
            async with conn.transaction(isolation="serializable"):
                await _bind_owner(conn, owner)
                row = await conn.fetchrow(
                    """SELECT * FROM memory.retract_owner_governed_claim_v1(
                         $1,$2,$3,$4,$5,$6
                       )""",
                    review_request_id,
                    apply_request_id,
                    claim_id,
                    value.expected_revision_number,
                    value.expected_claim_state_sha256,
                    value.reason,
                )
        except (asyncpg.PostgresError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="governed_claim_retraction_conflict",
            ) from exc
        if row is None:
            raise HTTPException(status_code=409, detail="governed_claim_retraction_missing")
        payload = dict(row)
        payload.update(
            {
                "contract_version": "memory_v1_governed_claim_retraction_response_v1",
                "owner_user_id": str(owner),
                "claim_id": str(claim_id),
                "history_retained": True,
                "physical_claim_deleted": False,
            }
        )
        return JSONResponse(jsonable_encoder(payload), headers=NO_STORE_HEADERS)
    finally:
        await conn.close()


@router.post("/{owner_user_id}/{claim_id}/retract")
async def retract_owner_governed_claim_v1(
    owner_user_id: CanonicalUUID,
    claim_id: CanonicalUUID,
    value: GovernedClaimRetractionV1,
    req: Request,
) -> JSONResponse:
    return await _retract(owner_user_id, claim_id, value, req)


@router.post("/{owner_user_id}/{claim_id}/correct")
async def correct_owner_governed_claim_v1(
    owner_user_id: CanonicalUUID,
    claim_id: CanonicalUUID,
    value: GovernedClaimCorrectionV1,
    req: Request,
) -> JSONResponse:
    """Retract the old claim and re-enter replacement text as reviewable evidence."""

    if not DSN:
        raise HTTPException(status_code=503, detail="governed_claim_lifecycle_unconfigured")
    owner = UUID(await require_memory_actor_v1(req, str(owner_user_id)))
    review_request_id, apply_request_id = _correction_operation_ids(
        owner,
        claim_id,
        value.operation_id,
    )
    source_system = "frontend/governed-claim-correction:user"
    external_id = f"governed-claim-correction:{claim_id}:{value.operation_id}"
    independence_key = f"governed-claim-correction:{claim_id}"
    metadata = json.dumps(
        {
            "contract_version": "memory_v1_governed_claim_correction_evidence_v1",
            "corrected_claim_id": str(claim_id),
            "correction_operation_id": str(value.operation_id),
            "expected_prior_revision_number": value.expected_revision_number,
            "expected_prior_claim_state_sha256": value.expected_claim_state_sha256,
            "replacement_requires_governed_review": True,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn = await asyncpg.connect(DSN, command_timeout=15)
    try:
        try:
            async with conn.transaction(isolation="serializable"):
                await _bind_owner(conn, owner)
                retraction = await conn.fetchrow(
                    """SELECT * FROM memory.retract_owner_governed_claim_v1(
                         $1,$2,$3,$4,$5,$6
                       )""",
                    review_request_id,
                    apply_request_id,
                    claim_id,
                    value.expected_revision_number,
                    value.expected_claim_state_sha256,
                    value.reason,
                )
                evidence = await conn.fetchrow(
                    """SELECT * FROM memory.record_owner_evidence_v1(
                         'user_statement'::memory.evidence_kind,$1,$2,$3,
                         clock_timestamp(),1,1,$4,
                         'high'::memory.sensitivity_level,$5::jsonb
                       )""",
                    source_system,
                    external_id,
                    value.replacement_text,
                    independence_key,
                    metadata,
                )
        except (asyncpg.PostgresError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="governed_claim_correction_conflict",
            ) from exc
        if retraction is None or evidence is None:
            raise HTTPException(status_code=409, detail="governed_claim_correction_missing")
        payload = dict(retraction)
        payload.update(
            {
                "contract_version": "memory_v1_governed_claim_correction_response_v1",
                "owner_user_id": str(owner),
                "claim_id": str(claim_id),
                "history_retained": True,
                "physical_claim_deleted": False,
                "automatic_claim_promotion": False,
                "replacement_status": "evidence_recorded_pending_governed_review",
                "replacement_evidence": dict(evidence),
            }
        )
        return JSONResponse(jsonable_encoder(payload), headers=NO_STORE_HEADERS)
    finally:
        await conn.close()


@router.delete("/{owner_user_id}/{claim_id}")
async def delete_owner_governed_claim_v1(
    owner_user_id: CanonicalUUID,
    claim_id: CanonicalUUID,
    value: GovernedClaimRetractionV1,
    req: Request,
) -> JSONResponse:
    """Semantic delete: append a retraction; never erase governed history."""

    return await _retract(owner_user_id, claim_id, value, req)


__all__ = ["router"]
