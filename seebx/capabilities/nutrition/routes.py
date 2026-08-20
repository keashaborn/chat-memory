from __future__ import annotations

import os
import uuid
import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.adapters.usda_fdc import (
    UsdaFdcError,
    nutrient_summary as usda_nutrient_summary,
    usda_fdc_client,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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

SCHEMA = os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")

CATALOG_SCHEMA = os.getenv("CATALOG_SCHEMA", "catalog_dev")


def _positive_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _infer_usda_serving_grams(food: dict, per_100g: dict[str, float | None]):
    serving_size = _positive_float((food or {}).get("servingSize"))
    serving_unit = str((food or {}).get("servingSizeUnit") or "").strip().lower()
    if serving_size is not None and serving_unit in ("g", "gram", "grams", "grm"):
        return serving_size

    # Branded USDA records sometimes mislabel gram-equivalent servings as MLT or MG.
    # Derive the serving mass only when multiple label/per-100g nutrient ratios agree.
    label = (food or {}).get("labelNutrients") or {}
    candidates = []
    for label_key, nutrient_key in (
        ("calories", "kcal"),
        ("protein", "protein"),
        ("carbohydrates", "carbs"),
        ("fat", "fat"),
        ("fiber", "fiber"),
        ("sugars", "sugar"),
        ("sodium", "sodium_mg"),
    ):
        label_value = _positive_float((label.get(label_key) or {}).get("value"))
        per_100_value = _positive_float(per_100g.get(nutrient_key))
        if label_value is None or per_100_value is None:
            continue
        inferred = label_value * 100.0 / per_100_value
        if 0.1 <= inferred <= 5000:
            candidates.append(inferred)

    if len(candidates) < 2:
        return None

    candidates.sort()
    midpoint = len(candidates) // 2
    median = (
        candidates[midpoint]
        if len(candidates) % 2
        else (candidates[midpoint - 1] + candidates[midpoint]) / 2.0
    )
    tolerance = max(2.0, median * 0.08)
    inliers = [value for value in candidates if abs(value - median) <= tolerance]
    if len(inliers) < 2:
        return None

    inliers.sort()
    midpoint = len(inliers) // 2
    inferred_grams = (
        inliers[midpoint]
        if len(inliers) % 2
        else (inliers[midpoint - 1] + inliers[midpoint]) / 2.0
    )
    return round(inferred_grams, 3)


class MyFoodUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=200)
    brand: str | None = Field(None, max_length=200)
    variant: str | None = Field(None, max_length=120)
    barcode: str | None = Field(None, max_length=80)
    preferred_mode: str | None = None
    preferred_quantity: float | None = Field(None, gt=0)
    preferred_serving_id: str | None = None
    nutrient_source: str | None = Field(None, max_length=40)
    nutrient_source_detail: str | None = Field(None, max_length=240)
    kcal: float | None = Field(None, ge=0)
    protein_g: float | None = Field(None, ge=0)
    carbs_g: float | None = Field(None, ge=0)
    fat_g: float | None = Field(None, ge=0)
    fiber_g: float | None = Field(None, ge=0)
    sugar_g: float | None = Field(None, ge=0)
    sodium_mg: float | None = Field(None, ge=0)
    is_verified: bool | None = None


class MyFoodServingUpdate(BaseModel):
    name: str | None = Field(None, max_length=120)
    grams: float | None = Field(None, gt=0)
    is_active: bool | None = None
    set_preferred: bool | None = None


MY_FOOD_RETURN_COLUMNS = """
  f.my_food_id, f.owner_user_id, f.display_name, f.source_display_name,
  f.brand, f.variant, f.source_type, f.source_food_id, f.source, f.source_id,
  f.barcode, f.basis, f.kcal, f.protein_g, f.carbs_g, f.fat_g,
  f.fiber_g, f.sugar_g, f.sodium_mg, f.nutrient_source,
  f.nutrient_source_detail, f.nutrient_updated_at, f.preferred_mode,
  f.preferred_quantity, f.preferred_serving_id,
  ps.name as preferred_serving_name, ps.grams as preferred_serving_grams,
  f.is_verified, f.is_active, f.created_at, f.updated_at
"""


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")

async def _db(req: Request):
    return await connect_lifeswitch(req)

