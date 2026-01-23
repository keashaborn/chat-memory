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

CATALOG_SCHEMA = os.getenv("CATALOG_SCHEMA", "catalog_dev")
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

# ----------------------------
# My Foods (private, user-owned)
# ----------------------------

@router.get("/my_foods")
async def list_my_foods(
    owner_user_id: str = Query(..., min_length=1),
    q: str | None = Query(None, min_length=1, max_length=120),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await asyncpg.connect(DSN)
    try:
        where = "owner_user_id = $1::uuid"
        args: list[object] = [owner]

        if include_inactive == 0:
            where += " and is_active"

        if q:
            where += " and (display_name ilike $2 or coalesce(brand,'') ilike $2 or coalesce(variant,'') ilike $2)"
            args.append(f"%{q}%")

        rows = await conn.fetch(
            f"""
            select my_food_id, owner_user_id, display_name, brand, variant,
                   source_type, source_food_id, source, source_id, barcode,
                   basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
                   is_verified, is_active, created_at, updated_at
            from {SCHEMA}.my_food
            where {where}
            order by lower(display_name), lower(coalesce(brand,'')), lower(coalesce(variant,''))
            """,
            *args,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/my_foods/create_from_catalog")
async def create_my_food_from_catalog(
    owner_user_id: str = Query(..., min_length=1),
    food_id: str = Query(..., min_length=1),
    variant: str | None = Query(None, max_length=128),
    display_name: str | None = Query(None, max_length=200),
    brand: str | None = Query(None, max_length=200),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    fid = _as_uuid(food_id, "food_id")

    conn = await asyncpg.connect(DSN)
    try:
        src = await conn.fetchrow(
            f"""
            select food_id, display_name, brand, barcode, source, source_id, basis,
                   kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
            from {CATALOG_SCHEMA}.food
            where food_id = $1::uuid and is_public and is_active
            limit 1
            """,
            fid,
        )
        if not src:
            raise HTTPException(status_code=404, detail="catalog food not found or not public")

        dn = (display_name or (src.get("display_name") if hasattr(src, "get") else src["display_name"]) or "").strip()
        if not dn:
            dn = "Unnamed food"

        br = (brand or (src.get("brand") if hasattr(src, "get") else src["brand"]) or None)
        bc = ((src.get("barcode") if hasattr(src, "get") else src["barcode"]) or None)

        src_id = (src.get("source_id") if hasattr(src, "get") else src["source_id"])
        src_id_txt = str(src_id) if src_id is not None else None

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food(
              owner_user_id, display_name, brand, variant,
              source_type, source_food_id, source, source_id, barcode,
              basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
              is_verified, is_active
            )
            values(
              $1::uuid, $2::text, $3::text, $4::text,
              'catalog', $5::uuid, $6::text, $7::text, $8::text,
              $9::text, $10::numeric, $11::numeric, $12::numeric, $13::numeric, $14::numeric, $15::numeric, $16::numeric,
              true, true
            )
            returning *
            """,
            owner,
            dn,
            br,
            (variant or None),
            fid,
            (src.get("source") if hasattr(src, "get") else src["source"]),
            src_id_txt,
            bc,
            (src.get("basis") if hasattr(src, "get") else src["basis"]),
            (src.get("kcal") if hasattr(src, "get") else src["kcal"]),
            (src.get("protein_g") if hasattr(src, "get") else src["protein_g"]),
            (src.get("carbs_g") if hasattr(src, "get") else src["carbs_g"]),
            (src.get("fat_g") if hasattr(src, "get") else src["fat_g"]),
            (src.get("fiber_g") if hasattr(src, "get") else src["fiber_g"]),
            (src.get("sugar_g") if hasattr(src, "get") else src["sugar_g"]),
            (src.get("sodium_mg") if hasattr(src, "get") else src["sodium_mg"]),
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()

