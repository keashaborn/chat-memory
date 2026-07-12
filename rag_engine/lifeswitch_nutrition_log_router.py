from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

SCHEMA = os.getenv("LIFESWITCH_NUTRITION_SCHEMA", "lifeswitch_nutrition")
PEOPLE_SCHEMA = os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")


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


async def _has_people_permission(conn, grantor_user_id: str, grantee_user_id: str, scope: str) -> bool:
    row = await conn.fetchrow(
        f"""
        select rp.relationship_permission_id
        from {PEOPLE_SCHEMA}.relationship_permission rp
        join {PEOPLE_SCHEMA}.relationship r
          on r.relationship_id=rp.relationship_id
        where rp.grantor_user_id=$1::uuid
          and rp.grantee_user_id=$2::uuid
          and rp.permission_scope=$3
          and rp.is_enabled=true
          and r.status='accepted'
        limit 1
        """,
        grantor_user_id,
        grantee_user_id,
        scope,
    )
    return bool(row)


async def _resolve_nutrition_view_target(conn, viewer_user_id: str, target_user_id: str = "") -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        allowed = await _has_people_permission(conn, target, viewer, "nutrition:view")
        if not allowed:
            raise HTTPException(status_code=403, detail="nutrition:view permission required")

    return target, delegated


def _parse_day(day: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(day))
    except Exception:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")


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
    owner = require_actor_matches_owner(req, owner_user_id)
    d = _parse_day(day)

    if (meal_id is None) == (my_food_id is None):
        raise HTTPException(status_code=400, detail="provide exactly one: meal_id or my_food_id")

    mid = _as_uuid(meal_id, "meal_id") if meal_id is not None else None
    fid = _as_uuid(my_food_id, "my_food_id") if my_food_id is not None else None

    use_grams = qty_g is not None
    use_serving = my_food_serving_id is not None or qty_servings is not None

    if mid is not None and use_serving:
        raise HTTPException(
            status_code=400,
            detail="serving quantity is only valid for my_food_id",
        )

    if fid is not None:
        if use_grams and use_serving:
            raise HTTPException(
                status_code=400,
                detail="provide qty_g OR (my_food_serving_id + qty_servings), not both",
            )
        if not use_grams and not use_serving:
            raise HTTPException(
                status_code=400,
                detail="must provide qty_g OR (my_food_serving_id + qty_servings)",
            )
        if use_serving and (my_food_serving_id is None or qty_servings is None):
            raise HTTPException(
                status_code=400,
                detail="serving mode requires my_food_serving_id and qty_servings",
            )

    sid = (
        _as_uuid(my_food_serving_id, "my_food_serving_id")
        if my_food_serving_id is not None
        else None
    )

    conn = await _db()
    try:
        if mid is not None:
            ok = await conn.fetchval(
                f"""
                select is_active
                from {SCHEMA}.meal
                where meal_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                mid,
                owner,
            )
            if ok is not True:
                raise HTTPException(status_code=404, detail="meal not found or inactive")

        resolved_qty_g = qty_g

        if fid is not None:
            ok = await conn.fetchval(
                f"""
                select is_active
                from {SCHEMA}.my_food
                where my_food_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                fid,
                owner,
            )
            if ok is not True:
                raise HTTPException(status_code=404, detail="my_food not found or inactive")

            if sid is not None:
                serving_grams = await conn.fetchval(
                    f"""
                    select grams
                    from {SCHEMA}.my_food_serving
                    where my_food_serving_id=$1::uuid
                      and my_food_id=$2::uuid
                    """,
                    sid,
                    fid,
                )
                if serving_grams is None:
                    raise HTTPException(
                        status_code=404,
                        detail="serving not found for this my_food_id",
                    )
                resolved_qty_g = float(serving_grams) * float(qty_servings)

        day_row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.nutrition_day (owner_user_id, day)
            values ($1::uuid, $2::date)
            on conflict (owner_user_id, day) do update
              set updated_at=now()
            returning
              nutrition_day_id, owner_user_id, day, notes,
              created_at, updated_at
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
              (
                nutrition_day_id, meal_id, my_food_id, qty_g,
                my_food_serving_id, qty_servings,
                sort_order, notes
              )
            values
              ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6, $7, $8)
            returning
              nutrition_entry_id, nutrition_day_id, meal_id, my_food_id,
              qty_g, my_food_serving_id, qty_servings,
              sort_order, notes, created_at, updated_at
            """,
            ndid,
            mid,
            fid,
            resolved_qty_g,
            sid,
            qty_servings,
            sort_order,
            notes,
        )

        return JSONResponse({
            "day": _row_to_jsonable(day_row),
            "entry": _row_to_jsonable(entry_row) if entry_row else None,
        })
    finally:
        await conn.close()


@router.patch("/log/entry")
async def update_log_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    nutrition_entry_id: str = Query(..., min_length=1),
    qty_g: float = Query(..., gt=0),
    sort_order: int | None = Query(None),
    notes: str | None = Query(None, max_length=500),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    eid = _as_uuid(nutrition_entry_id, "nutrition_entry_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.nutrition_entry e
            set
              qty_g = $3,
              my_food_serving_id = null,
              qty_servings = null,
              sort_order = coalesce($4, e.sort_order),
              notes = coalesce($5, e.notes),
              updated_at = now()
            from {SCHEMA}.nutrition_day d
            where e.nutrition_day_id = d.nutrition_day_id
              and d.owner_user_id = $1::uuid
              and e.nutrition_entry_id = $2::uuid
            returning
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at
            """,
            owner,
            eid,
            qty_g,
            sort_order,
            notes,
        )
        if not row:
            raise HTTPException(status_code=404, detail="nutrition_entry not found (or not owned by user)")
        return JSONResponse({"entry": _row_to_jsonable(row)})
    finally:
        await conn.close()

