from __future__ import annotations

"""Authenticated, backend-owned RESSE response endpoint."""

import os
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rag_engine.governed_memory_provider_v1 import LiveGovernedMemoryAssemblyProviderV1
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_client import get_openai_client, normalize_chat_model
from rag_engine.response_composition_root_v0_2 import (
    AuthenticatedResponseCommandV0_2,
    InactiveResponseCompositionRootV0_2,
)
from rag_engine.response_persistence_v1 import persist_finalized_response_v1


router = APIRouter()
DSN = (os.getenv("POSTGRES_DSN") or "").strip()


class ResseResponseRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID
    message: str = Field(min_length=1, max_length=32_768)
    thread_id: UUID | None = None
    no_store: bool = False


@router.post("/query")
async def resse_response_query(payload: ResseResponseRequestV1, req: Request):
    if not DSN:
        raise HTTPException(status_code=503, detail="response_runtime_unconfigured")
    owner = UUID(require_actor_matches_owner(req, str(payload.user_id)))
    request_id = str(getattr(req.state, "request_id", "") or uuid4())
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
        finalized = await root.execute(
            conn,
            AuthenticatedResponseCommandV0_2(
                authenticated_actor_user_id=owner,
                thread_id=thread_id,
                request_id=request_id,
                current_message=payload.message,
                request_field_names=tuple(sorted(payload.model_fields_set)),
                stateless=stateless,
            ),
        )
        if not payload.no_store:
            await persist_finalized_response_v1(
                conn,
                owner_user_id=owner,
                thread_id=thread_id,
                request_id=request_id,
                finalized=finalized,
            )
        return {
            "answer": finalized.assistant_text,
            "answer_id": str(finalized.answer_id),
            "output_kind": finalized.output_kind.value,
            "runtime": "resse_response_v0_2",
        }
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[resse_response] request failed error_type={type(exc).__name__}")
        raise HTTPException(status_code=503, detail="response_generation_unavailable") from None
    finally:
        await conn.close()


__all__ = ["router"]
