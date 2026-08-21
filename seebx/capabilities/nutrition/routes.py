from __future__ import annotations

import uuid
from fastapi import APIRouter, HTTPException, Query, Request
from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.lifeswitch_foods_postgres import (
    FoodsRepositoryError,
    lifeswitch_foods_repository,
)
from seebx.adapters.lifeswitch_meal_plans_postgres import (
    MealPlansRepositoryError,
    lifeswitch_meal_plans_repository,
)
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



def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")

@router.get("/meal_plans")
async def list_meal_plans(req: Request, owner_user_id: str = Query(...)):
    uid = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_meal_plans_repository(req) as repository:
        rows = await repository.list_plans(owner_user_id=uid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])


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
    async with lifeswitch_meal_plans_repository(req) as repository:
        row = await repository.create_plan(
            owner_user_id=uid,
            name=name.strip(),
            goal=goal,
            target_kcal=target_kcal,
            target_protein_g=target_protein_g,
            target_carbs_g=target_carbs_g,
            target_fat_g=target_fat_g,
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})


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

    async with lifeswitch_foods_repository(req) as repository:
        async with repository.transaction():
            source_id = str(int(fdc_id))
            normalized_variant = str(variant).strip() if variant else None
            existing_food = await repository.find_active_usda_food(
                owner_user_id=owner,
                source_id=source_id,
                variant=normalized_variant,
            )
            is_new_food = existing_food is None

            row = await repository.upsert_usda_food(
                owner_user_id=owner,
                display_name=str(desc).strip(),
                brand=str(brand_owner).strip() if brand_owner else None,
                variant=normalized_variant,
                source_id=source_id,
                barcode=str(gtin).strip() if gtin else None,
                kcal=kcal,
                protein_g=protein,
                carbs_g=carbs,
                fat_g=fat,
                fiber_g=fiber,
                sugar_g=sugar,
                sodium_mg=sodium_mg,
            )
            if not row:
                raise HTTPException(status_code=500, detail="insert_failed")

            if grams is not None and grams > 0:
                serving_name = household_serving or "1 serving"
                serving_name = " ".join(str(serving_name).strip().split())[:120] or "1 serving"
                fid = row["my_food_id"]
                existing_serving = await repository.find_serving_by_name(
                    my_food_id=fid,
                    name=serving_name,
                )
                has_default = await repository.has_active_default_serving(my_food_id=fid)
                should_default = is_new_food or not has_default

                if should_default:
                    await repository.clear_default_servings(my_food_id=fid)

                if existing_serving:
                    serving_id = existing_serving["my_food_serving_id"]
                    await repository.update_imported_serving(
                        serving_id=serving_id,
                        grams=grams,
                        source_label=serving_name,
                        is_default=should_default,
                    )
                else:
                    serving_id = await repository.create_imported_serving(
                        my_food_id=fid,
                        name=serving_name,
                        grams=grams,
                        is_default=should_default,
                    )

                if is_new_food and serving_id:
                    await repository.set_preferred_serving(
                        my_food_id=fid,
                        serving_id=serving_id,
                    )

            updated = await repository.get_food_with_preferred(my_food_id=row["my_food_id"])
            return JSONResponse(_row_to_jsonable(updated))

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

    async with lifeswitch_meal_plans_repository(req) as repository:
        owner = await repository.active_plan_owner(meal_plan_id=mpid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal_plan not found or inactive")
        owner = require_actor_matches_owner(req, str(owner))
        mfid = None
        fid = None
        sid = None
        if my_food_id:
            mfid = _as_uuid(my_food_id, "my_food_id")
            if not await repository.owned_food_is_active(my_food_id=mfid, owner_user_id=owner):
                raise HTTPException(status_code=404, detail="my_food not found or inactive")
            if use_serving:
                sid = _as_uuid(my_food_serving_id, "my_food_serving_id")
                if not await repository.serving_is_active(serving_id=sid, my_food_id=mfid):
                    raise HTTPException(status_code=400, detail="active serving not found for this food")
        if (not mfid) and food_id:
            fid = _as_uuid(food_id, "food_id")
            if not await repository.catalog_food_is_approved(food_id=fid):
                raise HTTPException(status_code=400, detail="food_id is not approved/public")
        row = await repository.create_item(
            meal_plan_id=mpid,
            meal_label=meal_label,
            sort_order=sort_order,
            my_food_id=mfid,
            food_id=fid,
            qty_g=qty_g,
            serving_id=sid,
            qty_servings=qty_servings,
            notes=notes,
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})


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

    def authorize_item(item):
        require_actor_matches_owner(req, str(item["owner_user_id"]))
        if item["food_id"] is not None and use_serving:
            raise HTTPException(status_code=400, detail="legacy catalog foods support grams only")

    try:
        async with lifeswitch_meal_plans_repository(req) as repository:
            row = await repository.update_item(
                meal_plan_id=mpid,
                item_id=item_id,
                meal_label=meal_label,
                qty_g=qty_g,
                raw_serving_id=my_food_serving_id,
                qty_servings=qty_servings,
                use_grams=use_grams,
                use_serving=use_serving,
                authorize_item=authorize_item,
                parse_serving_id=lambda value: _as_uuid(value, "my_food_serving_id"),
            )
    except MealPlansRepositoryError as error:
        details = {
            "item_not_found": "meal plan item not found",
            "serving_not_found": "active serving not found for this food",
        }
        raise HTTPException(status_code=404 if error.code == "item_not_found" else 400, detail=details.get(error.code, "meal plan repository failure"))
    return JSONResponse(_row_to_jsonable(row))


