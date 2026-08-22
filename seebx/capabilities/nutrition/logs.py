from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
from fastapi import APIRouter, HTTPException, Query, Request
from seebx.core.identity import require_actor
from seebx.adapters.lifeswitch_nutrition_log_postgres import (
    LifeSwitchNutritionLogRepository,
    NutritionLogBatchEntryWrite,
    NutritionLogEntryUpdate,
    NutritionLogEntryWrite,
    NutritionLogRepositoryError,
    lifeswitch_nutrition_log_repository,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()

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


async def _resolve_nutrition_view_target(
    repository: LifeSwitchNutritionLogRepository,
    viewer_user_id: str,
    target_user_id: str = "",
) -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        if os.getenv("LIFESWITCH_DELEGATED_READS_ENABLED", "0") != "1":
            raise HTTPException(status_code=403, detail="delegated_access_disabled")
        allowed = await repository.has_people_permission(
            grantor_user_id=target,
            grantee_user_id=viewer,
            scope="nutrition:view",
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="nutrition:view permission required")

    return target, delegated


def _parse_day(day: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(day))
    except Exception:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")


def _entry_totals(row) -> dict[str, float]:
    if row["meal_id"] is not None:
        return {
            "kcal": float(row["meal_kcal"] or 0),
            "protein_g": float(row["meal_protein"] or 0),
            "carbs_g": float(row["meal_carbs"] or 0),
            "fat_g": float(row["meal_fat"] or 0),
        }

    grams = float(row["qty_g"] or 0)
    return {
        "kcal": float(row["food_kcal_100g"] or 0) * grams / 100.0,
        "protein_g": float(row["food_protein_100g"] or 0) * grams / 100.0,
        "carbs_g": float(row["food_carbs_100g"] or 0) * grams / 100.0,
        "fat_g": float(row["food_fat_100g"] or 0) * grams / 100.0,
    }


class NutritionBatchItem(BaseModel):
    my_food_id: str
    qty_g: float | None = Field(None, gt=0)
    my_food_serving_id: str | None = None
    qty_servings: float | None = Field(None, gt=0)
    sort_order: int = 1
    notes: str | None = Field(None, max_length=500)


class NutritionBatchCreate(BaseModel):
    day: str = Field(..., min_length=10, max_length=10)
    items: list[NutritionBatchItem] = Field(..., min_length=1, max_length=100)


class NutritionDayCompletionUpdate(BaseModel):
    completed: bool


def _raise_repository_error(error: NutritionLogRepositoryError) -> None:
    static = {
        "meal_not_found_or_inactive": (404, "meal not found or inactive"),
        "food_not_found_or_inactive": (404, "my_food not found or inactive"),
        "serving_not_found_for_food": (404, "serving not found for this my_food_id"),
        "nutrition_day_create_failed": (500, "failed to create nutrition_day"),
        "entry_not_found": (404, "nutrition_entry not found (or not owned by user)"),
        "entry_not_single_food": (400, "only single-food entries support quantity editing"),
        "active_serving_not_found": (400, "active serving not found for this food"),
        "nutrition_day_not_found": (404, "nutrition day not found"),
        "nutrition_day_completion_conflict": (409, "nutrition day completion changed concurrently"),
    }
    if error.code == "batch_food_not_found_or_inactive":
        raise HTTPException(
            status_code=404,
            detail=f"items[{error.item_index}] food not found or inactive",
        )
    if error.code == "batch_serving_not_found_for_food":
        raise HTTPException(
            status_code=404,
            detail=f"items[{error.item_index}] active serving not found for this food",
        )
    status_code, detail = static.get(
        error.code,
        (500, "nutrition repository failure"),
    )
    raise HTTPException(status_code=status_code, detail=detail)


@router.post("/log/entry")
async def create_log_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    meal_id: str | None = Query(None),
    my_food_id: str | None = Query(None),
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
    sort_order: int = Query(0),
    notes: str | None = Query(None, max_length=500),
):
    owner = await require_actor(req, owner_user_id)
    parsed_day = _parse_day(day)

    if (meal_id is None) == (my_food_id is None):
        raise HTTPException(status_code=400, detail="provide exactly one: meal_id or my_food_id")

    parsed_meal_id = _as_uuid(meal_id, "meal_id") if meal_id is not None else None
    parsed_food_id = _as_uuid(my_food_id, "my_food_id") if my_food_id is not None else None
    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None

    if parsed_meal_id is not None and use_serving:
        raise HTTPException(status_code=400, detail="serving quantity is only valid for my_food_id")
    if parsed_food_id is not None:
        if use_grams and use_serving:
            raise HTTPException(status_code=400, detail="provide qty_g OR (my_food_serving_id + qty_servings), not both")
        if not use_grams and not use_serving:
            raise HTTPException(status_code=400, detail="must provide qty_g OR (my_food_serving_id + qty_servings)")
        if use_serving and (my_food_serving_id is None or qty_servings is None):
            raise HTTPException(status_code=400, detail="serving mode requires my_food_serving_id and qty_servings")

    parsed_serving_id = (
        _as_uuid(my_food_serving_id, "my_food_serving_id")
        if my_food_serving_id is not None
        else None
    )
    value = NutritionLogEntryWrite(
        day=parsed_day,
        meal_id=parsed_meal_id,
        my_food_id=parsed_food_id,
        qty_g=qty_g,
        my_food_serving_id=parsed_serving_id,
        qty_servings=qty_servings,
        sort_order=sort_order,
        notes=notes,
    )
    try:
        async with lifeswitch_nutrition_log_repository(req) as repository:
            result = await repository.create_entry(owner_user_id=owner, value=value)
    except NutritionLogRepositoryError as error:
        _raise_repository_error(error)

    return JSONResponse({
        "day": _row_to_jsonable(result.day_row),
        "entry": _row_to_jsonable(result.entry_row) if result.entry_row else None,
    })


