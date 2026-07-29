from __future__ import annotations

import unittest
from pathlib import Path
from uuid import UUID

from fastapi import Response
from pydantic import ValidationError

from rag_engine.lifeswitch_sage_router import (
    LifeSwitchSageRequestV1,
    apply_sage_no_store_headers,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
ROOT = Path(__file__).resolve().parents[1]


class LifeSwitchSageRouterTests(unittest.TestCase):
    def test_request_is_strict_and_calendar_scoped(self) -> None:
        value = LifeSwitchSageRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"contract_id":"training.calendar",'
            '"contract_version":"2026-07-29.1",'
            '"message":"Server-owned page contract and question."}'
        )
        self.assertEqual(value.user_id, ACTOR)

        with self.assertRaises(ValidationError):
            LifeSwitchSageRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "contract_id": "nutrition.capture",
                    "contract_version": "2026-07-29.1",
                    "message": "Not enabled.",
                }
            )

        with self.assertRaises(ValidationError):
            LifeSwitchSageRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "contract_id": "training.calendar",
                    "contract_version": "2026-07-29.1",
                    "message": "Hello.",
                    "search_capability_manifest": {},
                }
            )

    def test_no_store_headers_are_mandatory(self) -> None:
        response = Response()
        apply_sage_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")

    def test_runtime_excludes_unrelated_context_lanes(self) -> None:
        source = (
            ROOT / "rag_engine" / "lifeswitch_sage_router.py"
        ).read_text()
        self.assertIn("NoGovernedMemoryAssemblyProviderV1()", source)
        self.assertIn("fm_token_budget=0", source)
        self.assertIn("search_capability_manifest=None", source)
        self.assertNotIn("LiveGovernedMemoryAssemblyProviderV1", source)
        self.assertNotIn("SearchCapabilityManifestV1.create", source)
        self.assertNotIn("persist_finalized_response_v1", source)

    def test_app_mounts_dedicated_internal_route(self) -> None:
        source = (ROOT / "app.py").read_text()
        self.assertIn(
            'app.include_router(lifeswitch_sage_router, '
            'prefix="/lifeswitch/sage")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