@router.delete("/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}")
async def delete_meal_plan_item(meal_plan_id: str, meal_plan_item_id: str, req: Request):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    item_id = _as_uuid(meal_plan_item_id, "meal_plan_item_id")
    async with lifeswitch_meal_plans_repository(req) as repository:
        owner = await repository.plan_owner(meal_plan_id=mpid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal plan not found")
        require_actor_matches_owner(req, str(owner))
        row = await repository.delete_item(meal_plan_id=mpid, item_id=item_id)
    if not row:
        raise HTTPException(status_code=404, detail="meal plan item not found")
    return JSONResponse({"deleted": str(row["meal_plan_item_id"])})


@router.get("/meal_plans/{meal_plan_id}/items")
async def list_items(meal_plan_id: str, req: Request):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    async with lifeswitch_meal_plans_repository(req) as repository:
        owner = await repository.plan_owner(meal_plan_id=mpid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal_plan not found")
        require_actor_matches_owner(req, str(owner))
        rows = await repository.list_items(meal_plan_id=mpid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])


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
    async with lifeswitch_foods_repository(req) as repository:
        rows = await repository.list_foods(
            owner_user_id=owner,
            query=q,
            include_inactive=include_inactive != 0,
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

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

    async with lifeswitch_foods_repository(req) as repository:
        src = await repository.get_public_catalog_food(food_id=fid)
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

        row = await repository.upsert_catalog_food(
            owner_user_id=owner,
            display_name=dn,
            source_display_name=source_display_name,
            brand=br,
            variant=(variant or None),
            source_food_id=fid,
            source=(src.get("source") if hasattr(src, "get") else src["source"]),
            source_id=src_id_txt,
            barcode=bc,
            basis=(src.get("basis") if hasattr(src, "get") else src["basis"]),
            kcal=(src.get("kcal") if hasattr(src, "get") else src["kcal"]),
            protein_g=(src.get("protein_g") if hasattr(src, "get") else src["protein_g"]),
            carbs_g=(src.get("carbs_g") if hasattr(src, "get") else src["carbs_g"]),
            fat_g=(src.get("fat_g") if hasattr(src, "get") else src["fat_g"]),
            fiber_g=(src.get("fiber_g") if hasattr(src, "get") else src["fiber_g"]),
            sugar_g=(src.get("sugar_g") if hasattr(src, "get") else src["sugar_g"]),
            sodium_mg=(src.get("sodium_mg") if hasattr(src, "get") else src["sodium_mg"]),
        )
        updated = await repository.get_food_with_preferred(my_food_id=row["my_food_id"])
    return JSONResponse(_row_to_jsonable(updated))

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

    async with lifeswitch_foods_repository(req) as repository:
        async with repository.transaction():
            current = await repository.lock_food(my_food_id=fid)
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
                serving_ok = await repository.active_serving_belongs_to_food(
                    serving_id=preferred_serving_id,
                    my_food_id=fid,
                )
                if not serving_ok:
                    raise HTTPException(status_code=400, detail="preferred serving is not active for this food")

            def numeric_value(name: str, payload_value):
                return payload_value if name in fields else current[name]

            row = await repository.update_food_record(
                my_food_id=fid,
                values=(
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
                ),
            )
            if not row:
                raise HTTPException(status_code=404, detail="my_food not found")

            updated = await repository.get_food_with_preferred(my_food_id=fid)
            return JSONResponse(_row_to_jsonable(updated))

@router.post("/my_foods/{my_food_id}/deactivate")
async def deactivate_my_food(my_food_id: str, req: Request):
    fid = _as_uuid(my_food_id, "my_food_id")
    async with lifeswitch_foods_repository(req) as repository:
        owner = await repository.food_owner(my_food_id=fid)
        if not owner:
            raise HTTPException(status_code=404, detail="my_food not found")
        require_actor_matches_owner(req, str(owner))

        row = await repository.deactivate_food(my_food_id=fid)
        if not row:
            raise HTTPException(status_code=404, detail="my_food not found")
    return JSONResponse(_row_to_jsonable(row))

# ----------------------------
# My Food servings (presets)
# ----------------------------

@router.get("/my_foods/{my_food_id}/servings")
async def list_my_food_servings(my_food_id: str, req: Request):
    fid = _as_uuid(my_food_id, "my_food_id")
    async with lifeswitch_foods_repository(req) as repository:
        owner = await repository.food_owner(my_food_id=fid)
        if not owner:
            raise HTTPException(status_code=404, detail="my_food not found")
        require_actor_matches_owner(req, str(owner))

        rows = await repository.list_active_servings(my_food_id=fid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])

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

    async with lifeswitch_foods_repository(req) as repository:
        food = await repository.food_owner_and_active(my_food_id=fid)
        if not food or food["is_active"] is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")
        require_actor_matches_owner(req, str(food["owner_user_id"]))

        row = await repository.find_serving_record_by_name(my_food_id=fid, name=nm)
        if row:
            sid = row["my_food_serving_id"]
            await repository.update_manual_serving(serving_id=sid, name=nm, grams=grams)

            if is_default == 1 and row["is_default"] is not True:
                await repository.clear_default_servings(my_food_id=fid)
                await repository.set_default_serving(serving_id=sid)

            if is_default == 1:
                await repository.set_preferred_serving(my_food_id=fid, serving_id=sid)

            row2 = await repository.get_serving(serving_id=sid)
            return JSONResponse(_row_to_jsonable(row2 or row))

        if is_default == 1:
            await repository.clear_default_servings(my_food_id=fid)

        row = await repository.create_manual_serving(
            my_food_id=fid,
            name=nm,
            grams=grams,
            is_default=(is_default == 1),
        )
        if row and is_default == 1:
            await repository.set_preferred_serving(
                my_food_id=fid,
                serving_id=row["my_food_serving_id"],
            )

        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})

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

    try:
        async with lifeswitch_foods_repository(req) as repository:
            async with repository.transaction(translate_unique_violation=True):
                current = await repository.lock_serving(serving_id=sid, my_food_id=fid)
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
                row = await repository.update_serving_record(
                    serving_id=sid,
                    my_food_id=fid,
                    name=name,
                    grams=grams,
                    is_active=is_active,
                    manually_changed=manually_changed,
                )

                currently_preferred = str(current["preferred_serving_id"] or "") == str(sid)
                if set_preferred is True:
                    await repository.clear_other_default_servings(
                        my_food_id=fid,
                        serving_id=sid,
                    )
                    await repository.set_default_serving(serving_id=sid)
                    await repository.set_preferred_serving(my_food_id=fid, serving_id=sid)
                elif currently_preferred and (is_active is not True or set_preferred is False):
                    await repository.set_grams_preference(
                        my_food_id=fid,
                        preferred_quantity=grams,
                    )
                    await repository.clear_serving_default(serving_id=sid)

                updated = await repository.get_serving(serving_id=sid)
                return JSONResponse(_row_to_jsonable(updated or row))
    except FoodsRepositoryError as error:
        if error.code == "serving_name_conflict":
            raise HTTPException(status_code=409, detail="serving name already exists for this food") from error
        raise

# ----------------------------
# My Food overrides (alias + default grams + sort)
# ----------------------------

@router.get("/my_food_overrides")
async def list_my_food_overrides(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_foods_repository(req) as repository:
        rows = await repository.list_overrides(owner_user_id=owner)
    return JSONResponse([_row_to_jsonable(row) for row in rows])

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

    async with lifeswitch_foods_repository(req) as repository:
        ok = await repository.food_is_active(my_food_id=fid)
        if ok is not True:
            raise HTTPException(status_code=404, detail="my_food not found or inactive")

        row = await repository.upsert_override(
            owner_user_id=owner,
            my_food_id=fid,
            alias=al,
            default_grams=default_grams,
            sort_order=sort_order,
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})

@router.delete("/my_food_overrides")
async def delete_my_food_override(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    my_food_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    fid = _as_uuid(my_food_id, "my_food_id")
    async with lifeswitch_foods_repository(req) as repository:
        row = await repository.delete_override(owner_user_id=owner, my_food_id=fid)
        if not row:
            raise HTTPException(status_code=404, detail="override not found")
    return JSONResponse(_row_to_jsonable(row))
