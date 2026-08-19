from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from fastapi.responses import JSONResponse

router = APIRouter()

SCHEMA = os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _json_safe(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (decimal.Decimal,)):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _row_to_jsonable(r):
    d = dict(r)
    return {k: _json_safe(v) for k, v in d.items()}


async def _db(req: Request):
    return await connect_lifeswitch(req)


@router.get("/my_food_overrides")
async def list_my_food_overrides(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db(req)
    try:
        rows = await conn.fetch(
            f"""
            select owner_user_id, my_food_id, alias, default_grams, sort_order, created_at, updated_at
            from {SCHEMA}.my_food_override
            where owner_user_id=$1::uuid
            order by sort_order asc, updated_at desc
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/my_food_overrides/upsert")
async def upsert_my_food_override(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    my_food_id: str = Query(..., min_length=1),
    alias: str | None = Query(None, max_length=200),
    default_grams: float | None = Query(None, gt=0),
    sort_order: int = Query(0),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    fid = _as_uuid(my_food_id, "my_food_id")

    a = (alias or "").strip()
    if a == "":
        a = None

    conn = await _db(req)
    try:
        ok = await conn.fetchval(
            f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if ok is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food_override
              (owner_user_id, my_food_id, alias, default_grams, sort_order)
            values
              ($1::uuid, $2::uuid, $3, $4, $5)
            on conflict (owner_user_id, my_food_id) do update
              set alias=excluded.alias,
                  default_grams=excluded.default_grams,
                  sort_order=excluded.sort_order,
                  updated_at=now()
            returning owner_user_id, my_food_id, alias, default_grams, sort_order, created_at, updated_at
            """,
            owner,
            fid,
            a,
            default_grams,
            sort_order,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()