@router.post("/log/entries/batch")
async def create_log_entries_batch(
    body: NutritionBatchCreate,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    """Create several food entries in one transaction or create none."""
    owner = await require_actor(req, owner_user_id)
    day = _parse_day(body.day)
    prepared = []
    for index, item in enumerate(body.items):
        food_id = _as_uuid(item.my_food_id, f"items[{index}].my_food_id")
        use_grams = item.qty_g is not None
        use_serving = item.my_food_serving_id is not None or item.qty_servings is not None
        if use_grams and use_serving:
            raise HTTPException(status_code=400, detail=f"items[{index}] must provide qty_g OR serving quantity, not both")
        if not use_grams and not use_serving:
            raise HTTPException(status_code=400, detail=f"items[{index}] must provide qty_g OR serving quantity")
        if use_serving and (item.my_food_serving_id is None or item.qty_servings is None):
            raise HTTPException(status_code=400, detail=f"items[{index}] serving mode requires my_food_serving_id and qty_servings")
        serving_id = (
            _as_uuid(item.my_food_serving_id, f"items[{index}].my_food_serving_id")
            if item.my_food_serving_id is not None
            else None
        )
        prepared.append(NutritionLogBatchEntryWrite(
            my_food_id=food_id,
            qty_g=item.qty_g,
            my_food_serving_id=serving_id,
            qty_servings=item.qty_servings,
            sort_order=item.sort_order,
            notes=item.notes,
        ))

    try:
        async with lifeswitch_nutrition_log_repository(req) as repository:
            result = await repository.create_entries_batch(
                owner_user_id=owner,
                day=day,
                items=tuple(prepared),
            )
    except NutritionLogRepositoryError as error:
        _raise_repository_error(error)

    return JSONResponse({
        "day": _row_to_jsonable(result.day_row),
        "entries": [_row_to_jsonable(row) for row in result.entry_rows],
    })


@router.patch("/log/entry")
async def update_log_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    nutrition_entry_id: str = Query(..., min_length=1),
    qty_g: float | None = Query(None, gt=0),
    my_food_serving_id: str | None = Query(None, min_length=1),
    qty_servings: float | None = Query(None, gt=0),
    sort_order: int | None = Query(None),
    notes: str | None = Query(None, max_length=500),
):
    owner = await require_actor(req, owner_user_id)
    entry_id = _as_uuid(nutrition_entry_id, "nutrition_entry_id")
    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None
    if use_grams and use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity, not both")
    if not use_grams and not use_serving:
        raise HTTPException(status_code=400, detail="provide qty_g OR serving quantity")
    if use_serving and (my_food_serving_id is None or qty_servings is None):
        raise HTTPException(status_code=400, detail="serving mode requires my_food_serving_id and qty_servings")

    serving_id = (
        _as_uuid(my_food_serving_id, "my_food_serving_id")
        if my_food_serving_id is not None
        else None
    )
    value = NutritionLogEntryUpdate(
        nutrition_entry_id=entry_id,
        qty_g=qty_g,
        my_food_serving_id=serving_id,
        qty_servings=qty_servings,
        sort_order=sort_order,
        notes=notes,
    )
    try:
        async with lifeswitch_nutrition_log_repository(req) as repository:
            row = await repository.update_entry(owner_user_id=owner, value=value)
    except NutritionLogRepositoryError as error:
        _raise_repository_error(error)
    return JSONResponse({"entry": _row_to_jsonable(row)})


