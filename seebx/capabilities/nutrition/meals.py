from __future__ import annotations

import datetime as _dt
import decimal
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_meals_postgres import (
    MealsRepositoryError,
    lifeswitch_meals_repository,
)
from seebx.core.identity import (
    require_actor,
    require_request_actor,
    require_verified_actor_matches_owner,
)


router = APIRouter()


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _json_safe(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _row_to_jsonable(r):
    d = dict(r)
    return {k: _json_safe(v) for k, v in d.items()}


@router.get("/meals")
async def list_meals(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = await require_actor(req, owner_user_id)
    async with lifeswitch_meals_repository(req) as repository:
        rows = await repository.list_meals(
            owner_user_id=owner,
            include_inactive=include_inactive != 0,
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])


@router.post("/meals/create")
async def create_meal(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    name: str = Query(..., min_length=1, max_length=120),
    meal_type: str = Query("other"),
):
    owner = await require_actor(req, owner_user_id)
    if meal_type not in ("breakfast", "lunch", "dinner", "snack", "other"):
        raise HTTPException(status_code=400, detail="meal_type must be breakfast|lunch|dinner|snack|other")
    async with lifeswitch_meals_repository(req) as repository:
        row = await repository.create_meal(
            owner_user_id=owner,
            name=name.strip(),
            meal_type=meal_type,
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})


@router.post("/meals/{meal_id}/deactivate")
async def deactivate_meal(
    meal_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    mid = _as_uuid(meal_id, "meal_id")
    owner = await require_actor(req, owner_user_id)
    async with lifeswitch_meals_repository(req) as repository:
        row = await repository.deactivate_meal(meal_id=mid, owner_user_id=owner)
    if not row:
        raise HTTPException(status_code=404, detail="meal not found")
    return JSONResponse(_row_to_jsonable(row))


@router.post("/meals/{meal_id}/items/add")
async def add_meal_item(
    meal_id: str,
    req: Request,
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
    use_serving = my_food_serving_id is not None or qty_servings is not None
    if use_grams and use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR (my_food_serving_id + qty_servings), not both")
    if not use_grams and not use_serving:
        raise HTTPException(status_code=400, detail="must provide qty_g OR (my_food_serving_id + qty_servings)")
    if use_serving and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="servings mode requires my_food_serving_id and qty_servings")
    sid = _as_uuid(my_food_serving_id, "my_food_serving_id") if my_food_serving_id else None

    async with lifeswitch_meals_repository(req) as repository:
        owner = await repository.active_meal_owner(meal_id=mid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal not found or inactive")
        owner = await require_actor(req, str(owner))
        if not await repository.food_is_active(my_food_id=fid, owner_user_id=owner):
            raise HTTPException(status_code=404, detail="my_food not found or inactive")
        if sid and not await repository.serving_is_active(serving_id=sid, my_food_id=fid):
            raise HTTPException(status_code=404, detail="active serving not found for this my_food_id")
        row = await repository.create_item(
            meal_id=mid,
            my_food_id=fid,
            qty_g=qty_g,
            serving_id=sid,
            qty_servings=qty_servings,
            sort_order=sort_order,
            notes=notes,
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})


@router.patch("/meals/{meal_id}/items/{meal_item_id}")
async def update_meal_item(
    meal_id: str,
    meal_item_id: str,
    req: Request,
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
):
    """Replace an item's quantity while preserving its food and sort order."""
    mid = _as_uuid(meal_id, "meal_id")
    iid = _as_uuid(meal_item_id, "meal_item_id")
    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None
    if use_grams and use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR (my_food_serving_id + qty_servings), not both")
    if not use_grams and not use_serving:
        raise HTTPException(status_code=400, detail="must provide qty_g OR (my_food_serving_id + qty_servings)")
    if use_serving and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="servings mode requires my_food_serving_id and qty_servings")
    sid = _as_uuid(my_food_serving_id, "my_food_serving_id") if my_food_serving_id else None
    verified_actor = await require_request_actor(req)
    try:
        async with lifeswitch_meals_repository(req) as repository:
            row = await repository.update_item(
                meal_id=mid,
                meal_item_id=iid,
                qty_g=qty_g,
                serving_id=sid,
                qty_servings=qty_servings,
                authorize_owner=lambda owner: require_verified_actor_matches_owner(
                    verified_actor, owner
                ),
            )
    except MealsRepositoryError as error:
        details = {
            "meal_item_not_found_or_inactive": "meal item not found or meal inactive",
            "serving_not_found_for_item": "active serving not found for this meal item's food",
        }
        raise HTTPException(status_code=404, detail=details.get(error.code, "meal repository failure"))
    return JSONResponse(_row_to_jsonable(row))


@router.get("/meals/{meal_id}/items")
async def list_meal_items(meal_id: str, req: Request):
    mid = _as_uuid(meal_id, "meal_id")
    async with lifeswitch_meals_repository(req) as repository:
        owner = await repository.meal_owner(meal_id=mid)
        if not owner:
            raise HTTPException(status_code=404, detail="meal not found")
        await require_actor(req, str(owner))
        rows = await repository.list_items(meal_id=mid)
    return JSONResponse([_row_to_jsonable(row) for row in rows])


@router.post("/meals/{meal_id}/items/{meal_item_id}/delete")
async def delete_meal_item(
    meal_id: str,
    meal_item_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    owner = await require_actor(req, owner_user_id)
    mid = _as_uuid(meal_id, "meal_id")
    iid = _as_uuid(meal_item_id, "meal_item_id")
    async with lifeswitch_meals_repository(req) as repository:
        row = await repository.delete_item(
            meal_item_id=iid,
            meal_id=mid,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="meal_item not found (or not owned by user)")
    return JSONResponse(_row_to_jsonable(row))
