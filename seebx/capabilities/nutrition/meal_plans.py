from __future__ import annotations

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_meal_plans_postgres import (
    MealPlansRepositoryError,
    lifeswitch_meal_plans_repository,
)
from seebx.core.ownership import require_actor_matches_owner

from .common import _as_uuid, _row_to_jsonable


async def list_meal_plans(req: Request, owner_user_id: str = Query(...)):
    uid = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_meal_plans_repository(req) as repository:
        rows = await repository.list_plans(owner_user_id=uid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])

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

async def list_items(meal_plan_id: str, req: Request):
    mpid = _as_uuid(meal_plan_id, "meal_plan_id")
    async with lifeswitch_meal_plans_repository(req) as repository:
        owner = await repository.plan_owner(meal_plan_id=mpid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal_plan not found")
        require_actor_matches_owner(req, str(owner))
        rows = await repository.list_items(meal_plan_id=mpid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])
