from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rag_engine.lifeswitch_db import DSN


router = APIRouter()
ACCOUNT_WRITER_ROLE = "lifeswitch_chat_account_writer_v1"


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


async def _set_owner(conn: asyncpg.Connection, actor: UUID) -> None:
    await conn.execute("select set_config('app.user_id',$1,true)", str(actor))
    await conn.execute(
        "select set_config('app.lifeswitch_owner_id',$1,true)",
        str(actor),
    )
    await conn.execute(f"set local role {ACCOUNT_WRITER_ROLE}")


def _response(row: asyncpg.Record | None, *, changed: bool = False) -> AccountTimezoneV1:
    if row is None:
        return AccountTimezoneV1(
            timezone_name=None,
            source=None,
            revision=0,
            updated_at=None,
            changed=False,
        )
    return AccountTimezoneV1(
        timezone_name=str(row["timezone_name"]),
        source=str(row["timezone_source"]),
        revision=int(row["revision"]),
        updated_at=row["updated_at"],
        changed=bool(row["changed"] if "changed" in row.keys() else changed),
    )


@router.get("/timezone", response_model=AccountTimezoneV1)
async def get_account_timezone(req: Request) -> AccountTimezoneV1:
    actor = _actor(req)
    if not DSN:
        raise HTTPException(status_code=503, detail="timezone_service_unavailable")
    conn = await asyncpg.connect(DSN)
    try:
        async with conn.transaction(readonly=True):
            await _set_owner(conn, actor)
            row = await conn.fetchrow(
                "select * from lifeswitch_chat.read_account_timezone_setting_v1($1)",
                actor,
            )
            return _response(row)
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == "42501":
            raise HTTPException(status_code=403, detail="owner_scope_denied") from exc
        raise HTTPException(status_code=503, detail="timezone_service_unavailable") from exc
    finally:
        await conn.close()


@router.put("/timezone", response_model=AccountTimezoneV1)
async def put_account_timezone(
    req: Request,
    payload: TimezoneUpdateV1,
) -> AccountTimezoneV1:
    actor = _actor(req)
    timezone_name = _timezone(payload.timezone_name)
    request_hash = _request_hash(req)
    if not DSN:
        raise HTTPException(status_code=503, detail="timezone_service_unavailable")
    conn = await asyncpg.connect(DSN)
    try:
        async with conn.transaction():
            await _set_owner(conn, actor)
            row = await conn.fetchrow(
                """
                select * from lifeswitch_chat.write_account_timezone_setting_v1(
                  $1,$2,$3,$4
                )
                """,
                actor,
                timezone_name,
                payload.expected_revision,
                request_hash,
            )
            return _response(row)
    except asyncpg.PostgresError as exc:
        if exc.sqlstate in {"40001", "23505"}:
            raise HTTPException(status_code=409, detail="timezone_revision_conflict") from exc
        if exc.sqlstate == "22023":
            raise HTTPException(status_code=400, detail="invalid_timezone") from exc
        if exc.sqlstate == "42501":
            raise HTTPException(status_code=403, detail="owner_scope_denied") from exc
        raise HTTPException(status_code=503, detail="timezone_service_unavailable") from exc
    finally:
        await conn.close()


__all__ = [
    "AccountTimezoneV1",
    "TimezoneUpdateV1",
    "get_account_timezone",
    "put_account_timezone",
    "router",
]