@router.get("/meal_plans")
async def list_meal_plans(req: Request, owner_user_id: str = Query(...)):
    uid = require_actor_matches_owner(req, owner_user_id)
    conn = await _db(req)
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
    req: Request,
    owner_user_id: str = Query(...),
    name: str = Query(..., min_length=1, max_length=80),
    goal: str = Query("maintain"),
    target_kcal: float | None = Query(None),
    target_protein_g: float | None = Query(None),
    target_carbs_g: float | None = Query(None),
    target_fat_g: float | None = Query(None),
):
    uid = require_actor_matches_owner(req, owner_user_id)
    if goal not in ("cut", "bulk", "maintain"):
        raise HTTPException(status_code=400, detail="goal must be cut|bulk|maintain")

    conn = await _db(req)
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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    fdc_id: int = Query(..., ge=1),
    variant: str | None = Query(None, max_length=120),
):
    owner = require_actor_matches_owner(req, owner_user_id)

    try:
        j = await usda_fdc_client().detail(fdc_id)
    except UsdaFdcError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    desc = (j or {}).get("description") or f"FDC {fdc_id}"
    brand_owner = (j or {}).get("brandOwner") or (j or {}).get("brandName")
    gtin = (j or {}).get("gtinUpc")

    # Branded/package foods often include label serving metadata.
    # Keep macros normalized per 100g, but create a user-facing serving row when possible.
    household_serving = str((j or {}).get("householdServingFullText") or "").strip()

    nutrients = usda_nutrient_summary(j or {})
    kcal = nutrients["kcal"]
    protein = nutrients["protein_g"]
    carbs = nutrients["carbs_g"]
    fat = nutrients["fat_g"]
    fiber = nutrients["fiber_g"]
    sugar = nutrients["sugar_g"]
    sodium_mg = nutrients["sodium_mg"]
    grams = _infer_usda_serving_grams(
        j or {},
        {
            "kcal": kcal,
            "protein": protein,
            "carbs": carbs,
            "fat": fat,
            "fiber": fiber,
            "sugar": sugar,
            "sodium_mg": sodium_mg,
        },
    )

    conn = await _db(req)
    try:
        async with conn.transaction():
            source_id = str(int(fdc_id))
            normalized_variant = str(variant).strip() if variant else None
            existing_food = await conn.fetchrow(
                f"""
                select my_food_id, nutrient_source
                from {SCHEMA}.my_food
                where owner_user_id=$1::uuid
                  and source_type='usda'
                  and source_id=$2
                  and coalesce(variant,'')=coalesce($3,'')
                  and is_active
                limit 1
                """,
                owner,
                source_id,
                normalized_variant,
            )
            is_new_food = existing_food is None

            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.my_food as current
                  (owner_user_id, display_name, source_display_name, brand, variant,
                   source_type, source_food_id, source, source_id, barcode,
                   basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
                   nutrient_source, nutrient_updated_at, is_verified, is_active)
                values
                  ($1::uuid, $2, $2, $3, $4,
                   'usda', null, 'usda_fdc', $5, $6,
                   'per_100g', $7, $8, $9, $10, $11, $12, $13,
                   'usda', now(), true, true)
                on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
                where is_active
                do update set
                  source_display_name = excluded.source_display_name,
                  brand = excluded.brand,
                  barcode = excluded.barcode,
                  basis = excluded.basis,
                  kcal = case when current.nutrient_source='usda' then excluded.kcal else current.kcal end,
                  protein_g = case when current.nutrient_source='usda' then excluded.protein_g else current.protein_g end,
                  carbs_g = case when current.nutrient_source='usda' then excluded.carbs_g else current.carbs_g end,
                  fat_g = case when current.nutrient_source='usda' then excluded.fat_g else current.fat_g end,
                  fiber_g = case when current.nutrient_source='usda' then excluded.fiber_g else current.fiber_g end,
                  sugar_g = case when current.nutrient_source='usda' then excluded.sugar_g else current.sugar_g end,
                  sodium_mg = case when current.nutrient_source='usda' then excluded.sodium_mg else current.sodium_mg end,
                  nutrient_updated_at = case when current.nutrient_source='usda' then now() else current.nutrient_updated_at end,
                  is_verified = case when current.nutrient_source='usda' then true else current.is_verified end,
                  is_active = true,
                  updated_at = now()
                returning my_food_id
                """,
                owner,
                str(desc).strip(),
                str(brand_owner).strip() if brand_owner else None,
                normalized_variant,
                source_id,
                str(gtin).strip() if gtin else None,
                kcal, protein, carbs, fat, fiber, sugar, sodium_mg,
            )
            if not row:
                raise HTTPException(status_code=500, detail="insert_failed")

            if grams is not None and grams > 0:
                serving_name = household_serving or "1 serving"
                serving_name = " ".join(str(serving_name).strip().split())[:120] or "1 serving"
                fid = row["my_food_id"]
                existing_serving = await conn.fetchrow(
                    f"""
                    select my_food_serving_id, source_type
                    from {SCHEMA}.my_food_serving
                    where my_food_id=$1::uuid and lower(name)=lower($2)
                    limit 1
                    """,
                    fid,
                    serving_name,
                )
                has_default = await conn.fetchval(
                    f"select 1 from {SCHEMA}.my_food_serving where my_food_id=$1::uuid and is_default and is_active",
                    fid,
                )
                should_default = is_new_food or not has_default

                if should_default:
                    await conn.execute(
                        f"update {SCHEMA}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and is_default",
                        fid,
                    )

                if existing_serving:
                    serving_id = existing_serving["my_food_serving_id"]
                    await conn.execute(
                        f"""
                        update {SCHEMA}.my_food_serving
                        set grams=case when source_type in ('usda','legacy') then $2 else grams end,
                            source_type=case when source_type in ('usda','legacy') then 'usda' else source_type end,
                            source_label=coalesce(source_label, $3),
                            is_active=true,
                            is_default=case when $4::bool then true else is_default end,
                            updated_at=now()
                        where my_food_serving_id=$1::uuid
                        """,
                        serving_id,
                        grams,
                        serving_name,
                        should_default,
                    )
                else:
                    serving_id = await conn.fetchval(
                        f"""
                        insert into {SCHEMA}.my_food_serving
                          (my_food_id, name, grams, is_default, source_type, source_label, is_active)
                        values ($1::uuid, $2, $3, $4, 'usda', $2, true)
                        returning my_food_serving_id
                        """,
                        fid,
                        serving_name,
                        grams,
                        should_default,
                    )

                if is_new_food and serving_id:
                    await conn.execute(
                        f"""
                        update {SCHEMA}.my_food
                        set preferred_mode='serving', preferred_quantity=1,
                            preferred_serving_id=$2::uuid, updated_at=now()
                        where my_food_id=$1::uuid
                        """,
                        fid,
                        serving_id,
                    )

            updated = await conn.fetchrow(
                f"""
                select {MY_FOOD_RETURN_COLUMNS}
                from {SCHEMA}.my_food f
                left join {SCHEMA}.my_food_serving ps
                  on ps.my_food_serving_id=f.preferred_serving_id
                 and ps.my_food_id=f.my_food_id
                where f.my_food_id=$1::uuid
                """,
                row["my_food_id"],
            )
            return JSONResponse(_row_to_jsonable(updated))
    finally:
        await conn.close()

@router.post("/meal_plans/{meal_plan_id}/items/add")
async def add_item(
    meal_plan_id: str,
    req: Request,
    my_food_id: str | None = Query(None),
    food_id: str | None = Query(None),  # legacy
    meal_label: str = Query("other"),
    sort_order: int = Query(0),
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
    notes: str | None = Query(None),
):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")

    if meal_label not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="meal_label must be breakfast|lunch|dinner|snack|other")

    if not my_food_id and not food_id:
        raise HTTPException(status_code=400, detail="must provide my_food_id (preferred) or food_id (legacy)")

    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None
    if use_grams and use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity, not both")
    if not use_grams and not use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity")
    if use_serving and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="serving mode requires my_food_serving_id and qty_servings")
    if food_id and use_serving:
        raise HTTPException(status_code=400, detail="legacy catalog foods support grams only")

    conn = await _db(req)
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.meal_plan where meal_plan_id=$1::uuid and is_active",
            mpid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="meal_plan not found or inactive")
        owner = require_actor_matches_owner(req, str(owner))

        mfid = None
        fid = None
        sid = None

        if my_food_id:
            mfid = _as_uuid(my_food_id, "my_food_id")
            # must exist + active + belong to same owner as meal plan
            ok = await conn.fetchval(
                f"select is_active from {SCHEMA}.my_food where my_food_id=$1::uuid and owner_user_id=$2::uuid",
                mfid,
                owner,
            )
            if ok is not True:
                raise HTTPException(status_code=404, detail="my_food not found or inactive")

            if use_serving:
                sid = _as_uuid(my_food_serving_id, "my_food_serving_id")
                serving_ok = await conn.fetchval(
                    f"""
                    select 1
                    from {SCHEMA}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active
                    """,
                    sid,
                    mfid,
                )
                if not serving_ok:
                    raise HTTPException(status_code=400, detail="active serving not found for this food")

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
              (meal_plan_id, meal_label, sort_order, my_food_id, food_id,
               qty_g, my_food_serving_id, qty_servings, notes)
            values
              ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6, $7::uuid, $8, $9)
            returning meal_plan_item_id, meal_plan_id, meal_label, sort_order,
                      my_food_id, food_id, qty_g, my_food_serving_id, qty_servings, notes,
                      created_at, updated_at
            """,
            mpid, meal_label, sort_order,
            mfid, fid,
            qty_g, sid, qty_servings, notes,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()

