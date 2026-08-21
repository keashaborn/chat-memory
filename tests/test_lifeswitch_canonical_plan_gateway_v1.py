from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / "ops/sql/20260821_lifeswitch_canonical_plan_gateway_v1.sql"
ROLLBACK = ROOT / "ops/sql/20260821_lifeswitch_canonical_plan_gateway_v1_rollback.sql"
ROUTES = ROOT / "seebx/capabilities/plans/routes.py"
ADAPTER = ROOT / "seebx/adapters/lifeswitch_plan_postgres.py"


class CanonicalPlanGatewayV1Tests(unittest.TestCase):
    def test_migration_is_self_contained_owner_bound_and_single_source(self) -> None:
        sql = APPLY.read_text().lower()
        self.assertIn("create or replace function lifeswitch_chat.whitelist_target_value_v1", sql)
        self.assertIn("create or replace function lifeswitch_chat.whitelist_plan_document_v1", sql)
        self.assertIn("'canonical_plan'::text", sql)
        self.assertIn("lifeswitch_plan.plan_profile", sql)
        self.assertIn("owner to lifeswitch_owner", sql)
        self.assertNotIn("lifeswitch_agentic", sql)
        self.assertNotIn("legacy_fallback", sql)
        self.assertNotIn("coach_notes", sql)

    def test_rollback_restores_the_exact_fail_closed_gateway(self) -> None:
        sql = ROLLBACK.read_text().lower()
        self.assertIn("select null::text,null::jsonb", sql)
        self.assertIn("where false", sql)
        self.assertIn("owner to lifeswitch_owner", sql)
        self.assertNotIn("lifeswitch_agentic", sql)
        self.assertNotIn("legacy_fallback", sql)

    def test_plan_routes_keep_the_five_http_contracts_and_no_sql(self) -> None:
        source = ROUTES.read_text()
        routes = re.findall(r'@router\.(get|post)\("([^"]+)"\)', source)
        self.assertEqual(
            routes,
            [
                ("get", "/profile"),
                ("post", "/profile/upsert"),
                ("get", "/profile/comments"),
                ("post", "/profile/comments/create"),
                ("get", "/profile/history"),
            ],
        )
        self.assertIn("lifeswitch_plan_repository", source)
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotRegex(source.lower(), r"\b(select|insert|update|delete)\s+(from|into|lifeswitch_plan)")

    def test_plan_adapter_is_the_only_plan_sql_effect_boundary(self) -> None:
        source = ADAPTER.read_text()
        self.assertIn("connect_lifeswitch", source)
        self.assertIn("plan_profile_history", source)
        self.assertIn("async with self._connection.transaction()", source)
        self.assertIn("await connection.close()", source)


if __name__ == "__main__":
    unittest.main()
