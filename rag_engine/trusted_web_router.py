from __future__ import annotations

"""Disabled-by-default trusted web-search endpoint."""

import asyncio
import logging
import os
import time
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
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

from rag_engine.supabase_actor_auth import require_verified_supabase_actor
from rag_engine.trusted_web_audit_v1 import (
    acquire_trusted_web_rate_limit_v1,
    finish_trusted_web_audit_v1,
    query_sha256,
    start_trusted_web_audit_v1,
)
from rag_engine.trusted_web_ncbi_v1 import (
    NCBIClientError,
    NCBIPubMedClientV1,
    trusted_web_topic_uses_ncbi,
)
from rag_engine.trusted_web_ods_v1 import (
    NIHODSClientV1,
    ODSClientError,
    load_cached_ods_creatine_guidance,
    trusted_web_query_uses_ods,
)
from rag_engine.trusted_web_policy_v1 import (
    TrustedWebDispositionV1,
    TrustedWebPolicyDecisionV1,
    TrustedWebTopicV1,
    route_trusted_web_query,
)
from rag_engine.trusted_web_provider_v1 import (
    OpenAITrustedWebProviderV1,
    TrustedWebProviderError,
    TrustedWebProviderSecurityError,
    TrustedWebSettingsV1,
    TrustedWebSourceV1,
)


router = APIRouter()
logger = logging.getLogger("uvicorn.error")

NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


class TrustedWebRequestV1(BaseModel):
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


class TrustedWebResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    search_id: UUID
    policy_version: str
    topic: TrustedWebTopicV1
    disposition: TrustedWebDispositionV1
    reason: str
    searched: bool
    answer: str = Field(min_length=1, max_length=40_000)
    sources: tuple[TrustedWebSourceV1, ...] = ()


def apply_trusted_web_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


def _decline_answer(policy: TrustedWebPolicyDecisionV1) -> str:
    if policy.topic == TrustedWebTopicV1.USDA_FOOD_COMPOSITION:
        return "Use the existing USDA FoodData Central integration for food composition and nutrient numbers."
    if policy.topic == TrustedWebTopicV1.INTERNAL_EXERCISE_LIBRARY:
        return "Use the internal exercise library for basic exercise technique and coaching cues."
    if policy.topic == TrustedWebTopicV1.SAFETY_STOP:
        return (
            "I can’t continue the optimization or research flow for this request. "
            "Stop the experiment and seek appropriate professional or emergency care "
            "if there may be immediate danger or serious symptoms."
        )
    return "Web search is limited to approved nutrition, lifting, physique, supplement, and self-experimentation evidence."


