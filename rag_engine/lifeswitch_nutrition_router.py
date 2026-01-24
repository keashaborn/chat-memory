from __future__ import annotations

import os
import asyncio
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


@router.post("/my_foods/create_from_usda")
async def create_my_food_from_usda(
    owner_user_id: str = Query(..., min_length=1),
    fdc_id: int = Query(..., ge=1),
    variant: str | None = Query(None, max_length=120),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")

    api_key = os.getenv("USDA_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="USDA_API_KEY not configured on server")

    import requests

    def _do():
        return requests.get(
            f"https://api.nal.usda.gov/fdc/v1/food/{int(fdc_id)}",
            params={"api_key": api_key},
            timeout=(2, 30),
        )

    r = await asyncio.to_thread(_do)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="fdc_id not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"usda_fdc HTTP {r.status_code}")

    j = r.json() if r.content else {}
    desc = (j or {}).get("description") or f"FDC {fdc_id}"
    brand_owner = (j or {}).get("brandOwner")
    gtin = (j or {}).get("gtinUpc")

    nutr = (j or {}).get("foodNutrients") or []

    def _nutr_amount(nutrient_number: str):
        for n in nutr:
            nn = ((n.get("nutrient") or {}).get("number") or "")
            if str(nn) == str(nutrient_number):
                v = n.get("amount")
                try:
                    return float(v) if v is not None else None
                except Exception:
                    return None
        return None

    kcal = _nutr_amount("208")
    protein = _nutr_amount("203")
    carbs = _nutr_amount("205")
    fat = _nutr_amount("204")
    fiber = _nutr_amount("291")
    sugar = _nutr_amount("269")
    sodium_mg = _nutr_amount("307")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food
              (owner_user_id, display_name, brand, variant,
               source_type, source_food_id, source, source_id, barcode,
               basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               is_verified, is_active)
            values
              ($1::uuid, $2, $3, $4,
               'usda', null, 'usda_fdc', $5, $6,
               'per_100g', $7, $8, $9, $10, $11, $12, $13,
               true, true)
            on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
            where is_active
            do update set
              display_name = excluded.display_name,
              brand = excluded.brand,
              barcode = excluded.barcode,
              basis = excluded.basis,
              kcal = excluded.kcal,
              protein_g = excluded.protein_g,
              carbs_g = excluded.carbs_g,
              fat_g = excluded.fat_g,
              fiber_g = excluded.fiber_g,
              sugar_g = excluded.sugar_g,
              sodium_mg = excluded.sodium_mg,
              is_verified = excluded.is_verified,
              is_active = true,
              updated_at = now()
            returning my_food_id, owner_user_id, display_name, brand, variant,
                      source_type, source_food_id, source, source_id, barcode,
                      basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
                      is_verified, is_active, created_at, updated_at
            """,
            owner,
            str(desc).strip(),
            str(brand_owner).strip() if brand_owner else None,
            str(variant).strip() if variant else None,
            str(int(fdc_id)),
            str(gtin).strip() if gtin else None,
            kcal, protein, carbs, fat, fiber, sugar, sodium_mg,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()

@router.post("/meal_plans/{meal_plan_id}/items/add")
async def add_item(
    meal_plan_id: str,
    my_food_id: str | None = Query(None),
    food_id: str | None = Query(None),  # legacy
    meal_label: str = Query("other"),
    sort_order: int = Query(0),
    qty_g: float | None = Query(None),
    qty_servings: float | None = Query(None),
    notes: str | None = Query(None),
):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")

    if meal_label not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="meal_label must be breakfast|lunch|dinner|snack|other")

    if not my_food_id and not food_id:
        raise HTTPException(status_code=400, detail="must provide my_food_id (preferred) or food_id (legacy)")

    conn = await _db()
    try:
        mfid = None
        fid = None

        if my_food_id:
            mfid = _as_uuid(my_food_id, "my_food_id")
            # must exist + active
            ok = await conn.fetchval(
                f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid",
                mfid,
            )
            if ok is not True:
                raise HTTPException(status_code=404, detail="my_food not found or inactive")

        if (not mfid) and food_id:
            fid = _as_uuid(food_id, "food_id")
            # legacy: ensure catalog food is public/active
            ok = await conn.fetchval(
                "select (is_public and is_active) from catalog_dev.food where food_id=$1::uuid",
                fid,
            )
            if ok is not True:
                raise HTTPException(status_code=400, detail="food_id is not approved/public")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.meal_plan_item
              (meal_plan_id, meal_label, sort_order, my_food_id, food_id, qty_g, qty_servings, notes)
            values
              ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6, $7, $8)
            returning meal_plan_item_id, meal_plan_id, meal_label, sort_order, my_food_id, food_id, qty_g, qty_servings, notes,
                      created_at, updated_at
            """,
            mpid, meal_label, sort_order,
            mfid, fid,
            qty_g, qty_servings, notes,
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
            select
              i.meal_plan_item_id, i.meal_plan_id, i.meal_label, i.sort_order,
              i.my_food_id, i.food_id, i.qty_g, i.qty_servings, i.notes,
              coalesce(m.display_name, f.display_name) as display_name,
              coalesce(m.brand, f.brand) as brand,
              coalesce(m.kcal, f.kcal) as kcal,
              coalesce(m.protein_g, f.protein_g) as protein_g,
              coalesce(m.carbs_g, f.carbs_g) as carbs_g,
              coalesce(m.fat_g, f.fat_g) as fat_g,
              i.created_at, i.updated_at
            from {SCHEMA}.meal_plan_item i
            left join {SCHEMA}.my_food m on m.my_food_id = i.my_food_id
            left join catalog_dev.food f on f.food_id = i.food_id
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
            insert into {SCHEMA}.my_food
              (owner_user_id, display_name, brand, variant,
              source_type, source_food_id, source, source_id, barcode,
              basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
              is_verified, is_active)
            )
            values(
              values
              ($1::uuid, $2, $3, $4,
              'usda', null, 'usda_fdc', $5, $6,
              'per_100g', $7, $8, $9, $10, $11, $12, $13,
              true, true)
            on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
            where is_active
            do update set
              display_name = excluded.display_name,
              brand = excluded.brand,
              barcode = excluded.barcode,
              basis = excluded.basis,
              kcal = excluded.kcal,
              protein_g = excluded.protein_g,
              carbs_g = excluded.carbs_g,
              fat_g = excluded.fat_g,
              fiber_g = excluded.fiber_g,
              sugar_g = excluded.sugar_g,
              sodium_mg = excluded.sodium_mg,
              is_verified = excluded.is_verified,
              is_active = true,
              updated_at = now()
            returning my_food_id, owner_user_id, display_name, brand, variant,
                      source_type, source_food_id, source, source_id, barcode,
                      basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
                      is_verified, is_active, created_at, updated_at;
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



@router.post("/my_foods/{my_food_id}/deactivate")
async def deactivate_my_food(my_food_id: str):
    fid = _as_uuid(my_food_id, "my_food_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.my_food
            set is_active=false, updated_at=now()
            where my_food_id=$1::uuid
            returning my_food_id, owner_user_id, display_name, brand, variant, source_type, source_id, is_active, updated_at
            """,
            fid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="my_food not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


# ----------------------------
# My Food servings (presets)
# ----------------------------

@router.get("/my_foods/{my_food_id}/servings")
async def list_my_food_servings(my_food_id: str):
    fid = _as_uuid(my_food_id, "my_food_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default, created_at, updated_at
            from {SCHEMA}.my_food_serving
            where my_food_id = $1::uuid
            order by is_default desc, lower(name), grams
            """,
            fid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/my_foods/{my_food_id}/servings/create")
async def create_my_food_serving(
    my_food_id: str,
    name: str = Query(..., min_length=1, max_length=120),
    grams: float = Query(..., gt=0),
    is_default: int = Query(0, ge=0, le=1),
):
    fid = _as_uuid(my_food_id, "my_food_id")
    nm = (name or "").strip()
    if not nm:
        raise HTTPException(status_code=400, detail="name required")

    conn = await _db()
    try:
        ok = await conn.fetchval(
            f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if ok is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")

        # Idempotency: if same (name, grams) exists, return it; optionally set default.
        row = await conn.fetchrow(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default, created_at, updated_at
            from {SCHEMA}.my_food_serving
            where my_food_id=$1::uuid
              and lower(name)=lower($2)
              and grams=$3
            limit 1
            """,
            fid, nm, grams,
        )

        if row:
            if is_default == 1 and row["is_default"] is not True:
                await conn.execute(
                    f"update {SCHEMA}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and is_default",
                    fid,
                )
                await conn.execute(
                    f"update {SCHEMA}.my_food_serving set is_default=true, updated_at=now() where my_food_serving_id=$1::uuid",
                    row["my_food_serving_id"],
                )
                row = await conn.fetchrow(
                    f"""
                    select my_food_serving_id, my_food_id, name, grams, is_default, created_at, updated_at
                    from {SCHEMA}.my_food_serving
                    where my_food_serving_id=$1::uuid
                    """,
                    row["my_food_serving_id"],
                )
            return JSONResponse(_row_to_jsonable(row))

        if is_default == 1:
            await conn.execute(
                f"update {SCHEMA}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and is_default",
                fid,
            )

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food_serving (my_food_id, name, grams, is_default)
            values ($1::uuid, $2, $3, $4::bool)
            returning my_food_serving_id, my_food_id, name, grams, is_default, created_at, updated_at
            """,
            fid, nm, grams, (is_default == 1),
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()

