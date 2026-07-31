from __future__ import annotations

"""One server-owned search plan and executor for text and Realtime voice."""

import os
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.current_news_router import (
    CurrentNewsRequestV1,
    current_news_query,
)
from rag_engine.search_plan_v1 import SearchPlanV1, create_search_plan_v1
from rag_engine.search_runtime_budget_v1 import bind_search_budget_v1
from rag_engine.trusted_web_router import (
    TrustedWebRequestV1,
    trusted_web_query,
)
from rag_engine.web_search_actor_auth_v1 import require_web_search_actor_v1
from rag_engine.web_transcript_persistence_v1 import persist_web_exchange_v1
from rag_engine.voice_language_v1 import (
    AUTO_VOICE_LANGUAGE,
    SUPPORTED_VOICE_LANGUAGE_IDS,
)


router = APIRouter()
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


class SearchExecutionRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID
    thread_id: UUID | None = None
    query: str = Field(min_length=1, max_length=2_000)
    channel: Literal["text", "voice"]
    persist_transcript: bool = True
    response_language: str = AUTO_VOICE_LANGUAGE

    @field_validator("user_id", "thread_id", mode="before")
    @classmethod
    def parse_wire_uuid(cls, value: object) -> object:
        if value is None or isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("UUID fields must be strings")
        try:
            return UUID(value)
        except ValueError:
            raise ValueError("UUID field is invalid") from None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query is empty")
        return normalized

    @field_validator("response_language")
    @classmethod
    def validate_response_language(cls, value: str) -> str:
        if value not in SUPPORTED_VOICE_LANGUAGE_IDS:
            raise ValueError("response language is unsupported")
        return value


def _model_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return dict(value.dict())
    raise TypeError("value is not a serializable model")


def _source_list(value: Any) -> list[dict[str, Any]]:
    return [_model_json(item) for item in tuple(value or ())]


def _response_headers(
    *,
    route: str,
    searched: bool,
    cited_count: int = 0,
    admitted_count: int = 0,
    consulted_count: int = 0,
    rejected_count: int = 0,
) -> dict[str, str]:
    return {
        **NO_STORE_HEADERS,
        "X-VS-Search-Plan": "search_plan_v1",
        "X-VS-Search-Route": route,
        "X-VS-Web-Searched": "1" if searched else "0",
        "X-VS-Web-Source-Count": str(cited_count),
        "X-VS-Web-Cited-Source-Count": str(cited_count),
        "X-VS-Web-Admitted-Source-Count": str(admitted_count),
        "X-VS-Web-Provider-Consulted-Source-Count": str(consulted_count),
        "X-VS-Web-Consulted-Source-Count": str(consulted_count),
        "X-VS-Web-Rejected-Source-Count": str(rejected_count),
        "X-VS-Response-Runtime": (
            "current_news_v1"
            if route == "current_news"
            else "trusted_web_v1"
            if route == "trusted_health"
            else "resse_response_v0_2"
        ),
    }


_SOURCE_POLICY_VIOLATIONS = frozenset(
    {
        "current_news_source_policy_violation",
        "trusted_web_source_policy_violation",
    }
)


def _source_policy_violation_headers(
    *,
    route: str,
    exc: HTTPException,
) -> dict[str, str] | None:
    if (
        exc.status_code != 502
        or str(exc.detail or "") not in _SOURCE_POLICY_VIOLATIONS
    ):
        return None
    return _response_headers(route=route, searched=True)


