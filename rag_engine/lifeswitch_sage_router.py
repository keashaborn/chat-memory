from __future__ import annotations

"""Page-scoped LifeSwitch Sage generation without general chat context."""

import asyncio
import logging
import os
import time
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.memory_actor_auth_v1 import require_memory_actor_v1
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_client import get_openai_client
from rag_engine.response_composition_root_v0_2 import (
    AuthenticatedResponseCommandV0_2,
    InactiveResponseCompositionRootV0_2,
    NoGovernedMemoryAssemblyProviderV1,
)
from rag_engine.usage_ledger_v1 import persist_openai_chat_usage_v1


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
SAGE_QUERY_DEADLINE_SECONDS = 90.0
SAGE_RUNTIME_VERSION = "lifeswitch_sage_v1"
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


class LifeSwitchSageRequestV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

    user_id: UUID
    contract_id: Literal["nutrition.log", "training.calendar"]
    contract_version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$")
    message: str = Field(min_length=1, max_length=32_768, repr=False)

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

    @field_validator("message")
    @classmethod
    def bounded_message_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 32_768:
            raise ValueError("message exceeds the byte limit")
        return value


def apply_sage_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


@router.post("/query")
async def lifeswitch_sage_query(
    payload: LifeSwitchSageRequestV1,
    req: Request,
    response: Response,
):
    request_started_ns = time.monotonic_ns()
    apply_sage_no_store_headers(response)
    if not DSN:
        raise HTTPException(status_code=503, detail="sage_runtime_unconfigured")

    owner = UUID(await require_memory_actor_v1(req, str(payload.user_id)))
    request_id = str(getattr(req.state, "request_id", "") or uuid4())
    conn = await asyncpg.connect(DSN, command_timeout=90)
    try:
        root = InactiveResponseCompositionRootV0_2(
            openai_client=get_openai_client(),
            classifier_model=os.getenv("RESSE_CLASSIFIER_MODEL", "gpt-5.1"),
            memory_provider=NoGovernedMemoryAssemblyProviderV1(),
            generation_config=OpenAIChatGenerationConfigV1(),
        )
        execution = await asyncio.wait_for(
            root.execute_detailed(
                conn,
                AuthenticatedResponseCommandV0_2(
                    authenticated_actor_user_id=owner,
                    thread_id=uuid4(),
                    request_id=request_id,
                    current_message=payload.message,
                    request_field_names=tuple(sorted(payload.model_fields_set)),
                    fm_token_budget=0,
                    stateless=True,
                    search_capability_manifest=None,
                ),
            ),
            timeout=SAGE_QUERY_DEADLINE_SECONDS,
        )
        finalized = execution.finalized
        await persist_openai_chat_usage_v1(
            conn,
            owner_user_id=owner,
            answer_id=finalized.answer_id,
            # The current immutable usage schema has product channels chat/voice.
            source_channel="chat",
            provider_response=execution.provider_response,
        )
        response.headers["x-vs-sage-runtime"] = SAGE_RUNTIME_VERSION
        response.headers["x-vs-sage-contract"] = payload.contract_id
        response.headers["x-vs-sage-contract-version"] = payload.contract_version
        response.headers["x-vs-sage-memory"] = "disabled"
        response.headers["x-vs-sage-fm"] = "disabled"
        response.headers["x-vs-sage-web-search"] = "disabled"
        return {
            "answer": finalized.assistant_text,
            "answer_id": str(finalized.answer_id),
            "runtime": SAGE_RUNTIME_VERSION,
            "contract_id": payload.contract_id,
            "contract_version": payload.contract_version,
            "timings": {
                **execution.stage_timings.model_dump(mode="json"),
                "backend_total_ms": max(
                    0,
                    round(
                        (time.monotonic_ns() - request_started_ns)
                        / 1_000_000
                    ),
                ),
            },
        }
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error(
            "[lifeswitch_sage] request deadline exceeded timeout_seconds=%s",
            SAGE_QUERY_DEADLINE_SECONDS,
        )
        raise HTTPException(
            status_code=504,
            detail="sage_generation_timeout",
        ) from None
    except Exception:
        logger.exception(
            "[lifeswitch_sage] generation failed request_id=%s contract_id=%s",
            request_id,
            payload.contract_id,
        )
        raise HTTPException(
            status_code=502,
            detail="sage_generation_unavailable",
        ) from None
    finally:
        await conn.close()


__all__ = [
    "LifeSwitchSageRequestV1",
    "SAGE_QUERY_DEADLINE_SECONDS",
    "SAGE_RUNTIME_VERSION",
    "apply_sage_no_store_headers",
    "router",
]
