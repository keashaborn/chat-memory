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


def _usda_guide_tokens(text: str) -> list[str]:
    stop = {
        "the", "and", "or", "with", "for", "from", "this", "that", "food",
        "oz", "fl", "g", "gram", "grams", "lb", "lbs", "pound", "pounds",
    }
    out = []
    for t in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(t) <= 1:
            continue
        if t in stop:
            continue
        out.append(t)
    return out


def _usda_guide_query_variants(q: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(q or "").strip())
    variants: list[str] = []

    def add(x: str) -> None:
        x = re.sub(r"\s+", " ", str(x or "").strip())
        if x and x.lower() not in {v.lower() for v in variants}:
            variants.append(x)

    add(raw)
    add(raw.replace("/", " "))
    add(raw.replace("%", " percent "))

    m = re.search(r"\b(\d{2,3})\s*/\s*(\d{1,2})\b", raw)
    if m:
        lean, fat = m.group(1), m.group(2)
        rest = re.sub(r"\b\d{2,3}\s*/\s*\d{1,2}\b", "", raw).strip()
        add(f"{lean}% lean {fat}% fat {rest}")
        add(f"{lean} percent lean {fat} percent fat {rest}")

        low = raw.lower()
        if "beef" in low or "hamburger" in low:
            add(f"{lean}% lean {fat}% fat ground beef")
            add(f"{lean} percent lean {fat} percent fat ground beef")
        if "pork" in low:
            add(f"{lean}% lean {fat}% fat ground pork")

    # USDA search sometimes behaves better without slash/percent punctuation.
    add(re.sub(r"[%/]", " ", raw))

    digits = re.sub(r"\D+", "", raw)
    if 8 <= len(digits) <= 14:
        add(digits)
        stripped = digits.lstrip("0")
        if stripped and stripped != digits:
            add(stripped)

    return variants[:8]


def _usda_nutr_amount(detail: dict, nutrient_number: str):
    for n in (detail or {}).get("foodNutrients") or []:
        nn = ((n.get("nutrient") or {}).get("number") or "")
        if str(nn) == str(nutrient_number):
            v = n.get("amount")
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
    return None


def _usda_nutrient_summary(detail: dict) -> dict:
    kcal = _usda_nutr_amount(detail, "208")
    protein = _usda_nutr_amount(detail, "203")
    carbs = _usda_nutr_amount(detail, "205")
    fat = _usda_nutr_amount(detail, "204")
    fiber = _usda_nutr_amount(detail, "291")
    sugar = _usda_nutr_amount(detail, "269")
    sodium_mg = _usda_nutr_amount(detail, "307")

    macro_check = None
    if kcal is not None and protein is not None and carbs is not None and fat is not None:
        calc = (protein * 4.0) + (carbs * 4.0) + (fat * 9.0)
        diff = abs(calc - kcal)
        pct = diff / max(abs(kcal), 1.0)
        macro_check = {
            "macro_kcal_estimate": round(calc, 1),
            "kcal_difference": round(diff, 1),
            "kcal_difference_pct": round(pct, 3),
            "status": "ok" if pct <= 0.10 else ("warn" if pct <= 0.20 else "mismatch"),
        }

    return {
        "kcal": kcal,
        "protein_g": protein,
        "carbs_g": carbs,
        "fat_g": fat,
        "fiber_g": fiber,
        "sugar_g": sugar,
        "sodium_mg": sodium_mg,
        "macro_check": macro_check,
    }


def _usda_family_intent(q: str) -> str | None:
    low = str(q or "").lower()
    families = ["beef", "pork", "chicken", "turkey", "salmon", "cod", "shrimp", "tuna", "egg"]
    for fam in families:
        if fam in low:
            return fam
    if "hamburger" in low:
        return "beef"
    return None