@router.post("/execute")
async def execute_search_plan_v1(
    payload: SearchExecutionRequestV1,
    req: Request,
):
    owner = UUID(
        await require_web_search_actor_v1(
            req,
            str(payload.user_id),
            internal_assertion_required=True,
        )
    )
    plan: SearchPlanV1 = create_search_plan_v1(payload.query)
    plan_json = _model_json(plan)
    if payload.persist_transcript and payload.thread_id is None:
        raise HTTPException(
            status_code=400,
            detail="thread_id_required_for_search_transcript",
        )
    if plan.selected_route == "normal_chat":
        return JSONResponse(
            {
                "contract_version": "search_execution_v1",
                "plan": plan_json,
                "executed": False,
                "fallback_to_chat": False,
                "transcript_persistence": "not_applicable",
            },
            headers=_response_headers(
                route="normal_chat",
                searched=False,
            ),
        )

    bind_search_budget_v1(req, plan.budget)
    provider_response = Response()
    try:
        if plan.selected_route == "current_news":
            provider_result = await current_news_query(
                CurrentNewsRequestV1(
                    user_id=owner,
                    query=payload.query,
                    response_language=payload.response_language,
                ),
                req,
                provider_response,
            )
        else:
            provider_result = await trusted_web_query(
                TrustedWebRequestV1(
                    user_id=owner,
                    query=payload.query,
                    response_language=payload.response_language,
                ),
                req,
                provider_response,
            )
    except HTTPException as exc:
        headers = _source_policy_violation_headers(
            route=plan.selected_route,
            exc=exc,
        )
        if headers is None:
            raise
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from None

    result = _model_json(provider_result)
    searched = bool(result.get("searched"))
    if not searched:
        return JSONResponse(
            {
                "contract_version": "search_execution_v1",
                "plan": plan_json,
                "executed": False,
                "fallback_to_chat": True,
                "transcript_persistence": "not_applicable",
            },
            headers=_response_headers(
                route=plan.selected_route,
                searched=False,
            ),
        )

    answer = str(result.get("answer") or "").strip()
    if not answer:
        raise HTTPException(
            status_code=502,
            detail="search_execution_answer_missing",
        )
    search_id = UUID(str(result.get("search_id") or uuid4()))
    cited_sources = _source_list(
        result.get("cited_sources") or result.get("sources")
    )
    admitted_sources = _source_list(
        result.get("admitted_sources") or result.get("sources")
    )
    consulted_sources = _source_list(
        result.get("consulted_sources") or result.get("sources")
    )
    consulted_count = int(
        result.get("provider_consulted_source_count")
        or len(consulted_sources)
    )
    rejected_count = int(result.get("rejected_source_count") or 0)
    request_id = str(getattr(req.state, "request_id", "") or uuid4())[:128]
    if not payload.persist_transcript:
        answer_id = uuid4()
    elif not DSN:
        raise HTTPException(
            status_code=503,
            detail="search_transcript_store_unconfigured",
        )
    else:
        try:
            conn = await asyncpg.connect(DSN, command_timeout=15)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="search_transcript_store_unavailable",
            ) from None
        try:
            answer_id = await persist_web_exchange_v1(
                conn,
                owner_user_id=owner,
                thread_id=payload.thread_id,
                request_id=request_id,
                query=payload.query,
                answer=answer,
                search_id=search_id,
                route=plan.selected_route,
                policy_version=plan.policy_version,
                decision=plan.decision,
                cited_sources=cited_sources,
                admitted_sources=admitted_sources,
                consulted_source_count=consulted_count,
            )
        finally:
            await conn.close()

    return JSONResponse(
        {
            "contract_version": "search_execution_v1",
            "plan": plan_json,
            "executed": True,
            "fallback_to_chat": False,
            "transcript_persistence": (
                "persisted_memory_ineligible"
                if payload.persist_transcript
                else "skipped"
            ),
            "answer": answer,
            "answer_id": str(answer_id),
            "search_id": str(search_id),
            "sources": cited_sources,
            "cited_sources": cited_sources,
            "admitted_sources": admitted_sources,
            "consulted_sources": consulted_sources,
            "provider_consulted_source_count": consulted_count,
            "rejected_source_count": rejected_count,
        },
        headers=_response_headers(
            route=plan.selected_route,
            searched=True,
            cited_count=len(cited_sources),
            admitted_count=len(admitted_sources),
            consulted_count=consulted_count,
            rejected_count=rejected_count,
        ),
    )


__all__ = [
    "SearchExecutionRequestV1",
    "execute_search_plan_v1",
    "router",
]