def _safe_error_code(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text and len(text) <= 100 and all(
        char.isalnum() or char in {"_", "-"} for char in text
    ):
        return text
    return type(exc).__name__[:100]


@router.post("/query", response_model=TrustedWebResponseV1)
async def trusted_web_query(
    payload: TrustedWebRequestV1,
    req: Request,
    response: Response,
):
    started_ns = time.monotonic_ns()
    apply_trusted_web_no_store_headers(response)
    try:
        settings = TrustedWebSettingsV1.from_env()
    except TrustedWebProviderError:
        raise HTTPException(
            status_code=503,
            detail="trusted_web_configuration_invalid",
        ) from None
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="trusted_web_search_disabled")

    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    safety_secret = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
    if not dsn or len(safety_secret) < 20:
        raise HTTPException(
            status_code=503,
            detail="trusted_web_runtime_unconfigured",
        )

    owner = UUID(
        await require_verified_supabase_actor(req, str(payload.user_id))
    )
    request_id = str(getattr(req.state, "request_id", "") or uuid4())[:128]
    search_id = uuid4()
    policy = route_trusted_web_query(
        payload.query,
        allow_bacb=settings.allow_bacb,
    )

    try:
        conn = await asyncpg.connect(dsn, command_timeout=15)
    except Exception as exc:
        logger.error(
            "[trusted_web] search_id=%s status=failed error_type=%s",
            search_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_audit_store_unavailable",
        ) from None
    audit_started = False
    try:
        allowed = await acquire_trusted_web_rate_limit_v1(
            conn,
            actor_user_id=owner,
            requests_per_minute=settings.requests_per_minute,
        )
        if not allowed:
            response.headers["retry-after"] = "60"
            logger.warning(
                "[trusted_web] search_id=%s status=rate_limited",
                search_id,
            )
            raise HTTPException(status_code=429, detail="trusted_web_rate_limited")

        await start_trusted_web_audit_v1(
            conn,
            search_id=search_id,
            actor_user_id=owner,
            request_id=request_id,
            query_hash=query_sha256(payload.query),
            policy_version=policy.policy_version,
            topic=policy.topic.value,
            disposition=policy.disposition.value,
            allowed_domains=policy.allowed_domains,
        )
        audit_started = True

        if policy.disposition != TrustedWebDispositionV1.SEARCH:
            latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000)
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status=policy.disposition.value,
                latency_ms=latency_ms,
            )
            logger.info(
                "[trusted_web] search_id=%s status=%s topic=%s source_count=0 latency_ms=%s",
                search_id,
                policy.disposition.value,
                policy.topic.value,
                latency_ms,
            )
            return TrustedWebResponseV1(
                search_id=search_id,
                policy_version=policy.policy_version,
                topic=policy.topic,
                disposition=policy.disposition,
                reason=policy.reason,
                searched=False,
                answer=_decline_answer(policy),
            )

        from rag_engine.openai_client import get_openai_client

        provider = OpenAITrustedWebProviderV1(
            get_openai_client(),
            settings,
        )
        if trusted_web_topic_uses_ncbi(policy.topic):
            ods_records = ()
            if trusted_web_query_uses_ods(payload.query):
                ods_record = await load_cached_ods_creatine_guidance(conn)
                if ods_record is None:
                    ods_record = NIHODSClientV1().creatine_exercise_performance()
                ods_records = (ods_record,)
            ncbi_client = NCBIPubMedClientV1.from_env()
            if ods_records:
                ncbi_client = NCBIPubMedClientV1(
                    timeout_seconds=ncbi_client.timeout_seconds,
                    max_records=2,
                    api_key=ncbi_client.api_key,
                    tool_email=ncbi_client.tool_email,
                    tool_name=ncbi_client.tool_name,
                )
            ncbi_records = await asyncio.wait_for(
                asyncio.to_thread(
                    ncbi_client.search,
                    payload.query,
                ),
                timeout=35.0,
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    provider.synthesize_from_pubmed_records,
                    query=payload.query,
                    records=ncbi_records,
                    ods_records=ods_records,
                    actor_user_id=str(owner),
                    safety_secret=safety_secret,
                ),
                timeout=settings.timeout_seconds + 5.0,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    provider.search,
                    query=payload.query,
                    policy=policy,
                    actor_user_id=str(owner),
                    safety_secret=safety_secret,
                ),
                timeout=settings.timeout_seconds + 5.0,
            )
        latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000)
        await finish_trusted_web_audit_v1(
            conn,
            search_id=search_id,
            status="completed",
            latency_ms=latency_ms,
            provider_response_id=result.provider_response_id,
            sources=result.sources,
        )
        logger.info(
            "[trusted_web] search_id=%s status=completed topic=%s source_count=%s latency_ms=%s",
            search_id,
            policy.topic.value,
            len(result.sources),
            latency_ms,
        )
        return TrustedWebResponseV1(
            search_id=search_id,
            policy_version=policy.policy_version,
            topic=policy.topic,
            disposition=policy.disposition,
            reason=policy.reason,
            searched=True,
            answer=result.answer_markdown(),
            sources=result.sources,
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError as exc:
        if audit_started:
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status="failed",
                latency_ms=round(
                    (time.monotonic_ns() - started_ns) / 1_000_000
                ),
                error_code="trusted_web_timeout",
            )
        raise HTTPException(status_code=504, detail="trusted_web_timeout") from exc
    except ODSClientError as exc:
        if audit_started:
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status="failed",
                latency_ms=round(
                    (time.monotonic_ns() - started_ns) / 1_000_000
                ),
                error_code=_safe_error_code(exc),
            )
        raise HTTPException(status_code=503, detail="trusted_web_ods_unavailable") from None
    except NCBIClientError as exc:
        if audit_started:
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status="failed",
                latency_ms=round(
                    (time.monotonic_ns() - started_ns) / 1_000_000
                ),
                error_code=_safe_error_code(exc),
            )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_pubmed_unavailable",
        ) from None
    except TrustedWebProviderSecurityError as exc:
        if audit_started:
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status="blocked",
                latency_ms=round(
                    (time.monotonic_ns() - started_ns) / 1_000_000
                ),
                error_code=_safe_error_code(exc),
            )
        logger.error(
            "[trusted_web] search_id=%s status=blocked reason=%s",
            search_id,
            _safe_error_code(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="trusted_web_source_policy_violation",
        ) from None
    except TrustedWebProviderError as exc:
        if audit_started:
            await finish_trusted_web_audit_v1(
                conn,
                search_id=search_id,
                status="failed",
                latency_ms=round(
                    (time.monotonic_ns() - started_ns) / 1_000_000
                ),
                error_code=_safe_error_code(exc),
            )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_provider_unavailable",
        ) from None
    except Exception as exc:
        if audit_started:
            try:
                await finish_trusted_web_audit_v1(
                    conn,
                    search_id=search_id,
                    status="failed",
                    latency_ms=round(
                        (time.monotonic_ns() - started_ns) / 1_000_000
                    ),
                    error_code=type(exc).__name__[:100],
                )
            except Exception:
                pass
        logger.error(
            "[trusted_web] search_id=%s status=failed error_type=%s",
            search_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_unavailable",
        ) from None
    finally:
        await conn.close()


__all__ = [
    "TrustedWebRequestV1",
    "TrustedWebResponseV1",
    "apply_trusted_web_no_store_headers",
    "router",
]