@router.patch("/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}")
async def update_meal_plan_item(
    meal_plan_id: str,
    meal_plan_item_id: str,
    req: Request,
    meal_label: str = Query(...),
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    item_id = _as_uuid(meal_plan_item_id, "meal_plan_item_id")
    if meal_label not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="invalid meal_label")

    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None
    if use_grams and use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity, not both")
    if not use_grams and not use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity")
    if use_serving and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="serving mode requires my_food_serving_id and qty_servings")

    conn = await _db(req)
    try:
        async with conn.transaction():
            item = await conn.fetchrow(
                f"""
                select i.my_food_id, i.food_id, p.owner_user_id
                from {SCHEMA}.meal_plan_item i
                join {SCHEMA}.meal_plan p on p.meal_plan_id=i.meal_plan_id
                where i.meal_plan_id=$1::uuid
                  and i.meal_plan_item_id=$2::uuid
                """,
                mpid,
                item_id,
            )
            if not item:
                raise HTTPException(status_code=404, detail="meal plan item not found")
            require_actor_matches_owner(req, str(item["owner_user_id"]))
            if item["food_id"] is not None and use_serving:
                raise HTTPException(status_code=400, detail="legacy catalog foods support grams only")

            sid = None
            if use_serving:
                sid = _as_uuid(my_food_serving_id, "my_food_serving_id")
                serving_ok = await conn.fetchval(
                    f"""
                    select 1
                    from {SCHEMA}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active
                    """,
                    sid,
                    item["my_food_id"],
                )
                if not serving_ok:
                    raise HTTPException(status_code=400, detail="active serving not found for this food")

            row = await conn.fetchrow(
                f"""
                update {SCHEMA}.meal_plan_item
                set meal_label=$3,
                    qty_g=$4,
                    my_food_serving_id=$5::uuid,
                    qty_servings=$6,
                    updated_at=now()
                where meal_plan_id=$1::uuid
                  and meal_plan_item_id=$2::uuid
                returning meal_plan_item_id, meal_plan_id, meal_label, sort_order,
                          my_food_id, food_id, qty_g, my_food_serving_id, qty_servings,
                          notes, created_at, updated_at
                """,
                mpid,
                item_id,
                meal_label,
                qty_g if use_grams else None,
                sid,
                qty_servings if use_serving else None,
            )
            return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.delete("/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}")