def _usda_score_candidate(query: str, search_row: dict, detail: dict) -> tuple[float, list[str], list[str]]:
    qlow = str(query or "").lower()
    qtokens = set(_usda_guide_tokens(query))

    desc = str((detail or {}).get("description") or search_row.get("description") or "")
    brand_owner = str((detail or {}).get("brandOwner") or search_row.get("brand_owner") or "")
    brand_name = str((detail or {}).get("brandName") or search_row.get("brand_name") or "")
    gtin = str((detail or {}).get("gtinUpc") or search_row.get("gtin_upc") or "")
    data_type = str((detail or {}).get("dataType") or search_row.get("data_type") or "")

    text = " ".join([desc, brand_owner, brand_name, data_type]).lower()
    ctokens = set(_usda_guide_tokens(text))

    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    try:
        search_score = float(search_row.get("score") or 0)
    except Exception:
        search_score = 0.0
    score += min(25.0, search_score / 80.0)

    common = qtokens & ctokens
    if common:
        score += min(45.0, len(common) * 7.0)
        reasons.append("matches key words: " + ", ".join(sorted(list(common))[:8]))

    # Strong UPC/barcode match.
    qdigits = re.sub(r"\D+", "", qlow)
    if 8 <= len(qdigits) <= 14 and gtin:
        if gtin.endswith(qdigits) or qdigits.endswith(gtin.lstrip("0")) or qdigits == gtin:
            score += 100.0
            reasons.append("UPC/barcode match")
        else:
            score -= 15.0
            warnings.append("UPC/barcode does not match this candidate")

    # Food-family intent constraints.
    family = _usda_family_intent(query)
    other_families = {"beef", "pork", "chicken", "turkey", "salmon", "cod", "shrimp", "tuna", "egg"}
    if family:
        if family in text:
            score += 35.0
            reasons.append(f"matches requested food type: {family}")
        else:
            mismatches = sorted([x for x in other_families if x != family and x in text])
            if mismatches:
                score -= 80.0
                warnings.append(f"query says {family}, candidate appears to be {mismatches[0]}")

    # Raw/cooked intent.
    wants_cooked = "cooked" in qlow or "prepared" in qlow
    wants_raw = "raw" in qlow
    if wants_cooked:
        if any(x in text for x in ["cooked", "pan-broiled", "crumbles", "prepared"]):
            score += 20.0
            reasons.append("matches cooked/prepared state")
        if "raw" in text:
            score -= 30.0
            warnings.append("query says cooked, candidate appears raw")
    if wants_raw:
        if "raw" in text:
            score += 15.0
            reasons.append("matches raw state")
        if any(x in text for x in ["cooked", "pan-broiled", "prepared"]):
            score -= 20.0
            warnings.append("query says raw, candidate appears cooked/prepared")

    # Lean/fat ratio intent like 96/4.
    m = re.search(r"\b(\d{2,3})\s*/\s*(\d{1,2})\b", qlow)
    if not m:
        m = re.search(r"\b(\d{2,3})\s*(?:%|percent)?\s*lean\b.*\b(\d{1,2})\s*(?:%|percent)?\s*fat\b", qlow)
    if m:
        lean, fat = m.group(1), m.group(2)
        if lean in text and fat in text and ("lean" in text or "fat" in text):
            score += 35.0
            reasons.append(f"matches lean/fat ratio {lean}/{fat}")
        else:
            score -= 10.0
            warnings.append(f"candidate does not clearly match lean/fat ratio {lean}/{fat}")

    if data_type.lower() == "branded" and len(qtokens) >= 3:
        score += 5.0
        reasons.append("branded item candidate")

    return score, reasons[:6], warnings[:6]


