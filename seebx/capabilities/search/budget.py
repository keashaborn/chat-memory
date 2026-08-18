from __future__ import annotations

"""Server-only binding for effective search execution budgets."""

from fastapi import Request

from seebx.capabilities.search.plan import SearchBudgetV1


_REQUEST_STATE_KEY = "server_search_budget_v1"


def bind_search_budget_v1(
    request: Request,
    budget: SearchBudgetV1,
) -> None:
    """Bind a server-created plan budget to this request.

    The value is stored in request state, so browser fields and headers cannot
    expand the budget selected by the server planner.
    """

    setattr(request.state, _REQUEST_STATE_KEY, budget)


def resolve_search_budget_v1(
    request: Request,
    *,
    default_max_searches: int,
    default_max_sources: int,
) -> SearchBudgetV1:
    if default_max_searches < 1 or default_max_sources < 1:
        raise ValueError("search_budget_defaults_invalid")
    planned = getattr(request.state, _REQUEST_STATE_KEY, None)
    if not isinstance(planned, SearchBudgetV1):
        return SearchBudgetV1(
            max_searches=default_max_searches,
            max_sources=default_max_sources,
        )
    if planned.max_searches < 1 or planned.max_sources < 1:
        raise ValueError("search_budget_plan_invalid")
    return SearchBudgetV1(
        max_searches=min(default_max_searches, planned.max_searches),
        max_sources=min(default_max_sources, planned.max_sources),
    )


__all__ = [
    "bind_search_budget_v1",
    "resolve_search_budget_v1",
]
