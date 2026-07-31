from __future__ import annotations

import unittest

from fastapi import Request

from rag_engine.search_plan_v1 import SearchBudgetV1
from rag_engine.search_runtime_budget_v1 import (
    bind_search_budget_v1,
    resolve_search_budget_v1,
)


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search/execute",
            "headers": [],
        }
    )


class SearchRuntimeBudgetV1Tests(unittest.TestCase):
    def test_direct_route_uses_server_defaults(self) -> None:
        effective = resolve_search_budget_v1(
            request(),
            default_max_searches=2,
            default_max_sources=5,
        )
        self.assertEqual(effective.max_searches, 2)
        self.assertEqual(effective.max_sources, 5)

    def test_plan_budget_can_only_reduce_route_caps(self) -> None:
        req = request()
        bind_search_budget_v1(
            req,
            SearchBudgetV1(max_searches=1, max_sources=3),
        )
        effective = resolve_search_budget_v1(
            req,
            default_max_searches=4,
            default_max_sources=10,
        )
        self.assertEqual(effective.max_searches, 1)
        self.assertEqual(effective.max_sources, 3)

    def test_route_caps_cannot_be_expanded_by_plan(self) -> None:
        req = request()
        bind_search_budget_v1(
            req,
            SearchBudgetV1(max_searches=12, max_sources=30),
        )
        effective = resolve_search_budget_v1(
            req,
            default_max_searches=2,
            default_max_sources=5,
        )
        self.assertEqual(effective.max_searches, 2)
        self.assertEqual(effective.max_sources, 5)

    def test_zero_execution_budget_fails_closed(self) -> None:
        req = request()
        bind_search_budget_v1(
            req,
            SearchBudgetV1(max_searches=0, max_sources=0),
        )
        with self.assertRaisesRegex(ValueError, "search_budget_plan_invalid"):
            resolve_search_budget_v1(
                req,
                default_max_searches=2,
                default_max_sources=5,
            )


if __name__ == "__main__":
    unittest.main()
