from __future__ import annotations

import asyncio
import re
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse
import uuid
import decimal
import datetime as _dt

from seebx.adapters.lifeswitch_catalog_postgres import (
    lifeswitch_catalog_reader,
)

from seebx.adapters.usda_fdc import (
    UsdaFdcError,
    nutrient_summary as usda_nutrient_summary,
    usda_fdc_client,
)
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

@router.get("/exercises/search")
async def search_exercises(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    locale: str = Query("en", min_length=2, max_length=10),
):
    async with lifeswitch_catalog_reader() as catalog:
        rows = await catalog.search_exercises(
            q,
            limit,
            locale,
        )
    return JSONResponse([_row_to_jsonable(r) for r in rows])


@router.get("/exercises/browse")
async def browse_exercises(
    q: str = Query("", max_length=120),
    movement_group: str = Query("", max_length=80),
    kind: str = Query("strength", min_length=1, max_length=40),
    limit: int = Query(100, ge=1, le=200),
):
    clean_q = str(q or "").strip()
    clean_group = str(movement_group or "").strip().lower()
    clean_kind = str(kind or "strength").strip().lower()

    async with lifeswitch_catalog_reader() as catalog:
        rows = await catalog.browse_exercises(
            clean_q,
            clean_group,
            clean_kind,
            limit,
        )

    families: list[dict] = []
    family_by_id: dict[str, dict] = {}

    for row in rows:
        family_id = str(row["exercise_family_id"])
        family = family_by_id.get(family_id)

        if family is None:
            family = {
                "exercise_family_id": family_id,
                "slug": row["family_slug"],
                "display_name": row["family_name"],
                "kind": row["kind"],
                "movement_group": row["movement_group"],
                "movement_pattern": row["movement_pattern"],
                "primary_muscles": list(row["family_primary_muscles"] or []),
                "description": row["description"],
                "sort_order": row["family_sort_order"],
                "variants": [],
            }
            family_by_id[family_id] = family
            families.append(family)

        family["variants"].append(
            {
                "exercise_family_member_id": str(
                    row["exercise_family_member_id"]
                ),
                "exercise_id": str(row["exercise_id"]),
                "slug": row["exercise_slug"],
                "display_name": row["display_name"],
                "variant_label": row["variant_label"],
                "modality": row["modality"],
                "primary_muscles": list(row["primary_muscles"] or []),
                "equipment_required": list(row["equipment_required"] or []),
                "unilateral": bool(row["unilateral"]),
                "is_default": bool(row["is_default"]),
                "sort_order": row["variant_sort_order"],
            }
        )

    return JSONResponse(families)


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

def _usda_nutrient_summary(detail: dict) -> dict:
    return usda_nutrient_summary(detail)


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