@router.delete("/log/entry")
async def delete_log_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    nutrition_entry_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    eid = _as_uuid(nutrition_entry_id, "nutrition_entry_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            delete from {SCHEMA}.nutrition_entry e
            using {SCHEMA}.nutrition_day d
            where e.nutrition_day_id = d.nutrition_day_id
              and d.owner_user_id = $1::uuid
              and e.nutrition_entry_id = $2::uuid
            returning
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at
            """,
            owner,
            eid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="nutrition_entry not found (or not owned by user)")
        return JSONResponse({"deleted": _row_to_jsonable(row)})
    finally:
        await conn.close()


@router.get("/log/day")
async def get_log_day(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    target_user_id: str = Query("", max_length=80),
):
    viewer = require_actor_matches_owner(req, owner_user_id)
    d = _parse_day(day)

    conn = await _db()
    try:
        owner, delegated = await _resolve_nutrition_view_target(conn, viewer, target_user_id)

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
            return JSONResponse({
                "day": None,
                "entries": [],
                "_target_user_id": owner,
                "_delegated_view": delegated,
            })

        ndid = str(day_row["nutrition_day_id"])

        rows = await conn.fetch(
            f"""
            select
              e.nutrition_entry_id, e.nutrition_day_id, e.meal_id, e.my_food_id, e.qty_g, e.my_food_serving_id, e.qty_servings, e.sort_order, e.notes,
              e.created_at, e.updated_at,

            coalesce(m.name, f.display_name) as label,
            m.meal_type as meal_type,
            s.name as serving_name,
            s.grams as serving_grams,

            -- Food identity fields (so frontend can render like FoodsPage)
            f.brand as food_brand,
            f.variant as food_variant,
            f.source_type as food_source_type,
            f.source_id as food_source_id,

            -- per 100g
            f.kcal as food_kcal_100g,
            f.protein_g as food_protein_100g,
            f.carbs_g as food_carbs_100g,
            f.fat_g as food_fat_100g,

              mt.kcal as meal_kcal, mt.protein_g as meal_protein, mt.carbs_g as meal_carbs, mt.fat_g as meal_fat

            from {SCHEMA}.nutrition_entry e
            left join {SCHEMA}.meal m on m.meal_id = e.meal_id
            left join {SCHEMA}.my_food f on f.my_food_id = e.my_food_id
            left join {SCHEMA}.my_food_serving s
              on s.my_food_serving_id = e.my_food_serving_id
             and s.my_food_id = e.my_food_id

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

        return JSONResponse({
            "day": _row_to_jsonable(day_row),
            "entries": [_row_to_jsonable(r) for r in rows],
            "_target_user_id": owner,
            "_delegated_view": delegated,
        })
    finally:
        await conn.close()