async def delete_meal_plan_item(meal_plan_id: str, meal_plan_item_id: str, req: Request):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    item_id = _as_uuid(meal_plan_item_id, "meal_plan_item_id")
    conn = await _db(req)
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.meal_plan where meal_plan_id=$1::uuid",
            mpid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="meal plan not found")
        require_actor_matches_owner(req, str(owner))

        row = await conn.fetchrow(
            f"""
            delete from {SCHEMA}.meal_plan_item
            where meal_plan_id=$1::uuid
              and meal_plan_item_id=$2::uuid
            returning meal_plan_item_id
            """,
            mpid,
            item_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="meal plan item not found")
        return JSONResponse({"deleted": str(row["meal_plan_item_id"])})
    finally:
        await conn.close()


@router.get("/meal_plans/{meal_plan_id}/items")
async def list_items(meal_plan_id: str, req: Request):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    conn = await _db(req)
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.meal_plan where meal_plan_id=$1::uuid",
            mpid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="meal_plan not found")
        require_actor_matches_owner(req, str(owner))

        rows = await conn.fetch(
            f"""
            select
              i.meal_plan_item_id, i.meal_plan_id, i.meal_label, i.sort_order,
              i.my_food_id, i.food_id, i.qty_g, i.my_food_serving_id, i.qty_servings, i.notes,
              s.name as serving_name,
              s.grams as serving_grams,
              coalesce(i.qty_g, s.grams * i.qty_servings) as qty_g_resolved,
              coalesce(m.display_name, f.display_name) as display_name,
              coalesce(m.brand, f.brand) as brand,
              coalesce(m.kcal, f.kcal) as kcal,
              coalesce(m.protein_g, f.protein_g) as protein_g,
              coalesce(m.carbs_g, f.carbs_g) as carbs_g,
              coalesce(m.fat_g, f.fat_g) as fat_g,
              i.created_at, i.updated_at
            from {SCHEMA}.meal_plan_item i
            left join {SCHEMA}.my_food m on m.my_food_id = i.my_food_id
            left join {SCHEMA}.my_food_serving s
              on s.my_food_serving_id = i.my_food_serving_id
             and s.my_food_id = i.my_food_id
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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    q: str | None = Query(None, min_length=1, max_length=120),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db(req)
    try:
        where = "f.owner_user_id = $1::uuid"
        args: list[object] = [owner]

        if include_inactive == 0:
            where += " and f.is_active"

        if q:
            where += " and (f.display_name ilike $2 or coalesce(f.brand,'') ilike $2 or coalesce(f.variant,'') ilike $2)"
            args.append(f"%{q}%")

        rows = await conn.fetch(
            f"""
            select {MY_FOOD_RETURN_COLUMNS}
            from {SCHEMA}.my_food f
            left join {SCHEMA}.my_food_serving ps
              on ps.my_food_serving_id = f.preferred_serving_id
             and ps.my_food_id = f.my_food_id
            where {where}
            order by lower(f.display_name), lower(coalesce(f.brand,'')), lower(coalesce(f.variant,''))
            """,
            *args,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/my_foods/create_from_catalog")
async def create_my_food_from_catalog(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    food_id: str = Query(..., min_length=1),
    variant: str | None = Query(None, max_length=128),
    display_name: str | None = Query(None, max_length=200),
    brand: str | None = Query(None, max_length=200),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    fid = _as_uuid(food_id, "food_id")

    conn = await _db(req)
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
        src_id_txt = str(src_id) if src_id is not None else str(fid)
        source_display_name = str(src["display_name"] or dn).strip()

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food as current
              (owner_user_id, display_name, source_display_name, brand, variant,
               source_type, source_food_id, source, source_id, barcode,
               basis, kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               nutrient_source, nutrient_updated_at, is_verified, is_active)
            values
              ($1::uuid, $2, $3, $4, $5,
               'catalog', $6::uuid, $7, $8, $9,
               $10, $11, $12, $13, $14, $15, $16, $17,
               'catalog', now(), true, true)
            on conflict (owner_user_id, source_type, source_id, coalesce(variant,''))
            where is_active
            do update set
              source_display_name = excluded.source_display_name,
              brand = excluded.brand,
              barcode = excluded.barcode,
              basis = excluded.basis,
              kcal = case when current.nutrient_source='catalog' then excluded.kcal else current.kcal end,
              protein_g = case when current.nutrient_source='catalog' then excluded.protein_g else current.protein_g end,
              carbs_g = case when current.nutrient_source='catalog' then excluded.carbs_g else current.carbs_g end,
              fat_g = case when current.nutrient_source='catalog' then excluded.fat_g else current.fat_g end,
              fiber_g = case when current.nutrient_source='catalog' then excluded.fiber_g else current.fiber_g end,
              sugar_g = case when current.nutrient_source='catalog' then excluded.sugar_g else current.sugar_g end,
              sodium_mg = case when current.nutrient_source='catalog' then excluded.sodium_mg else current.sodium_mg end,
              nutrient_updated_at = case when current.nutrient_source='catalog' then now() else current.nutrient_updated_at end,
              is_active = true,
              updated_at = now()
            returning my_food_id
            """,
            owner,
            dn,
            source_display_name,
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
        updated = await conn.fetchrow(
            f"""
            select {MY_FOOD_RETURN_COLUMNS}
            from {SCHEMA}.my_food f
            left join {SCHEMA}.my_food_serving ps
              on ps.my_food_serving_id=f.preferred_serving_id
             and ps.my_food_id=f.my_food_id
            where f.my_food_id=$1::uuid
            """,
            row["my_food_id"],
        )
        return JSONResponse(_row_to_jsonable(updated))
    finally:
        await conn.close()



@router.patch("/my_foods/{my_food_id}")
async def update_my_food(
    my_food_id: str,
    payload: MyFoodUpdate,
    req: Request,
):
    fid = _as_uuid(my_food_id, "my_food_id")
    fields = set(payload.__fields_set__)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields provided")

    conn = await _db(req)
    try:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"select * from {SCHEMA}.my_food where my_food_id=$1::uuid for update",
                fid,
            )
            if not current:
                raise HTTPException(status_code=404, detail="my_food not found")
            require_actor_matches_owner(req, str(current["owner_user_id"]))

            display_name = current["display_name"]
            if "display_name" in fields:
                display_name = str(payload.display_name or "").strip()
                if not display_name:
                    raise HTTPException(status_code=400, detail="display_name required")

            def optional_text(name: str, value, current_value):
                if name not in fields:
                    return current_value
                cleaned = str(value or "").strip()
                return cleaned or None

            brand = optional_text("brand", payload.brand, current["brand"])
            variant = optional_text("variant", payload.variant, current["variant"])
            barcode = optional_text("barcode", payload.barcode, current["barcode"])
            nutrient_source_detail = optional_text(
                "nutrient_source_detail",
                payload.nutrient_source_detail,
                current["nutrient_source_detail"],
            )

            nutrient_fields = {
                "kcal", "protein_g", "carbs_g", "fat_g",
                "fiber_g", "sugar_g", "sodium_mg",
            }
            nutrients_changed = bool(fields.intersection(nutrient_fields))

            nutrient_source = current["nutrient_source"]
            if "nutrient_source" in fields:
                nutrient_source = str(payload.nutrient_source or "").strip()
                if not nutrient_source:
                    raise HTTPException(status_code=400, detail="nutrient_source required")
            elif nutrients_changed:
                nutrient_source = "manual"

            preferred_mode = (
                str(payload.preferred_mode or "").strip().lower()
                if "preferred_mode" in fields
                else current["preferred_mode"]
            )
            preferred_quantity = (
                payload.preferred_quantity
                if "preferred_quantity" in fields
                else current["preferred_quantity"]
            )
            preferred_serving_id = current["preferred_serving_id"]
            if "preferred_serving_id" in fields:
                preferred_serving_id = (
                    _as_uuid(payload.preferred_serving_id, "preferred_serving_id")
                    if payload.preferred_serving_id
                    else None
                )

            if preferred_mode not in ("grams", "serving"):
                raise HTTPException(status_code=400, detail="preferred_mode must be grams|serving")
            if preferred_quantity is None or float(preferred_quantity) <= 0:
                raise HTTPException(status_code=400, detail="preferred_quantity must be > 0")
            if preferred_mode == "grams" and preferred_serving_id is not None:
                raise HTTPException(status_code=400, detail="grams mode cannot include preferred_serving_id")
            if preferred_mode == "serving":
                if preferred_serving_id is None:
                    raise HTTPException(status_code=400, detail="serving mode requires preferred_serving_id")
                serving_ok = await conn.fetchval(
                    f"""
                    select 1
                    from {SCHEMA}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                      and is_active
                    """,
                    preferred_serving_id,
                    fid,
                )
                if not serving_ok:
                    raise HTTPException(status_code=400, detail="preferred serving is not active for this food")

            def numeric_value(name: str, payload_value):
                return payload_value if name in fields else current[name]

            row = await conn.fetchrow(
                f"""
                update {SCHEMA}.my_food
                set display_name=$2,
                    brand=$3,
                    variant=$4,
                    barcode=$5,
                    kcal=$6,
                    protein_g=$7,
                    carbs_g=$8,
                    fat_g=$9,
                    fiber_g=$10,
                    sugar_g=$11,
                    sodium_mg=$12,
                    nutrient_source=$13,
                    nutrient_source_detail=$14,
                    nutrient_updated_at=case when $15::bool then now() else nutrient_updated_at end,
                    preferred_mode=$16,
                    preferred_quantity=$17,
                    preferred_serving_id=$18::uuid,
                    is_verified=$19,
                    updated_at=now()
                where my_food_id=$1::uuid
                returning my_food_id
                """,
                fid,
                display_name,
                brand,
                variant,
                barcode,
                numeric_value("kcal", payload.kcal),
                numeric_value("protein_g", payload.protein_g),
                numeric_value("carbs_g", payload.carbs_g),
                numeric_value("fat_g", payload.fat_g),
                numeric_value("fiber_g", payload.fiber_g),
                numeric_value("sugar_g", payload.sugar_g),
                numeric_value("sodium_mg", payload.sodium_mg),
                nutrient_source,
                nutrient_source_detail,
                nutrients_changed or "nutrient_source" in fields,
                preferred_mode,
                preferred_quantity,
                preferred_serving_id,
                payload.is_verified if "is_verified" in fields else current["is_verified"],
            )
            if not row:
                raise HTTPException(status_code=404, detail="my_food not found")

            updated = await conn.fetchrow(
                f"""
                select {MY_FOOD_RETURN_COLUMNS}
                from {SCHEMA}.my_food f
                left join {SCHEMA}.my_food_serving ps
                  on ps.my_food_serving_id=f.preferred_serving_id
                 and ps.my_food_id=f.my_food_id
                where f.my_food_id=$1::uuid
                """,
                fid,
            )
            return JSONResponse(_row_to_jsonable(updated))
    finally:
        await conn.close()