@router.get("/foods/usda/guide")
async def usda_food_guide(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=10),
):
    if not USDA_API_KEY:
        raise HTTPException(status_code=500, detail="USDA_API_KEY not configured on server")

    import requests

    query = re.sub(r"\s+", " ", str(q or "").strip())
    query_variants = _usda_guide_query_variants(query)
    search_errors: list[dict] = []

    def _search_one(search_q: str, page_size: int):
        return requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"api_key": USDA_API_KEY, "query": search_q, "pageSize": page_size},
            timeout=HTTP_TIMEOUT,
        )

    seen: dict[int, dict] = {}
    for search_q in query_variants:
        r = await asyncio.to_thread(_search_one, search_q, max(10, limit * 3))
        if r.status_code != 200:
            search_errors.append({"query": search_q, "status": r.status_code})
            continue

        j = r.json() if r.content else {}
        for f in (j or {}).get("foods") or []:
            fid = f.get("fdcId")
            try:
                fid_int = int(fid)
            except Exception:
                continue

            row = {
                "fdc_id": fid_int,
                "description": f.get("description"),
                "brand_owner": f.get("brandOwner"),
                "brand_name": f.get("brandName"),
                "gtin_upc": f.get("gtinUpc"),
                "data_type": f.get("dataType"),
                "published_date": f.get("publishedDate"),
                "score": f.get("score"),
                "matched_queries": [search_q],
            }

            if fid_int not in seen:
                seen[fid_int] = row
            else:
                seen[fid_int]["matched_queries"].append(search_q)
                try:
                    if float(row.get("score") or 0) > float(seen[fid_int].get("score") or 0):
                        row["matched_queries"] = seen[fid_int]["matched_queries"]
                        seen[fid_int] = row
                except Exception:
                    pass

    candidates = sorted(
        seen.values(),
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )[: max(12, limit * 3)]

    def _fetch_detail(fdc_id: int):
        return requests.get(
            f"https://api.nal.usda.gov/fdc/v1/food/{int(fdc_id)}",
            params={"api_key": USDA_API_KEY},
            timeout=HTTP_TIMEOUT,
        )

    enriched = []
    for row in candidates:
        r = await asyncio.to_thread(_fetch_detail, int(row["fdc_id"]))
        if r.status_code != 200:
            continue

        detail = r.json() if r.content else {}
        nutrients = _usda_nutrient_summary(detail)
        guide_score, reasons, warnings = _usda_score_candidate(query, row, detail)

        confidence = max(0.05, min(0.99, guide_score / 140.0))

        enriched.append({
            "fdc_id": row["fdc_id"],
            "description": detail.get("description") or row.get("description"),
            "brand_owner": detail.get("brandOwner") or row.get("brand_owner"),
            "brand_name": detail.get("brandName") or row.get("brand_name"),
            "gtin_upc": detail.get("gtinUpc") or row.get("gtin_upc"),
            "data_type": detail.get("dataType") or row.get("data_type"),
            "published_date": detail.get("publishedDate") or row.get("published_date"),
            "serving": {
                "serving_size": detail.get("servingSize"),
                "serving_size_unit": detail.get("servingSizeUnit"),
                "household_serving": detail.get("householdServingFullText"),
            },
            "basis": "per_100g",
            "nutrients": {
                "kcal": nutrients["kcal"],
                "protein_g": nutrients["protein_g"],
                "carbs_g": nutrients["carbs_g"],
                "fat_g": nutrients["fat_g"],
                "fiber_g": nutrients["fiber_g"],
                "sugar_g": nutrients["sugar_g"],
                "sodium_mg": nutrients["sodium_mg"],
            },
            "macro_check": nutrients["macro_check"],
            "search_score": row.get("score"),
            "guide_score": round(guide_score, 2),
            "confidence": round(confidence, 2),
            "reasons": reasons,
            "warnings": warnings,
            "matched_queries": row.get("matched_queries") or [],
            "import": {
                "fdc_id": row["fdc_id"],
            },
        })

    enriched.sort(key=lambda x: x["guide_score"], reverse=True)

    return JSONResponse({
        "query": query,
        "query_variants": query_variants,
        "search_errors": search_errors,
        "candidates": enriched[:limit],
    })

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
