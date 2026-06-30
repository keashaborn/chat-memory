from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
import asyncpg
from fastapi import APIRouter, HTTPException, Request
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional

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


def _parse_day(day: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(day))
    except Exception:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")


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


class NutritionEntryIn(BaseModel):
    my_food_id: str = Field(..., min_length=1)
    qty_g: float = Field(..., gt=0)
    sort_order: int = 0
    notes: Optional[str] = Field(None, max_length=500)


class LogBatchIn(BaseModel):
    owner_user_id: str = Field(..., min_length=1)
    day: str = Field(..., min_length=10, max_length=10)
    entries: List[NutritionEntryIn] = Field(..., min_length=1)
    group_notes: Optional[str] = Field(None, max_length=500)


@router.post("/log/entries")
async def create_log_entries_batch(payload: LogBatchIn, req: Request):
    owner = require_actor_matches_owner(req, payload.owner_user_id)
    d = _parse_day(payload.day)

    conn = await _db()
    try:
        # ensure day exists (upsert)
        day_row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.nutrition_day (owner_user_id, day)
            values ($1::uuid, $2::date)
            on conflict (owner_user_id, day) do update
              set updated_at=now()
            returning nutrition_day_id, owner_user_id, day, notes, created_at, updated_at
            """,
            owner,
            d,
        )
        if not day_row:
            raise HTTPException(status_code=500, detail="failed to create nutrition_day")

        ndid = str(day_row["nutrition_day_id"])

        # validate foods exist+active
        # (batch check)
        fids = [_as_uuid(e.my_food_id, "my_food_id") for e in payload.entries]
        rows = await conn.fetch(
            f"""
            select my_food_id, is_active
            from {SCHEMA}.my_food
            where my_food_id = any($1::uuid[])
            """,
            fids,
        )
        active = {str(r["my_food_id"]): (r["is_active"] is True) for r in rows}
        for fid in fids:
            if active.get(str(fid)) is not True:
                raise HTTPException(status_code=404, detail=f"my_food not found or inactive: {fid}")

        # insert entries
        out = []
        for e in payload.entries:
            fid = _as_uuid(e.my_food_id, "my_food_id")
            notes = e.notes
            if payload.group_notes:
                notes = (notes + " | " if notes else "") + payload.group_notes

            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.nutrition_entry
                  (nutrition_day_id, meal_id, my_food_id, qty_g, sort_order, notes)
                values
                  ($1::uuid, null, $2::uuid, $3, $4, $5)
                returning nutrition_entry_id, nutrition_day_id, meal_id, my_food_id, qty_g, sort_order, notes, created_at, updated_at
                """,
                ndid,
                fid,
                e.qty_g,
                e.sort_order,
                notes,
            )
            out.append(_row_to_jsonable(row) if row else None)

        return JSONResponse({"day": _row_to_jsonable(day_row), "entries": out})
    finally:
        await conn.close()