@router.delete("/log/entry")
async def delete_log_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    nutrition_entry_id: str = Query(..., min_length=1),
):
    owner = await require_actor(req, owner_user_id)
    entry_id = _as_uuid(nutrition_entry_id, "nutrition_entry_id")
    async with lifeswitch_nutrition_log_repository(req) as repository:
        row = await repository.delete_entry(
            owner_user_id=owner,
            nutrition_entry_id=entry_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="nutrition_entry not found (or not owned by user)")
    return JSONResponse({"deleted": _row_to_jsonable(row)})


@router.get("/log/range")
async def get_log_range(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    start_day: str = Query(..., min_length=10, max_length=10),
    end_day: str = Query(..., min_length=10, max_length=10),
    include_entries: int = Query(0, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    viewer = await require_actor(req, owner_user_id)
    start = _parse_day(start_day)
    end = _parse_day(end_day)
    if start > end:
        raise HTTPException(status_code=400, detail="start_day must be on or before end_day")
    if (end - start).days > 365:
        raise HTTPException(status_code=400, detail="date range cannot exceed 366 days")

    async with lifeswitch_nutrition_log_repository(req) as repository:
        owner, delegated = await _resolve_nutrition_view_target(
            repository,
            viewer,
            target_user_id,
        )
        snapshot = await repository.read_range(
            owner_user_id=owner,
            start_day=start,
            end_day=end,
        )

    entries_by_day: dict[str, list[dict]] = {}
    totals_by_day: dict[str, dict[str, float]] = {}
    for row in snapshot.entry_rows:
        day_key = row["nutrition_day_date"].isoformat()
        totals = totals_by_day.setdefault(
            day_key,
            {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0},
        )
        entry_totals = _entry_totals(row)
        for key, value in entry_totals.items():
            totals[key] += value

        if include_entries:
            entry = _row_to_jsonable(row)
            entry.pop("nutrition_day_date", None)
            entries_by_day.setdefault(day_key, []).append(entry)

    days = []
    for day_row in snapshot.day_rows:
        day_key = day_row["day"].isoformat()
        days.append({
            "day": _row_to_jsonable(day_row),
            "entries": entries_by_day.get(day_key, []) if include_entries else [],
            "totals": totals_by_day.get(
                day_key,
                {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0},
            ),
        })

    return JSONResponse({
        "start_day": start.isoformat(),
        "end_day": end.isoformat(),
        "days": days,
        "_target_user_id": owner,
        "_delegated_view": delegated,
    })


@router.get("/log/day")
async def get_log_day(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    target_user_id: str = Query("", max_length=80),
):
    viewer = await require_actor(req, owner_user_id)
    parsed_day = _parse_day(day)

    async with lifeswitch_nutrition_log_repository(req) as repository:
        owner, delegated = await _resolve_nutrition_view_target(
            repository,
            viewer,
            target_user_id,
        )
        snapshot = await repository.read_day(
            owner_user_id=owner,
            day=parsed_day,
        )

    if not snapshot.day_row:
        return JSONResponse({
            "day": None,
            "entries": [],
            "_target_user_id": owner,
            "_delegated_view": delegated,
        })

    return JSONResponse({
        "day": _row_to_jsonable(snapshot.day_row),
        "entries": [_row_to_jsonable(row) for row in snapshot.entry_rows],
        "_target_user_id": owner,
        "_delegated_view": delegated,
    })


@router.patch("/log/day")
async def set_log_day_completion(
    body: NutritionDayCompletionUpdate,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
):
    owner = await require_actor(req, owner_user_id)
    parsed_day = _parse_day(day)
    try:
        async with lifeswitch_nutrition_log_repository(req) as repository:
            result = await repository.set_day_completion(
                owner_user_id=owner,
                day=parsed_day,
                completed=body.completed,
            )
    except NutritionLogRepositoryError as error:
        _raise_repository_error(error)
    return JSONResponse({
        "day": _row_to_jsonable(result.day_row),
        "changed": result.changed,
    })
