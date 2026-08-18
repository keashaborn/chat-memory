from __future__ import annotations

"""Disabled-by-default trusted web-search endpoint."""

import asyncio
import logging
import time
from uuid import UUID, uuid4

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

from seebx.capabilities.search.citation import CITATION_EVIDENCE_CONTRACT
from seebx.capabilities.search.admission import (
    TRUSTED_HEALTH_MAX_ADMITTED_SOURCES,
    WEB_EVIDENCE_ADMISSION_CONTRACT,
    admit_trusted_web_sources_v1,
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
from seebx.capabilities.search.budget import resolve_search_budget_v1
from rag_engine.trusted_web_provider_v1 import (
    OpenAITrustedWebProviderV1,
    TrustedWebProviderError,
    TrustedWebProviderSecurityError,
    TrustedWebSettingsV1,
    TrustedWebSourceV1,
    TRUSTED_WEB_INSTRUCTIONS_V1,
    WEB_SOURCE_PROVENANCE_CONTRACT,
)
from rag_engine.voice_language_v1 import (
    DEFAULT_VOICE_LANGUAGE,
    SUPPORTED_VOICE_LANGUAGE_IDS,
    response_language_instruction,
)
from seebx.capabilities.search.runtime import (
    NO_STORE_HEADERS,
    SearchAuditStoreUnavailableError,
    SearchRateLimitExceededError,
    SearchRuntimeConfigurationError,
    apply_search_no_store_headers,
    open_search_audit_session,
    safe_search_error_code,
    search_runtime_settings_from_env,
)
from seebx.capabilities.search.authorization import (
    require_web_search_actor_v1,
)


router = APIRouter()
logger = logging.getLogger("uvicorn.error")

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
    response_language: str = DEFAULT_VOICE_LANGUAGE

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

    @field_validator("response_language")
    @classmethod
    def validate_response_language(cls, value: str) -> str:
        if value not in SUPPORTED_VOICE_LANGUAGE_IDS:
            raise ValueError("response language is unsupported")
        return value


class TrustedWebResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    search_id: UUID
    policy_version: str
    topic: TrustedWebTopicV1
    disposition: TrustedWebDispositionV1
    reason: str
    searched: bool
    answer: str = Field(min_length=1, max_length=40_000)
    source_contract: str = WEB_SOURCE_PROVENANCE_CONTRACT
    admission_contract: str = WEB_EVIDENCE_ADMISSION_CONTRACT
    citation_evidence_contract: str = CITATION_EVIDENCE_CONTRACT
    citation_exact_page_source_count: int = Field(default=0, ge=0, le=50)
    citation_freshness_verified_source_count: int = Field(
        default=0,
        ge=0,
        le=50,
    )
    citation_archived_source_count: int = Field(default=0, ge=0, le=50)
    citation_freshness_status: str = Field(
        default="not_applicable",
        max_length=40,
    )
    sources: tuple[TrustedWebSourceV1, ...] = ()
    cited_sources: tuple[TrustedWebSourceV1, ...] = ()
    admitted_sources: tuple[TrustedWebSourceV1, ...] = ()
    consulted_sources: tuple[TrustedWebSourceV1, ...] = ()
    provider_consulted_source_count: int = Field(default=0, ge=0, le=50)
    admitted_source_count: int = Field(default=0, ge=0, le=50)
    rejected_source_count: int = Field(default=0, ge=0, le=50)
    max_admitted_sources: int = Field(
        default=TRUSTED_HEALTH_MAX_ADMITTED_SOURCES,
        ge=1,
        le=50,
    )


def apply_trusted_web_no_store_headers(response: Response) -> None:
    apply_search_no_store_headers(response)


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
    return (
        "Internet research is limited to the server's approved source packs "
        "for current news, health and medical evidence, nutrition, exercise "
        "and training, and official software or cybersecurity references."
    )


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

    try:
        runtime = search_runtime_settings_from_env()
    except SearchRuntimeConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="trusted_web_runtime_unconfigured",
        ) from None
    dsn = runtime.postgres_dsn
    safety_secret = runtime.safety_secret

    owner = UUID(await require_web_search_actor_v1(req, str(payload.user_id)))
    request_id = str(getattr(req.state, "request_id", "") or uuid4())[:128]
    search_id = uuid4()
    policy = route_trusted_web_query(
        payload.query,
        allow_bacb=settings.allow_bacb,
    )
    try:
        execution_budget = resolve_search_budget_v1(
            req,
            default_max_searches=2,
            default_max_sources=TRUSTED_HEALTH_MAX_ADMITTED_SOURCES,
        )
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail="trusted_web_search_budget_invalid",
        ) from None

    try:
        audit = await open_search_audit_session(
            postgres_dsn=dsn,
            actor_user_id=owner,
            requests_per_minute=settings.requests_per_minute,
            search_id=search_id,
            request_id=request_id,
            query=payload.query,
            policy_version=policy.policy_version,
            topic=policy.topic.value,
            disposition=policy.disposition.value,
            allowed_domains=policy.allowed_domains,
        )
    except SearchAuditStoreUnavailableError as exc:
        logger.error(
            "[trusted_web] search_id=%s status=failed error_type=%s",
            search_id,
            type(exc.__cause__).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_audit_store_unavailable",
        ) from None
    except SearchRateLimitExceededError:
        response.headers["retry-after"] = "60"
        logger.warning(
            "[trusted_web] search_id=%s status=rate_limited",
            search_id,
        )
        raise HTTPException(
            status_code=429,
            detail="trusted_web_rate_limited",
        ) from None
    except Exception as exc:
        logger.error(
            "[trusted_web] search_id=%s status=failed error_type=%s",
            search_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_unavailable",
        ) from None

    try:
        if policy.disposition != TrustedWebDispositionV1.SEARCH:
            latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000)
            await audit.finish(
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
                ods_record = await load_cached_ods_creatine_guidance(
                    audit.connection
                )
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
                    response_language=payload.response_language,
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
                    max_searches=execution_budget.max_searches,
                    instructions=(
                        TRUSTED_WEB_INSTRUCTIONS_V1
                        + "\n"
                        + response_language_instruction(
                            payload.response_language
                        )
                    ),
                ),
                timeout=settings.timeout_seconds + 5.0,
            )
        admission = admit_trusted_web_sources_v1(
            cited_sources=result.cited_sources,
            consulted_sources=result.consulted_sources,
            max_sources=execution_budget.max_sources,
            policy_pack="trusted_health",
        )
        latency_ms = round((time.monotonic_ns() - started_ns) / 1_000_000)
        await audit.finish(
            status="completed",
            latency_ms=latency_ms,
            provider_response_id=result.provider_response_id,
            sources=result.consulted_sources,
            cited_sources=admission.validated_cited_sources,
            admitted_sources=admission.admitted_sources,
            rejected_source_reasons=admission.rejected_source_reasons,
        )
        logger.info(
            "[trusted_web] search_id=%s status=completed topic=%s cited_source_count=%s admitted_source_count=%s provider_consulted_source_count=%s rejected_source_count=%s latency_ms=%s",
            search_id,
            policy.topic.value,
            len(result.cited_sources),
            len(admission.admitted_sources),
            len(result.consulted_sources),
            admission.rejected_source_count,
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
            sources=admission.validated_cited_sources,
            cited_sources=admission.validated_cited_sources,
            citation_evidence_contract=admission.citation_evidence_contract,
            citation_exact_page_source_count=(
                admission.exact_page_source_count
            ),
            citation_freshness_verified_source_count=(
                admission.freshness_verified_source_count
            ),
            citation_archived_source_count=admission.archived_source_count,
            citation_freshness_status=admission.freshness_status,
            admitted_sources=admission.admitted_sources,
            consulted_sources=result.consulted_sources,
            provider_consulted_source_count=len(result.consulted_sources),
            admitted_source_count=len(admission.admitted_sources),
            rejected_source_count=admission.rejected_source_count,
            max_admitted_sources=admission.max_sources,
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError as exc:
        await audit.finish(
            status="failed",
            latency_ms=round(
                (time.monotonic_ns() - started_ns) / 1_000_000
            ),
            error_code="trusted_web_timeout",
        )
        raise HTTPException(status_code=504, detail="trusted_web_timeout") from exc
    except ODSClientError as exc:
        await audit.finish(
            status="failed",
            latency_ms=round(
                (time.monotonic_ns() - started_ns) / 1_000_000
            ),
            error_code=safe_search_error_code(exc),
        )
        raise HTTPException(status_code=503, detail="trusted_web_ods_unavailable") from None
    except NCBIClientError as exc:
        await audit.finish(
            status="failed",
            latency_ms=round(
                (time.monotonic_ns() - started_ns) / 1_000_000
            ),
            error_code=safe_search_error_code(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_pubmed_unavailable",
        ) from None
    except TrustedWebProviderSecurityError as exc:
        await audit.finish(
            status="blocked",
            latency_ms=round(
                (time.monotonic_ns() - started_ns) / 1_000_000
            ),
            error_code=safe_search_error_code(exc),
        )
        logger.error(
            "[trusted_web] search_id=%s status=blocked reason=%s",
            search_id,
            safe_search_error_code(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="trusted_web_source_policy_violation",
        ) from None
    except TrustedWebProviderError as exc:
        await audit.finish(
            status="failed",
            latency_ms=round(
                (time.monotonic_ns() - started_ns) / 1_000_000
            ),
            error_code=safe_search_error_code(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="trusted_web_provider_unavailable",
        ) from None
    except Exception as exc:
        try:
            await audit.finish(
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
        await audit.close()


__all__ = [
    "TrustedWebRequestV1",
    "TrustedWebResponseV1",
    "apply_trusted_web_no_store_headers",
    "router",
]
