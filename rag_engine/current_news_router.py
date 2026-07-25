from __future__ import annotations

"""Disabled-fetch skeleton for bounded current-news lookup."""

import logging
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

try:
    from pydantic import field_validator
except ImportError:
    from pydantic import validator as _pydantic_validator

    def field_validator(*fields, mode="after", **kwargs):
        return _pydantic_validator(
            *fields,
            pre=(mode == "before"),
            allow_reuse=True,
        )

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)
from rag_engine.trusted_web_router import NO_STORE_HEADERS


router = APIRouter()
logger = logging.getLogger("uvicorn.error")


class CurrentNewsRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @classmethod
    def model_validate(cls, value):
        if hasattr(super(), "model_validate"):
            return super().model_validate(value)
        return cls.parse_obj(value)

    @classmethod
    def model_validate_json(cls, value: str):
        if hasattr(super(), "model_validate_json"):
            return super().model_validate_json(value)
        return cls.parse_raw(value)

    user_id: UUID
    query: str = Field(min_length=4, max_length=2_000)

    @field_validator("user_id", mode="before")
    @classmethod
    def parse_wire_uuid(cls, value: object) -> object:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("user_id must be a JSON string")
        try:
            return UUID(value)
        except ValueError:
            raise ValueError("user_id is invalid") from None

    @field_validator("query")
    @classmethod
    def validate_query_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 4:
            raise ValueError("query is too short")
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("query contains control characters")
        return normalized


class CurrentNewsResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    search_id: UUID
    policy_version: str
    topic: TrustedWebTopicV1
    disposition: TrustedWebDispositionV1
    reason: str
    searched: bool
    answer: str = Field(min_length=1, max_length=4_000)
    sources: tuple[object, ...] = ()


def apply_current_news_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


def _current_news_skeleton_answer(policy_topic: TrustedWebTopicV1) -> str:
    if policy_topic == TrustedWebTopicV1.CURRENT_NEWS:
        return (
            "Current news lookup is routed and policy-approved, but live news "
            "retrieval is not enabled yet."
        )
    return (
        "Current news lookup was not used because this question was outside "
        "current-news scope."
    )


@router.post("/query", response_model=CurrentNewsResponseV1)
async def current_news_query(
    payload: CurrentNewsRequestV1,
    req: Request,
    response: Response,
):
    started_ns = time.monotonic_ns()
    apply_current_news_no_store_headers(response)
    require_actor_matches_owner(req, str(payload.user_id))

    search_id = uuid4()
    policy = route_trusted_web_query(payload.query)
    searched = False
    # Fetch is deliberately disabled in this skeleton phase. Even policy-approved
    # current-news requests return a structured non-search response.
    disposition = TrustedWebDispositionV1.DECLINE
    reason = (
        "current_news_fetch_not_enabled"
        if policy.topic == TrustedWebTopicV1.CURRENT_NEWS
        else policy.reason
    )
    latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000)
    logger.info(
        "[current_news] search_id=%s status=skeleton topic=%s searched=%s latency_ms=%s",
        search_id,
        policy.topic.value,
        searched,
        latency_ms,
    )
    return CurrentNewsResponseV1(
        search_id=search_id,
        policy_version=policy.policy_version,
        topic=policy.topic,
        disposition=disposition,
        reason=reason,
        searched=searched,
        answer=_current_news_skeleton_answer(policy.topic),
        sources=(),
    )


__all__ = [
    "CurrentNewsRequestV1",
    "CurrentNewsResponseV1",
    "apply_current_news_no_store_headers",
    "router",
]