@router.post("/my_foods/{my_food_id}/deactivate")
async def deactivate_my_food(my_food_id: str, req: Request):
    fid = _as_uuid(my_food_id, "my_food_id")
    conn = await _db(req)
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="my_food not found")
        require_actor_matches_owner(req, str(owner))

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
async def list_my_food_servings(my_food_id: str, req: Request):
    fid = _as_uuid(my_food_id, "my_food_id")
    conn = await _db(req)
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="my_food not found")
        require_actor_matches_owner(req, str(owner))

        rows = await conn.fetch(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default,
                   source_type, source_label, is_active, created_at, updated_at
            from {SCHEMA}.my_food_serving
            where my_food_id = $1::uuid and is_active
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
    req: Request,
    name: str = Query(..., min_length=1, max_length=120),
    grams: float = Query(..., gt=0),
    is_default: int = Query(0, ge=0, le=1),
):
    fid = _as_uuid(my_food_id, "my_food_id")
    nm = (name or "").strip()
    if not nm:
        raise HTTPException(status_code=400, detail="name required")

    conn = await _db(req)
    try:
        food = await conn.fetchrow(
            f"select owner_user_id, is_active from {SCHEMA}.my_food where my_food_id=$1::uuid",
            fid,
        )
        if not food or food["is_active"] is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")
        require_actor_matches_owner(req, str(food["owner_user_id"]))

        # Upsert by (my_food_id, lower(name)):
        # - If name exists, overwrite grams and optionally set default.
        # - If not, insert new row.
        row = await conn.fetchrow(
            f"""
            select my_food_serving_id, my_food_id, name, grams, is_default,
                   source_type, source_label, is_active, created_at, updated_at
            from {SCHEMA}.my_food_serving
            where my_food_id=$1::uuid
              and lower(name)=lower($2)
            order by updated_at desc nulls last, created_at desc
            limit 1
            """,
            fid,
            nm,
        )

        if row:
            sid = row["my_food_serving_id"]

            await conn.execute(
                f"""
                update {SCHEMA}.my_food_serving
                set name=$2,
                    grams=$3,
                    source_type='manual',
                    source_label=coalesce(source_label, name),
                    is_active=true,
                    updated_at=now()
                where my_food_serving_id=$1::uuid
                """,
                sid,
                nm,
                grams,
            )

            # optionally set as default
            if is_default == 1 and row["is_default"] is not True:
                await conn.execute(
                    f"""
                    update {SCHEMA}.my_food_serving
                    set is_default=false, updated_at=now()
                    where my_food_id=$1::uuid and is_default
                    """,
                    fid,
                )
                await conn.execute(
                    f"""
                    update {SCHEMA}.my_food_serving
                    set is_default=true, updated_at=now()
                    where my_food_serving_id=$1::uuid
                    """,
                    sid,
                )

            if is_default == 1:
                await conn.execute(
                    f"""
                    update {SCHEMA}.my_food
                    set preferred_mode='serving',
                        preferred_quantity=1,
                        preferred_serving_id=$2::uuid,
                        updated_at=now()
                    where my_food_id=$1::uuid
                    """,
                    fid,
                    sid,
                )

            row2 = await conn.fetchrow(
                f"""
                select my_food_serving_id, my_food_id, name, grams, is_default,
                       source_type, source_label, is_active, created_at, updated_at
                from {SCHEMA}.my_food_serving
                where my_food_serving_id=$1::uuid
                """,
                sid,
            )
            return JSONResponse(_row_to_jsonable(row2 or row))

        # insert new row
        if is_default == 1:
            await conn.execute(
                f"""
                update {SCHEMA}.my_food_serving
                set is_default=false, updated_at=now()
                where my_food_id=$1::uuid and is_default
                """,
                fid,
            )

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_food_serving
              (my_food_id, name, grams, is_default, source_type, source_label, is_active)
            values ($1::uuid, $2, $3, $4::bool, 'manual', $2, true)
            returning my_food_serving_id, my_food_id, name, grams, is_default,
                      source_type, source_label, is_active, created_at, updated_at
            """,
            fid,
            nm,
            grams,
            (is_default == 1),
        )

        if row and is_default == 1:
            await conn.execute(
                f"""
                update {SCHEMA}.my_food
                set preferred_mode='serving',
                    preferred_quantity=1,
                    preferred_serving_id=$2::uuid,
                    updated_at=now()
                where my_food_id=$1::uuid
                """,
                fid,
                row["my_food_serving_id"],
            )

        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()


@router.patch("/my_foods/{my_food_id}/servings/{my_food_serving_id}")
async def update_my_food_serving(
    my_food_id: str,
    my_food_serving_id: str,
    payload: MyFoodServingUpdate,
    req: Request,
):
    fid = _as_uuid(my_food_id, "my_food_id")
    sid = _as_uuid(my_food_serving_id, "my_food_serving_id")
    fields = set(payload.__fields_set__)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields provided")

    conn = await _db(req)
    try:
        async with conn.transaction():
            current = await conn.fetchrow(
                f"""
                select s.*, f.owner_user_id, f.preferred_serving_id
                from {SCHEMA}.my_food_serving s
                join {SCHEMA}.my_food f on f.my_food_id=s.my_food_id
                where s.my_food_serving_id=$1::uuid
                  and s.my_food_id=$2::uuid
                for update of s, f
                """,
                sid,
                fid,
            )
            if not current:
                raise HTTPException(status_code=404, detail="serving not found")
            require_actor_matches_owner(req, str(current["owner_user_id"]))

            name = current["name"]
            if "name" in fields:
                name = str(payload.name or "").strip()
                if not name:
                    raise HTTPException(status_code=400, detail="name required")

            grams = payload.grams if "grams" in fields else current["grams"]
            is_active = payload.is_active if "is_active" in fields else current["is_active"]
            if grams is None or float(grams) <= 0:
                raise HTTPException(status_code=400, detail="grams must be > 0")

            set_preferred = payload.set_preferred if "set_preferred" in fields else None
            if set_preferred is True and is_active is not True:
                raise HTTPException(status_code=400, detail="inactive serving cannot be preferred")

            manually_changed = bool(fields.intersection({"name", "grams"}))
            row = await conn.fetchrow(
                f"""
                update {SCHEMA}.my_food_serving
                set name=$3,
                    grams=$4,
                    is_active=$5,
                    source_type=case when $6::bool then 'manual' else source_type end,
                    source_label=coalesce(source_label, name),
                    is_default=case when $5::bool then is_default else false end,
                    updated_at=now()
                where my_food_serving_id=$1::uuid
                  and my_food_id=$2::uuid
                returning my_food_serving_id, my_food_id, name, grams, is_default,
                          source_type, source_label, is_active, created_at, updated_at
                """,
                sid,
                fid,
                name,
                grams,
                is_active,
                manually_changed,
            )

            currently_preferred = str(current["preferred_serving_id"] or "") == str(sid)
            if set_preferred is True:
                await conn.execute(
                    f"update {SCHEMA}.my_food_serving set is_default=false, updated_at=now() where my_food_id=$1::uuid and my_food_serving_id<>$2::uuid and is_default",
                    fid,
                    sid,
                )
                await conn.execute(
                    f"update {SCHEMA}.my_food_serving set is_default=true, updated_at=now() where my_food_serving_id=$1::uuid",
                    sid,
                )
                await conn.execute(
                    f"""
                    update {SCHEMA}.my_food
                    set preferred_mode='serving', preferred_quantity=1,
                        preferred_serving_id=$2::uuid, updated_at=now()
                    where my_food_id=$1::uuid
                    """,
                    fid,
                    sid,
                )
            elif currently_preferred and (is_active is not True or set_preferred is False):
                await conn.execute(
                    f"""
                    update {SCHEMA}.my_food
                    set preferred_mode='grams', preferred_quantity=$2,
                        preferred_serving_id=null, updated_at=now()
                    where my_food_id=$1::uuid
                    """,
                    fid,
                    grams,
                )
                await conn.execute(
                    f"update {SCHEMA}.my_food_serving set is_default=false, updated_at=now() where my_food_serving_id=$1::uuid",
                    sid,
                )

            updated = await conn.fetchrow(
                f"""
                select my_food_serving_id, my_food_id, name, grams, is_default,
                       source_type, source_label, is_active, created_at, updated_at
                from {SCHEMA}.my_food_serving
                where my_food_serving_id=$1::uuid
                """,
                sid,
            )
            return JSONResponse(_row_to_jsonable(updated or row))
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="serving name already exists for this food")
    finally:
        await conn.close()


# ----------------------------
# My Food overrides (alias + default grams + sort)
# ----------------------------

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
            where owner_user_id = $1::uuid
            order by sort_order asc, updated_at desc nulls last, created_at desc
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

    al = (alias or "").strip() if alias is not None else None
    if al == "":
        al = None

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
              set
                alias = coalesce(excluded.alias, {SCHEMA}.my_food_override.alias),
                default_grams = coalesce(excluded.default_grams, {SCHEMA}.my_food_override.default_grams),
                sort_order = excluded.sort_order,
                updated_at = now()
            returning owner_user_id, my_food_id, alias, default_grams, sort_order, created_at, updated_at
            """,
            owner, fid, al, default_grams, sort_order,
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()


@router.delete("/my_food_overrides")
async def delete_my_food_override(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    my_food_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    fid = _as_uuid(my_food_id, "my_food_id")
    conn = await _db(req)
    try:
        row = await conn.fetchrow(
            f"""
            delete from {SCHEMA}.my_food_override
            where owner_user_id=$1::uuid and my_food_id=$2::uuid
            returning owner_user_id, my_food_id
            """,
            owner, fid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="override not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
