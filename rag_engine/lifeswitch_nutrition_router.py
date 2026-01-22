from __future__ import annotations

import os
import uuid
import asyncpg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import decimal
import datetime as _dt

router = APIRouter()

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

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

SCHEMA = os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")

def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")

async def _db():
    return await asyncpg.connect(DSN)

@router.get("/meal_plans")
async def list_meal_plans(owner_user_id: str = Query(...)):
    uid = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select meal_plan_id, owner_user_id, name, goal,
                   target_kcal, target_protein_g, target_carbs_g, target_fat_g,
                   is_active, created_at, updated_at
            from {SCHEMA}.meal_plan
            where owner_user_id = $1::uuid
            order by updated_at desc
            """,
            uid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.post("/meal_plans/create")
async def create_meal_plan(
    owner_user_id: str = Query(...),
    name: str = Query(..., min_length=1, max_length=80),
    goal: str = Query("maintain"),
    target_kcal: float | None = Query(None),
    target_protein_g: float | None = Query(None),
    target_carbs_g: float | None = Query(None),
    target_fat_g: float | None = Query(None),
):
    uid = _as_uuid(owner_user_id, "owner_user_id")
    if goal not in ("cut", "bulk", "maintain"):
        raise HTTPException(status_code=400, detail="goal must be cut|bulk|maintain")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.meal_plan
              (owner_user_id, name, goal, target_kcal, target_protein_g, target_carbs_g, target_fat_g)
            values
              ($1::uuid, $2, $3, $4, $5, $6, $7)
            on conflict (owner_user_id, name) do update
              set goal=excluded.goal,
                  target_kcal=excluded.target_kcal,
                  target_protein_g=excluded.target_protein_g,
                  target_carbs_g=excluded.target_carbs_g,
                  target_fat_g=excluded.target_fat_g,
                  updated_at=now(),
                  is_active=true
            returning meal_plan_id, owner_user_id, name, goal,
                      target_kcal, target_protein_g, target_carbs_g, target_fat_g,
                      is_active, created_at, updated_at
            """,
            uid, name.strip(), goal,
            target_kcal, target_protein_g, target_carbs_g, target_fat_g
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()

@router.post("/meal_plans/{meal_plan_id}/items/add")
async def add_item(
    meal_plan_id: str,
    food_id: str = Query(...),
    meal_label: str = Query("other"),
    sort_order: int = Query(0),
    qty_g: float | None = Query(None),
    qty_servings: float | None = Query(None),
    notes: str | None = Query(None),
):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    fid = _as_uuid(food_id, "food_id")

    if meal_label not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="meal_label must be breakfast|lunch|dinner|snack|other")

    conn = await _db()
    try:
        # ensure food is approved/public
        ok = await conn.fetchval(
            "select is_public from catalog_dev.food where food_id=$1::uuid",
            fid
        )
        if ok is not True:
            raise HTTPException(status_code=400, detail="food_id is not approved/public")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.meal_plan_item
              (meal_plan_id, meal_label, sort_order, food_id, qty_g, qty_servings, notes)
            values
              ($1::uuid, $2, $3, $4::uuid, $5, $6, $7)
            returning meal_plan_item_id, meal_plan_id, meal_label, sort_order, food_id, qty_g, qty_servings, notes,
                      created_at, updated_at
            """,
            mpid, meal_label, sort_order, fid, qty_g, qty_servings, notes
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()

@router.get("/meal_plans/{meal_plan_id}/items")
async def list_items(meal_plan_id: str):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select i.meal_plan_item_id, i.meal_plan_id, i.meal_label, i.sort_order,
                   i.food_id, i.qty_g, i.qty_servings, i.notes,
                   f.display_name, f.brand, f.kcal, f.protein_g, f.carbs_g, f.fat_g,
                   i.created_at, i.updated_at
            from {SCHEMA}.meal_plan_item i
            join catalog_dev.food f on f.food_id = i.food_id
            where i.meal_plan_id = $1::uuid
            order by i.meal_label, i.sort_order, i.created_at
            """,
            mpid
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()
