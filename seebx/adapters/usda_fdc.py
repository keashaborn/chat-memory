from __future__ import annotations

"""Canonical external adapter for USDA FoodData Central."""

import asyncio
from collections.abc import Callable, Sequence
import os
from typing import Any

import requests


FOODS_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
FOOD_DETAIL_URL = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
HttpGet = Callable[..., Any]


class UsdaFdcError(RuntimeError):
    """Stable, credential-free USDA provider failure."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.upstream_status = upstream_status


def resolve_usda_api_key() -> str:
    api_key = (os.getenv("USDA_API_KEY") or "").strip()
    if not api_key:
        raise UsdaFdcError(500, "USDA_API_KEY not configured on server")
    return api_key


def resolve_usda_timeout() -> tuple[float, float]:
    try:
        timeout = (
            float(os.getenv("HTTP_CONNECT_TIMEOUT", "2")),
            float(os.getenv("HTTP_READ_TIMEOUT", "30")),
        )
    except (TypeError, ValueError) as error:
        raise UsdaFdcError(500, "USDA timeout configuration invalid") from error
    if any(value <= 0 for value in timeout):
        raise UsdaFdcError(500, "USDA timeout configuration invalid")
    return timeout


class UsdaFdcClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout: tuple[float, float],
        http_get: HttpGet = requests.get,
    ) -> None:
        if not str(api_key).strip():
            raise ValueError("api_key must not be empty")
        self._api_key = str(api_key)
        self._timeout = timeout
        self._http_get = http_get

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        not_found_detail: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http_get(url, params=params, timeout=self._timeout)
        except Exception as error:
            raise UsdaFdcError(502, "usda_fdc unavailable") from error

        status = int(response.status_code)
        if status == 404 and not_found_detail:
            raise UsdaFdcError(404, not_found_detail, upstream_status=status)
        if status != 200:
            raise UsdaFdcError(
                502,
                f"usda_fdc HTTP {status}",
                upstream_status=status,
            )
        try:
            payload = response.json() if response.content else {}
        except Exception as error:
            raise UsdaFdcError(502, "usda_fdc invalid response") from error
        if not isinstance(payload, dict):
            raise UsdaFdcError(502, "usda_fdc invalid response")
        return payload

    async def search(
        self,
        query: str,
        *,
        page_size: int,
        data_types: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "pageSize": page_size,
        }
        if data_types:
            params["dataType"] = list(data_types)
        payload = await asyncio.to_thread(
            self._get_json,
            FOODS_SEARCH_URL,
            params=params,
        )
        foods = payload.get("foods") or []
        return [row for row in foods if isinstance(row, dict)]

    async def detail(self, fdc_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_json,
            FOOD_DETAIL_URL.format(fdc_id=int(fdc_id)),
            params={"api_key": self._api_key},
            not_found_detail="fdc_id not found",
        )


def nutrient_summary(detail: dict[str, Any]) -> dict[str, float | dict | None]:
    def amount(number: str) -> float | None:
        for row in (detail or {}).get("foodNutrients") or []:
            nutrient_number = ((row.get("nutrient") or {}).get("number") or "")
            if str(nutrient_number) != number:
                continue
            try:
                value = row.get("amount")
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        return None

    values = {
        "kcal": amount("208"),
        "protein_g": amount("203"),
        "carbs_g": amount("205"),
        "fat_g": amount("204"),
        "fiber_g": amount("291"),
        "sugar_g": amount("269"),
        "sodium_mg": amount("307"),
    }
    macro_check = None
    if all(values[key] is not None for key in ("kcal", "protein_g", "carbs_g", "fat_g")):
        calculated = values["protein_g"] * 4.0 + values["carbs_g"] * 4.0 + values["fat_g"] * 9.0
        difference = abs(calculated - values["kcal"])
        difference_pct = difference / max(abs(values["kcal"]), 1.0)
        macro_check = {
            "macro_kcal_estimate": round(calculated, 1),
            "kcal_difference": round(difference, 1),
            "kcal_difference_pct": round(difference_pct, 3),
            "status": "ok" if difference_pct <= 0.10 else ("warn" if difference_pct <= 0.20 else "mismatch"),
        }
    return {**values, "macro_check": macro_check}


def usda_fdc_client() -> UsdaFdcClient:
    return UsdaFdcClient(
        api_key=resolve_usda_api_key(),
        timeout=resolve_usda_timeout(),
    )


__all__ = ["UsdaFdcClient", "UsdaFdcError", "nutrient_summary", "usda_fdc_client"]
