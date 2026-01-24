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


# ----------------------------
# Meals (templates)
# ----------------------------

@router.get("/meals")
async def list_meals(
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        where = "owner_user_id=$1::uuid"
        if include_inactive == 0:
            where += " and is_active"
        rows = await conn.fetch(
            f"""
            select meal_id, owner_user_id, name, meal_type, is_active, created_at, updated_at
            from {SCHEMA}.meal
            where {where}
            order by lower(meal_type), lower(name)
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/meals/create")
async def create_meal(
    owner_user_id: str = Query(..., min_length=1),
    name: str = Query(..., min_length=1, max_length=120),
    meal_type: str = Query("other"),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    if meal_type not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="meal_type must be breakfast|lunch|dinner|snack|other")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.meal (owner_user_id, name, meal_type)
            values ($1::uuid, $2, $3)
            on conflict (owner_user_id, name) do update
              set meal_type=excluded.meal_type,
                  is_active=true,
                  updated_at=now()
            returning meal_id, owner_user_id, name, meal_type, is_active, created_at, updated_at
            """,
            owner, name.strip(), meal_type,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()


# ----------------------------
# Meal items (support grams OR servings preset)
# ----------------------------

@router.post("/meals/{meal_id}/items/add")
async def add_meal_item(
    meal_id: str,
    my_food_id: str = Query(..., min_length=1),
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
    sort_order: int = Query(0),
    notes: str | None = Query(None, max_length=500),
):
    """
    Exactly one quantity mode:
      - grams mode: qty_g
      - serving mode: my_food_serving_id + qty_servings
    """
    mid = _as_uuid(meal_id, "meal_id")
    fid = _as_uuid(my_food_id, "my_food_id")

    use_grams = qty_g is not None
    use_serv = (my_food_serving_id is not None) or (qty_servings is not None)

    if use_grams and use_serv:
        raise HTTPException(status_code=400, detail="provide qty_g OR (my_food_serving_id + qty_servings), not both")
    if not use_grams and not use_serv:
        raise HTTPException(status_code=400, detail="must provide qty_g OR (my_food_serving_id + qty_servings)")
    if use_serv and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="servings mode requires my_food_serving_id and qty_servings")

    sid = _as_uuid(my_food_serving_id, "my_food_serving_id") if my_food_serving_id else None

    conn = await _db()
    try:
        ok = await conn.fetchval(
            f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if ok is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")

        if sid:
            owns = await conn.fetchval(
                f"select 1 from {SCHEMA}.my_food_serving where my_food_serving_id=$1::uuid and my_food_id=$2::uuid",
                sid, fid,
            )
            if owns != 1:
                raise HTTPException(status_code=404, detail="serving not found for this my_food_id")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.meal_item
              (meal_id, my_food_id, qty_g, my_food_serving_id, qty_servings, sort_order, notes)
            values
              ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7)
            returning meal_item_id, meal_id, my_food_id,
                      qty_g, my_food_serving_id, qty_servings,
                      sort_order, notes, created_at, updated_at
            """,
            mid, fid, qty_g, sid, qty_servings, sort_order, notes,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()


@router.get("/meals/{meal_id}/items")
async def list_meal_items(meal_id: str):
    mid = _as_uuid(meal_id, "meal_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              i.meal_item_id, i.meal_id, i.my_food_id,
              i.qty_g, i.my_food_serving_id, i.qty_servings,
              coalesce(i.qty_g, (s.grams * i.qty_servings)) as qty_g_resolved,
              s.name as serving_name, s.grams as serving_grams,
              i.sort_order, i.notes,

              f.display_name, f.brand, f.variant,
              f.kcal, f.protein_g, f.carbs_g, f.fat_g,

              i.created_at, i.updated_at
            from {SCHEMA}.meal_item i
            join {SCHEMA}.my_food f on f.my_food_id = i.my_food_id
            left join {SCHEMA}.my_food_serving s on s.my_food_serving_id = i.my_food_serving_id
            where i.meal_id = $1::uuid
            order by i.sort_order, i.created_at
            """,
            mid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.post("/meals/{meal_id}/items/{meal_item_id}/delete")
async def delete_meal_item(
    meal_id: str,
    meal_item_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    mid = _as_uuid(meal_id, "meal_id")
    iid = _as_uuid(meal_item_id, "meal_item_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            delete from {SCHEMA}.meal_item i
            using {SCHEMA}.meal m
            where i.meal_item_id = $1::uuid
              and i.meal_id = $2::uuid
              and m.meal_id = i.meal_id
              and m.owner_user_id = $3::uuid
            returning
              i.meal_item_id, i.meal_id, i.my_food_id,
              i.qty_g, i.my_food_serving_id, i.qty_servings,
              i.sort_order, i.notes, i.created_at, i.updated_at
            """,
            iid, mid, owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="meal_item not found (or not owned by user)")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
