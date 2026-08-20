from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from seebx.adapters.usda_fdc import (
    FOOD_DETAIL_URL,
    FOODS_SEARCH_URL,
    UsdaFdcClient,
    UsdaFdcError,
    nutrient_summary,
    resolve_usda_api_key,
    resolve_usda_timeout,
)


class Response:
    def __init__(self, status_code: int, payload=None, *, content=True):
        self.status_code = status_code
        self.payload = payload
        self.content = b"x" if content else b""

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class UsdaFdcAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_is_resolved_at_call_time_without_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(UsdaFdcError) as missing:
                resolve_usda_api_key()
        self.assertEqual(missing.exception.status_code, 500)
        self.assertEqual(
            missing.exception.detail,
            "USDA_API_KEY not configured on server",
        )
        with patch.dict(os.environ, {"USDA_API_KEY": " test-key "}, clear=True):
            self.assertEqual(resolve_usda_api_key(), "test-key")

    def test_timeout_configuration_is_positive_and_content_free(self):
        with patch.dict(
            os.environ,
            {"HTTP_CONNECT_TIMEOUT": "bad", "HTTP_READ_TIMEOUT": "30"},
            clear=True,
        ):
            with self.assertRaises(UsdaFdcError) as invalid:
                resolve_usda_timeout()
        self.assertEqual(invalid.exception.status_code, 500)
        self.assertEqual(invalid.exception.detail, "USDA timeout configuration invalid")

    async def test_search_binds_key_query_size_and_data_type(self):
        calls = []

        def get(url, *, params, timeout):
            calls.append((url, params, timeout))
            return Response(200, {"foods": [{"fdcId": 7}, "invalid"]})

        client = UsdaFdcClient(
            api_key="secret",
            timeout=(2.0, 30.0),
            http_get=get,
        )
        foods = await client.search(
            "012345678901",
            page_size=15,
            data_types=("Branded",),
        )

        self.assertEqual(foods, [{"fdcId": 7}])
        self.assertEqual(calls[0][0], FOODS_SEARCH_URL)
        self.assertEqual(
            calls[0][1],
            {
                "api_key": "secret",
                "query": "012345678901",
                "pageSize": 15,
                "dataType": ["Branded"],
            },
        )
        self.assertEqual(calls[0][2], (2.0, 30.0))

    async def test_detail_maps_success_not_found_and_upstream_failure(self):
        responses = [
            Response(200, {"fdcId": 11}),
            Response(404, {}),
            Response(503, {}),
        ]

        def get(url, *, params, timeout):
            self.assertEqual(url, FOOD_DETAIL_URL.format(fdc_id=11))
            return responses.pop(0)

        client = UsdaFdcClient(
            api_key="secret",
            timeout=(2.0, 30.0),
            http_get=get,
        )
        self.assertEqual(await client.detail(11), {"fdcId": 11})
        with self.assertRaises(UsdaFdcError) as missing:
            await client.detail(11)
        self.assertEqual((missing.exception.status_code, missing.exception.detail), (404, "fdc_id not found"))
        with self.assertRaises(UsdaFdcError) as unavailable:
            await client.detail(11)
        self.assertEqual((unavailable.exception.status_code, unavailable.exception.detail), (502, "usda_fdc HTTP 503"))
        self.assertEqual(unavailable.exception.upstream_status, 503)

    async def test_transport_and_payload_failures_are_content_free(self):
        def transport_failure(*args, **kwargs):
            raise RuntimeError("private network detail")

        client = UsdaFdcClient(
            api_key="secret",
            timeout=(2.0, 30.0),
            http_get=transport_failure,
        )
        with self.assertRaises(UsdaFdcError) as unavailable:
            await client.search("food", page_size=10)
        self.assertEqual(unavailable.exception.detail, "usda_fdc unavailable")
        self.assertNotIn("private", str(unavailable.exception))

        invalid = UsdaFdcClient(
            api_key="secret",
            timeout=(2.0, 30.0),
            http_get=lambda *a, **k: Response(200, ValueError("secret")),
        )
        with self.assertRaises(UsdaFdcError) as invalid_response:
            await invalid.search("food", page_size=10)
        self.assertEqual(invalid_response.exception.detail, "usda_fdc invalid response")

    def test_nutrient_summary_preserves_usda_numbers_and_macro_check(self):
        detail = {
            "foodNutrients": [
                {"nutrient": {"number": "208"}, "amount": 170},
                {"nutrient": {"number": "203"}, "amount": 20},
                {"nutrient": {"number": "205"}, "amount": 0},
                {"nutrient": {"number": "204"}, "amount": 10},
                {"nutrient": {"number": "291"}, "amount": 1},
                {"nutrient": {"number": "269"}, "amount": "bad"},
                {"nutrient": {"number": "307"}, "amount": 75},
            ]
        }
        value = nutrient_summary(detail)
        self.assertEqual(value["kcal"], 170.0)
        self.assertEqual(value["protein_g"], 20.0)
        self.assertEqual(value["fat_g"], 10.0)
        self.assertIsNone(value["sugar_g"])
        self.assertEqual(value["macro_check"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
