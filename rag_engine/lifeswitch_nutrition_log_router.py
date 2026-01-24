from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
import asyncpg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

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


async def _db():
    return await asyncpg.connect(DSN)


def _parse_day(day: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(day))
    except Exception:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")


@router.post("/log/entry")
async def create_log_entry(
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    meal_id: str | None = Query(None),
    my_food_id: str | None = Query(None),
    qty_g: float | None = Query(None, gt=0),
    sort_order: int = Query(0),
    notes: str | None = Query(None, max_length=500),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    d = _parse_day(day)

    # exactly one of meal_id / my_food_id
    if (meal_id is None) == (my_food_id is None):
        raise HTTPException(status_code=400, detail="provide exactly one: meal_id or my_food_id")

    mid = _as_uuid(meal_id, "meal_id") if meal_id is not None else None
    fid = _as_uuid(my_food_id, "my_food_id") if my_food_id is not None else None

    conn = await _db()
    try:
        if mid is not None:
            ok = await conn.fetchval(f"select is_active from {SCHEMA}.meal where meal_id=$1::uuid", mid)
            if ok is not True:
                raise HTTPException(status_code=404, detail="meal not found or inactive")

        if fid is not None:
            ok = await conn.fetchval(f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid", fid)
            if ok is not True:
                raise HTTPException(status_code=404, detail="my_food not found or inactive")

        day_row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.nutrition_day (owner_user_id, day)
            values ($1::uuid, $2::date)
            on conflict (owner_user_id, day) do update
              set updated_at=now()
            returning nutrition_day_id, owner_user_id, day, notes, created_at, updated_at
            """,
            owner,
            d,
        )
        if not day_row:
            raise HTTPException(status_code=500, detail="failed to create nutrition_day")

        ndid = str(day_row["nutrition_day_id"])

        entry_row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.nutrition_entry
              (nutrition_day_id, meal_id, my_food_id, qty_g, sort_order, notes)
            values
              ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6)
            returning nutrition_entry_id, nutrition_day_id, meal_id, my_food_id, qty_g, sort_order, notes, created_at, updated_at
            """,
            ndid,
            mid,
            fid,
            qty_g,
            sort_order,
            notes,
        )

        return JSONResponse({"day": _row_to_jsonable(day_row), "entry": _row_to_jsonable(entry_row) if entry_row else None})
    finally:
        await conn.close()


@router.get("/log/day")
async def get_log_day(
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    d = _parse_day(day)

    conn = await _db()
    try:
        day_row = await conn.fetchrow(
            f"""
            select nutrition_day_id, owner_user_id, day, notes, created_at, updated_at
            from {SCHEMA}.nutrition_day
            where owner_user_id=$1::uuid and day=$2::date
            """,
            owner,
            d,
        )
        if not day_row:
            return JSONResponse({"day": None, "entries": []})

        ndid = str(day_row["nutrition_day_id"])

        rows = await conn.fetch(
            f"""
            select
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.sort_order, e.notes,
              e.created_at, e.updated_at,

              coalesce(m.name, f.display_name) as label,
              m.meal_type as meal_type,

              f.kcal as food_kcal_100g, f.protein_g as food_protein_100g, f.carbs_g as food_carbs_100g, f.fat_g as food_fat_100g,

              mt.kcal as meal_kcal, mt.protein_g as meal_protein, mt.carbs_g as meal_carbs, mt.fat_g as meal_fat

            from {SCHEMA}.nutrition_entry e
            left join {SCHEMA}.meal m on m.meal_id = e.meal_id
            left join {SCHEMA}.my_food f on f.my_food_id = e.my_food_id

            left join lateral (
              select
                sum((mf.kcal * mi.qty_g)/100.0) as kcal,
                sum((mf.protein_g * mi.qty_g)/100.0) as protein_g,
                sum((mf.carbs_g * mi.qty_g)/100.0) as carbs_g,
                sum((mf.fat_g * mi.qty_g)/100.0) as fat_g
              from {SCHEMA}.meal_item mi
              join {SCHEMA}.my_food mf on mf.my_food_id = mi.my_food_id
              where mi.meal_id = e.meal_id
            ) mt on true

            where e.nutrition_day_id = $1::uuid
            order by e.sort_order, e.created_at
            """,
            ndid,
        )

        return JSONResponse({"day": _row_to_jsonable(day_row), "entries": [_row_to_jsonable(r) for r in rows]})
    finally:
        await conn.close()