@router.get("/foods/usda/barcode")
async def usda_food_barcode(
    upc: str = Query(..., min_length=6, max_length=32),
    limit: int = Query(5, ge=1, le=10),
):
    try:
        client = usda_fdc_client()
    except UsdaFdcError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    digits = re.sub(r"\D+", "", str(upc or ""))
    if len(digits) < 6:
        raise HTTPException(status_code=400, detail="upc must contain at least 6 digits")

    def _norm(v: str) -> str:
        return re.sub(r"\D+", "", str(v or "")).lstrip("0")

    target = _norm(digits)

    try:
        foods = await client.search(
            digits,
            data_types=("Branded",),
            page_size=max(10, limit * 3),
        )
    except UsdaFdcError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    exact = []
    loose = []
    for f in foods:
        gtin = f.get("gtinUpc") or ""
        ng = _norm(gtin)
        if ng and ng == target:
            exact.append(f)
        elif ng and (ng.endswith(target) or target.endswith(ng)):
            loose.append(f)

    matches = (exact or loose)[:limit]

    out = []
    for f in matches:
        fid = f.get("fdcId")
        try:
            fid_int = int(fid)
        except Exception:
            continue

        detail = {}
        try:
            detail = await client.detail(fid_int)
        except UsdaFdcError:
            detail = {}

        nutrients = _usda_nutrient_summary(detail) if detail else {
            "kcal": None,
            "protein_g": None,
            "carbs_g": None,
            "fat_g": None,
            "fiber_g": None,
            "sugar_g": None,
            "sodium_mg": None,
            "macro_check": {"status": "unknown"},
        }

        out.append({
            "fdc_id": fid_int,
            "description": detail.get("description") or f.get("description"),
            "brand_owner": detail.get("brandOwner") or f.get("brandOwner"),
            "brand_name": detail.get("brandName") or f.get("brandName"),
            "gtin_upc": detail.get("gtinUpc") or f.get("gtinUpc"),
            "data_type": detail.get("dataType") or f.get("dataType"),
            "published_date": detail.get("publishedDate") or f.get("publishedDate"),
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
            "search_score": f.get("score"),
            "guide_score": 140 if exact else 100,
            "confidence": 0.99 if exact else 0.75,
            "reasons": ["exact UPC/barcode match"] if exact else ["UPC/barcode candidate"],
            "warnings": [] if exact else ["UPC match was not exact after normalization"],
            "matched_queries": [digits],
            "import": {
                "fdc_id": fid_int,
            },
        })

    return JSONResponse({
        "upc": digits,
        "match_type": "exact" if exact else ("loose" if loose else "none"),
        "candidates": out,
    })


@router.get("/foods/usda/guide")
async def usda_food_guide(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=10),
):
    try:
        client = usda_fdc_client()
    except UsdaFdcError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    query = re.sub(r"\s+", " ", str(q or "").strip())
    query_variants = _usda_guide_query_variants(query)
    search_errors: list[dict] = []

    search_gate = asyncio.Semaphore(6)
    async def _search_one(search_q: str):
        try:
            async with search_gate:
                foods = await client.search(search_q, page_size=max(10, limit * 3))
            return search_q, foods, None
        except UsdaFdcError as error:
            return search_q, [], error

    seen: dict[int, dict] = {}
    for search_q, foods, error in await asyncio.gather(
        *(_search_one(search_q) for search_q in query_variants)
    ):
        if error is not None:
            if error.upstream_status is not None:
                search_errors.append({"query": search_q, "status": error.upstream_status})
            else:
                search_errors.append({"query": search_q, "error": error.detail})
            continue
        for food in foods:
            try:
                fdc_id = int(food.get("fdcId"))
            except (TypeError, ValueError):
                continue
            row = {
                "fdc_id": fdc_id,
                "description": food.get("description"),
                "brand_owner": food.get("brandOwner"),
                "brand_name": food.get("brandName"),
                "gtin_upc": food.get("gtinUpc"),
                "data_type": food.get("dataType"),
                "published_date": food.get("publishedDate"),
                "score": food.get("score"),
                "matched_queries": [search_q],
            }
            if fdc_id not in seen:
                seen[fdc_id] = row
                continue
            seen[fdc_id]["matched_queries"].append(search_q)
            try:
                if float(row.get("score") or 0) > float(seen[fdc_id].get("score") or 0):
                    row["matched_queries"] = seen[fdc_id]["matched_queries"]
                    seen[fdc_id] = row
            except (TypeError, ValueError):
                pass

    detail_depth = min(12, max(8, limit * 2))
    candidates = sorted(
        seen.values(),
        key=lambda x: float(x.get("score") or 0),
        reverse=True,
    )[:detail_depth]

    detail_gate = asyncio.Semaphore(8)
    async def _fetch_detail(row: dict):
        try:
            async with detail_gate:
                return row, await client.detail(int(row["fdc_id"]))
        except UsdaFdcError:
            return None

    enriched = []
    detail_results = await asyncio.gather(*(_fetch_detail(row) for row in candidates))
    for result in detail_results:
        if result is None:
            continue
        row, detail = result
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
