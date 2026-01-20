from __future__ import annotations

import os
import asyncio
import re
import json
import asyncpg
from fastapi import APIRouter, Query, HTTPException
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

BARCODE_RE = re.compile(r"^\d{8,14}$")

def _normalize_barcode(b: str) -> str:
    b = (b or "").strip()
    b = re.sub(r"\s+", "", b)
    if not BARCODE_RE.match(b):
        raise HTTPException(status_code=400, detail="barcode must be 8-14 digits")
    return b

DSN = os.getenv("POSTGRES_DSN")
if not DSN:
    raise RuntimeError("POSTGRES_DSN is not set for Brains; catalog endpoints require DB access")
CATALOG_SCHEMA = os.getenv("CATALOG_SCHEMA", "catalog_dev")
USDA_API_KEY = os.getenv("USDA_API_KEY")

HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "2"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
DEFAULT_PUBLIC_IMPORT = os.getenv('DEFAULT_PUBLIC_IMPORT', '0') == '1'

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
            select sf.food_id, sf.display_name, sf.brand, sf.barcode, sf.source, sf.basis, sf.kcal, sf.protein_g, sf.carbs_g, sf.fat_g, sf.score, sf.matched_text, sf.matched_source
            from {CATALOG_SCHEMA}.search_foods($1::text, $2::int, $3::text) sf
            join {CATALOG_SCHEMA}.food f on f.food_id = sf.food_id
            where f.is_public
            """,
            q,
            limit,
            locale,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/foods/by_barcode")
async def food_by_barcode(
    barcode: str = Query(..., min_length=8, max_length=32),
    refresh: int = Query(0, ge=0, le=1),
):
    bc = _normalize_barcode(barcode)
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            select food_id, display_name, brand, barcode, source, basis,
                   kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
            from {CATALOG_SCHEMA}.food
            where barcode = $1 and is_active
            limit 1
            """,
            bc,
        )
        if row and refresh == 0:
            return JSONResponse(_row_to_jsonable(row))

        import requests

        def _fetch():
            return requests.get(
                f"https://world.openfoodfacts.org/api/v2/product/{bc}.json",
                timeout=HTTP_TIMEOUT,
            )

        r = await asyncio.to_thread(_fetch)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="barcode not found (open_food_facts)")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"open_food_facts HTTP {r.status_code}")

        j = r.json() if r.content else {}
        product = (j or {}).get("product") or {}
        if not product:
            raise HTTPException(status_code=404, detail="barcode not found (no product)")

        name = (
            product.get("product_name")
            or product.get("product_name_en")
            or product.get("generic_name")
            or product.get("generic_name_en")
            or bc
        )
        name = str(name).strip() or bc

        brand = product.get("brands")
        brand = str(brand).strip() if brand else None

        nutr = product.get("nutriments") or {}

        def _num(k: str):
            v = nutr.get(k)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except Exception:
                return None

        kcal = _num("energy-kcal_100g")
        protein = _num("proteins_100g")
        carbs = _num("carbohydrates_100g")
        fat = _num("fat_100g")
        fiber = _num("fiber_100g")
        sugar = _num("sugars_100g")
        sodium_g = _num("sodium_100g")
        sodium_mg = sodium_g * 1000.0 if sodium_g is not None else None

        up = await conn.fetchrow(
            f"""
            insert into {CATALOG_SCHEMA}.food
              (display_name, brand, barcode, source, source_id, basis,
               kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               is_public, is_active, data)
            values
              ($1,$2,$3,'open_food_facts',$4,'per_100g',
               $5,$6,$7,$8,$9,$10,$11,
               $13::bool,true,$12::jsonb)
            on conflict (source, source_id) do update
              set display_name = excluded.display_name,
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
                  data = excluded.data,
                  is_active = true
            returning food_id, display_name, brand, barcode, source, basis,
                      kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
            """,
            name, brand, bc, bc,
            kcal, protein, carbs, fat, fiber, sugar, sodium_mg,
            json.dumps(j),
            DEFAULT_PUBLIC_IMPORT,
        )

        return JSONResponse(_row_to_jsonable(up))
    finally:
        await conn.close()


@router.get("/foods/usda/search")
async def usda_food_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
):
    if not USDA_API_KEY:
        raise HTTPException(status_code=500, detail="USDA_API_KEY not configured on server")

    import requests

    def _do():
        return requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"api_key": USDA_API_KEY, "query": q, "pageSize": limit},
            timeout=HTTP_TIMEOUT,
        )

    r = await asyncio.to_thread(_do)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"usda_fdc HTTP {r.status_code}")

    j = r.json() if r.content else {}
    foods = (j or {}).get("foods") or []

    out = []
    for f in foods:
        out.append({
            "fdc_id": f.get("fdcId"),
            "description": f.get("description"),
            "brand_owner": f.get("brandOwner"),
            "brand_name": f.get("brandName"),
            "gtin_upc": f.get("gtinUpc"),
            "data_type": f.get("dataType"),
            "published_date": f.get("publishedDate"),
            "score": f.get("score"),
        })

    return JSONResponse(out)

@router.post("/foods/usda/import")
async def usda_food_import(
    fdc_id: int = Query(..., ge=1),
):
    if not USDA_API_KEY:
        raise HTTPException(status_code=500, detail="USDA_API_KEY not configured on server")

    import requests

    def _do():
        return requests.get(
            f"https://api.nal.usda.gov/fdc/v1/food/{int(fdc_id)}",
            params={"api_key": USDA_API_KEY},
            timeout=HTTP_TIMEOUT,
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

    # Nutrients are in foodNutrients; values are per 100g for most items.
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

    # USDA nutrient numbers (common): Energy=208 (kcal), Protein=203, Carb=205, Fat=204, Fiber=291, Sugars=269, Sodium=307 (mg)
    kcal = _nutr_amount("208")
    protein = _nutr_amount("203")
    carbs = _nutr_amount("205")
    fat = _nutr_amount("204")
    fiber = _nutr_amount("291")
    sugar = _nutr_amount("269")
    sodium_mg = _nutr_amount("307")

    conn = await _db()
    try:
        up = await conn.fetchrow(
            f"""
            insert into {CATALOG_SCHEMA}.food
              (display_name, brand, barcode, source, source_id, basis,
               kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
               is_public, is_active, data)
            values
              ($1,$2,$3,'usda_fdc',$4,'per_100g',
               $5,$6,$7,$8,$9,$10,$11,
               $13::bool,true,$12::jsonb)
            on conflict (source, source_id) do update
              set display_name = excluded.display_name,
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
                  data = excluded.data,
                  is_active = true
            returning food_id, display_name, brand, barcode, source, source_id, basis,
                      kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
            """,
            str(desc).strip(),
            (str(brand_owner).strip() if brand_owner else None),
            (str(gtin).strip() if gtin else None),
            str(int(fdc_id)),
            kcal, protein, carbs, fat, fiber, sugar, sodium_mg,
            json.dumps(j),
            DEFAULT_PUBLIC_IMPORT,
        )
        return JSONResponse(_row_to_jsonable(up))
    finally:
        await conn.close()


@router.post("/foods/approve")
async def approve_food(
    food_id: str = Query(..., min_length=10),
):
    # Admin operation: mark a food as public/approved.
    try:
        fid = str(uuid.UUID(food_id))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid food_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {CATALOG_SCHEMA}.food
               set is_public = true,
                   updated_at = now()
             where food_id = $1::uuid
             returning food_id, display_name, brand, barcode, source, source_id, is_public
            """,
            fid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="food_id not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
