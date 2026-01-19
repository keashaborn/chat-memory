from __future__ import annotations

import os
import asyncpg
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
import uuid
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

DSN = os.getenv("POSTGRES_DSN")
if not DSN:
    raise RuntimeError("POSTGRES_DSN is not set for Brains; catalog endpoints require DB access")
CATALOG_SCHEMA = os.getenv("CATALOG_SCHEMA", "catalog_dev")

async def _db():
    return await asyncpg.connect(DSN)

@router.get("/exercises/search")
async def search_exercises(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    locale: str = Query("en", min_length=2, max_length=10),
):
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select exercise_id, display_name, kind, modality, score, matched_text, matched_source, brand_name, model_name
            from {CATALOG_SCHEMA}.search_exercises($1::text, $2::int, $3::text)
            """,
            q,
            limit,
            locale,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.get("/foods/search")
async def search_foods(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    locale: str = Query("en", min_length=2, max_length=10),
):
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select food_id, display_name, brand, barcode, source, basis, kcal, protein_g, carbs_g, fat_g, score, matched_text, matched_source
            from {CATALOG_SCHEMA}.search_foods($1::text, $2::int, $3::text)
            """,
            q,
            limit,
            locale,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()
