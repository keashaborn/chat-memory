from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from seebx.adapters.lifeswitch_timezone_postgres import (
    AccountTimezoneRecord,
    AccountTimezoneRepositoryConflict,
    AccountTimezoneRepositoryInvalid,
    AccountTimezoneRepositoryOwnerDenied,
    AccountTimezoneRepositoryUnavailable,
    account_timezone_repository,
)


router = APIRouter()


class TimezoneUpdateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timezone_name: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)


class AccountTimezoneV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract_version: Literal["lifeswitch_account_timezone_v1"] = (
        "lifeswitch_account_timezone_v1"
    )
    timezone_name: str | None
    source: Literal["account_setting", "reviewed_migration"] | None
    revision: int = Field(ge=0)
    updated_at: datetime | None
    changed: bool = False


def _actor(req: Request) -> UUID:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    try:
        actor = UUID(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="authenticated_actor_required") from exc
    if actor.int == 0:
        raise HTTPException(status_code=401, detail="authenticated_actor_required")
    return actor


def _timezone(value: str) -> str:
    clean = str(value or "").strip()
    if clean != value or len(clean) > 80:
        raise HTTPException(status_code=400, detail="invalid_timezone")
    try:
        ZoneInfo(clean)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid_timezone") from exc
    return clean


def _request_hash(req: Request) -> str:
    request_id = str(getattr(req.state, "request_id", "") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id_required")
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _response(row: AccountTimezoneRecord | None) -> AccountTimezoneV1:
    if row is None:
        return AccountTimezoneV1(
            timezone_name=None,
            source=None,
            revision=0,
            updated_at=None,
            changed=False,
        )
    return AccountTimezoneV1(
        timezone_name=row.timezone_name,
        source=row.timezone_source,
        revision=row.revision,
        updated_at=row.updated_at,
        changed=row.changed,
    )


@router.get("/timezone", response_model=AccountTimezoneV1)
async def get_account_timezone(req: Request) -> AccountTimezoneV1:
    actor = _actor(req)
    try:
        async with account_timezone_repository(req) as repository:
            row = await repository.get(actor)
            return _response(row)
    except AccountTimezoneRepositoryOwnerDenied as exc:
        raise HTTPException(status_code=403, detail="owner_scope_denied") from exc
    except AccountTimezoneRepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="timezone_service_unavailable") from exc


@router.put("/timezone", response_model=AccountTimezoneV1)
async def put_account_timezone(
    req: Request,
    payload: TimezoneUpdateV1,
) -> AccountTimezoneV1:
    actor = _actor(req)
    timezone_name = _timezone(payload.timezone_name)
    request_hash = _request_hash(req)
    try:
        async with account_timezone_repository(req) as repository:
            row = await repository.put(
                actor,
                timezone_name,
                payload.expected_revision,
                request_hash,
            )
            return _response(row)
    except AccountTimezoneRepositoryConflict as exc:
        raise HTTPException(status_code=409, detail="timezone_revision_conflict") from exc
    except AccountTimezoneRepositoryInvalid as exc:
        raise HTTPException(status_code=400, detail="invalid_timezone") from exc
    except AccountTimezoneRepositoryOwnerDenied as exc:
        raise HTTPException(status_code=403, detail="owner_scope_denied") from exc
    except AccountTimezoneRepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="timezone_service_unavailable") from exc


__all__ = [
    "AccountTimezoneV1",
    "TimezoneUpdateV1",
    "get_account_timezone",
    "put_account_timezone",
    "router",
]
