from __future__ import annotations

"""Authenticated, backend-owned RESSE response endpoint."""

import asyncio
import os
import logging
import time
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.governed_memory_provider_v1 import LiveGovernedMemoryAssemblyProviderV1
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_client import get_openai_client, normalize_chat_model
from rag_engine.response_composition_root_v0_2 import (
    AuthenticatedResponseCommandV0_2,
    InactiveResponseCompositionRootV0_2,
)
from rag_engine.response_inspection_v1 import build_response_inspection_v1
from rag_engine.response_persistence_v1 import persist_finalized_response_v1
from rag_engine.voice_observability_v1 import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
RESPONSE_QUERY_DEADLINE_SECONDS = 90.0
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def apply_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


class ResseResponseRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID
    message: str = Field(min_length=1, max_length=32_768)
    thread_id: UUID | None = None
    no_store: bool = False
    include_inspection: bool = False

    @field_validator("user_id", "thread_id", mode="before")
    @classmethod
    def parse_wire_uuid(cls, value: object) -> object:
        if value is None or isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("UUID fields must be JSON strings")
        try:
            return UUID(value)
        except ValueError:
            raise ValueError("UUID field is invalid") from None


@router.post("/query")
async def resse_response_query(
    payload: ResseResponseRequestV1, req: Request, response: Response
):
    request_started_ns = time.monotonic_ns()
    if not DSN:
        raise HTTPException(status_code=503, detail="response_runtime_unconfigured")
    owner = UUID(require_actor_matches_owner(req, str(payload.user_id)))
    request_id = str(getattr(req.state, "request_id", "") or uuid4())
    voice_turn_id = voice_turn_id_from_request(req)
    for name, value in voice_turn_response_headers(voice_turn_id).items():
        response.headers[name] = value
    if payload.no_store:
        apply_no_store_headers(response)
    stateless = payload.thread_id is None
    thread_id = payload.thread_id or uuid4()
    if not payload.no_store and stateless:
        raise HTTPException(status_code=400, detail="thread_id_required")

    conn = await asyncpg.connect(DSN, command_timeout=90)
    try:
        root = InactiveResponseCompositionRootV0_2(
            openai_client=get_openai_client(),
            classifier_model=os.getenv("RESSE_CLASSIFIER_MODEL", "gpt-5.1"),
            memory_provider=LiveGovernedMemoryAssemblyProviderV1(conn),
            generation_config=OpenAIChatGenerationConfigV1(
                model=normalize_chat_model(os.getenv("OPENAI_CHAT_MODEL")),
            ),
        )
        execution = await asyncio.wait_for(
            root.execute_detailed(
                conn,
                AuthenticatedResponseCommandV0_2(
                    authenticated_actor_user_id=owner,
                    thread_id=thread_id,
                    request_id=request_id,
                    current_message=payload.message,
                    request_field_names=tuple(
                        sorted(
                            set(payload.model_fields_set) - {"include_inspection"}
                        )
                    ),
                    stateless=stateless,
                ),
            ),
            timeout=RESPONSE_QUERY_DEADLINE_SECONDS,
        )
        finalized = execution.finalized
        persistence_started_ns = time.monotonic_ns()
        if not payload.no_store:
            await persist_finalized_response_v1(
                conn,
                owner_user_id=owner,
                thread_id=thread_id,
                request_id=request_id,
                finalized=finalized,
            )
        persistence_ms = max(
            0,
            round((time.monotonic_ns() - persistence_started_ns) / 1_000_000),
        )
        result = {
            "answer": finalized.assistant_text,
            "answer_id": str(finalized.answer_id),
            "output_kind": finalized.output_kind.value,
            "runtime": "resse_response_v0_2",
            "timings": {
                **execution.stage_timings.model_dump(mode="json"),
                "persistence_ms": persistence_ms,
                "backend_total_ms": max(
                    0,
                    round((time.monotonic_ns() - request_started_ns) / 1_000_000),
                ),
            },
        }
        if payload.include_inspection:
            try:
                result["inspection"] = build_response_inspection_v1(
                    trusted_plan=execution.trusted_plan,
                    provider_response=execution.provider_response,
                    finalized=finalized,
                    transcript_persistence=(
                        "skipped" if payload.no_store else "persisted"
                    ),
                    voice_turn_id=voice_turn_id,
                ).model_dump(mode="json")
            except Exception:
                logger.error(
                    "[response_inspection] trace unavailable answer_id=%s",
                    finalized.answer_id,
                )
        return result
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.error(
            "[resse_response] request deadline exceeded timeout_seconds=%s",
            RESPONSE_QUERY_DEADLINE_SECONDS,
        )
        raise HTTPException(
            status_code=504, detail="response_generation_timeout"
        ) from None
    except Exception as exc:
        logger.error(
            "[resse_response] request failed error_type=%s persistence_stage=%s",
            type(exc).__name__,
            str(getattr(exc, "stage", "not_applicable")),
        )
        raise HTTPException(status_code=503, detail="response_generation_unavailable") from None
    finally:
        await conn.close()


__all__ = ["router"]
