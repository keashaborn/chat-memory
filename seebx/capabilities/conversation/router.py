from __future__ import annotations

"""Authenticated, backend-owned conversation response endpoint."""

import asyncio
import os
import logging
import time
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from seebx.adapters.conversation_attachments import (
    fetch_ready_message_attachments,
)
from seebx.capabilities.conversation.attachments import (
    MAX_ATTACHMENT_COUNT,
    build_attachment_context_block_v1,
)
from seebx.capabilities.conversation.memory_context import (
    InactiveMemoryContextProviderV1,
)
from seebx.capabilities.conversation.memory_contracts import (
    MEMORY_MODE_ZEP,
    MemoryAnswerProvenanceV1,
    MemoryNotApplicableReason,
    MemoryResponseConfigurationError,
)
from seebx.adapters.conversation_persistence import (
    persist_conversation_response,
)
from seebx.adapters.lifeswitch_context_runtime import (
    LazyPostgresRestrictedLifeSwitchReadSessionV1,
    LifeSwitchChatPoolManagerV1,
    LifeSwitchChatRuntimeSettingsV1,
)
from seebx.capabilities.conversation.lifeswitch_context import (
    LifeSwitchResponseContextProviderV1,
)
from rag_engine.lifeswitch_prior_answer_provenance_runtime_v1 import (
    InactivePriorLifeSwitchProvenanceProviderV1,
)
from seebx.core.identity import (
    ActorContext,
    require_actor_context,
)
from seebx.adapters.openai_chat import OpenAIChatGenerationConfigV1
from seebx.adapters.openai import get_openai_client
from seebx.capabilities.conversation.composition import (
    AuthenticatedResponseCommandV0_2,
    ConversationResponseComposer,
)
from seebx.capabilities.conversation.lifeswitch_composition import (
    LifeSwitchConversationComposer,
)
from seebx.capabilities.conversation.lifeswitch_inspection import build_response_inspection_v4
from seebx.capabilities.conversation.inspection import build_response_inspection_v2
from seebx.adapters.usage_postgres import persist_openai_chat_usage
from seebx.core.voice_observability import (
    voice_turn_id_from_request,
    voice_turn_response_headers,
)
from seebx.contracts.search import (
    TEXT_SEARCH_AUTHORIZATION_BASIS,
    VOICE_SEARCH_AUTHORIZATION_BASIS,
    SearchCapabilityManifestV1,
)
from seebx.capabilities.search.authorization import (
    TEXT_SEARCH_AUTHORIZATION_VALUE,
    VOICE_SEARCH_AUTHORIZATION_HEADER,
    VOICE_SEARCH_AUTHORIZATION_VALUE,
    require_web_search_actor_v1,
)
from seebx.contracts.voice_language import (
    AUTO_VOICE_LANGUAGE,
    voice_language_from_request,
)
from rag_engine.zep_memory_provider_v1 import (
    ZepMemoryChatProviderV1,
)
from seebx.capabilities.conversation.zep_runtime import (
    ZEP_MEMORY_RUNTIME,
    ZEP_PROMPT_SETTINGS,
)


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
DSN = (os.getenv("POSTGRES_DSN") or "").strip()
LIFESWITCH_CHAT_SETTINGS = LifeSwitchChatRuntimeSettingsV1.from_environment()
LIFESWITCH_CHAT_POOL = LifeSwitchChatPoolManagerV1(LIFESWITCH_CHAT_SETTINGS)
RESPONSE_QUERY_DEADLINE_SECONDS = 90.0
NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}
RESPONSE_MEMORY_MODE = MEMORY_MODE_ZEP


def response_memory_provenance_for_mode(
    *,
    mode: str,
    memory_provenance: MemoryAnswerProvenanceV1 | None,
) -> dict[str, object]:
    """Serialize exactly one mode-owned provenance contract; never fall back."""

    if mode == MEMORY_MODE_ZEP:
        if memory_provenance is None:
            raise MemoryResponseConfigurationError(
                "response_memory_provenance_mode_mismatch"
            )
        value = MemoryAnswerProvenanceV1.model_validate_json(
            memory_provenance.model_dump_json()
        )
        return value.model_dump(mode="json")
    raise MemoryResponseConfigurationError("response_memory_mode_invalid")


def memory_not_applicable_reason(
    *,
    no_store: bool,
    has_attachments: bool,
    is_voice: bool,
    has_web_search: bool,
) -> MemoryNotApplicableReason | None:
    """Choose one deterministic server-owned exclusion reason."""

    if any(type(value) is not bool for value in (
        no_store,
        has_attachments,
        is_voice,
        has_web_search,
    )):
        raise MemoryResponseConfigurationError(
            "memory_response_exclusion_state_invalid"
        )
    if no_store:
        return MemoryNotApplicableReason.NO_STORE
    if has_attachments:
        return MemoryNotApplicableReason.ATTACHMENT
    if is_voice:
        return MemoryNotApplicableReason.VOICE
    if has_web_search:
        return MemoryNotApplicableReason.WEB_SEARCH
    return None


@router.on_event("shutdown")
async def close_lifeswitch_chat_pool_v1() -> None:
    try:
        await LIFESWITCH_CHAT_POOL.close()
    finally:
        await ZEP_MEMORY_RUNTIME.close()


def apply_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


def _no_store_http_exception(
    status_code: int,
    detail: str,
    *,
    inherited_headers: dict[str, str] | None = None,
) -> HTTPException:
    headers = dict(inherited_headers or {})
    headers.update(NO_STORE_HEADERS)
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=headers,
    )


class ResseResponseRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID
    message: str = Field(min_length=1, max_length=32_768)
    thread_id: UUID | None = None
    message_id: UUID | None = None
    no_store: bool = False
    include_inspection: bool = False
    attachment_ids: tuple[UUID, ...] = Field(
        default=(),
        max_length=MAX_ATTACHMENT_COUNT,
    )
    attachment_message_id: UUID | None = None

    @field_validator(
        "user_id",
        "thread_id",
        "message_id",
        "attachment_message_id",
        mode="before",
    )
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

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def parse_attachment_ids(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("attachment_ids must be an array")
        parsed = []
        for item in value:
            if isinstance(item, UUID):
                parsed.append(item)
            elif isinstance(item, str):
                try:
                    parsed.append(UUID(item))
                except ValueError:
                    raise ValueError("attachment id is invalid") from None
            else:
                raise ValueError("attachment id is invalid")
        if len(set(parsed)) != len(parsed):
            raise ValueError("attachment ids must be unique")
        return tuple(parsed)

    @field_validator("attachment_ids")
    @classmethod
    def attachment_pairing(
        cls, value: tuple[UUID, ...], info: object
    ) -> tuple[UUID, ...]:
        return value


@router.post("/query")
async def resse_response_query(
    payload: ResseResponseRequestV1, req: Request, response: Response
):
    request_started_ns = time.monotonic_ns()
    response_memory_mode = RESPONSE_MEMORY_MODE
    if not DSN:
        raise _no_store_http_exception(503, "response_runtime_unconfigured")
    actor_context: ActorContext | None = None
    voice_turn_id = voice_turn_id_from_request(req)
    tentative_exclusion_reason = memory_not_applicable_reason(
        no_store=payload.no_store,
        has_attachments=bool(payload.attachment_ids),
        is_voice=voice_turn_id is not None,
        has_web_search=bool(
            (req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER) or "").strip()
        ),
    )
    if response_memory_mode != MEMORY_MODE_ZEP:
        raise _no_store_http_exception(503, "response_memory_mode_invalid")
    tentative_zep_eligible = (
        payload.thread_id is not None
        and tentative_exclusion_reason is None
    )
    try:
        actor_context = await require_actor_context(
            req,
            str(payload.user_id),
        )
    except HTTPException as exc:
        raise _no_store_http_exception(
            exc.status_code,
            str(exc.detail),
            inherited_headers=dict(exc.headers or {}),
        ) from None
    owner = actor_context.owner_user_id
    search_capability_manifest = None
    search_authorization = (
        req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER) or ""
    ).strip()
    if search_authorization:
        await require_web_search_actor_v1(
            req,
            str(owner),
            internal_assertion_required=True,
        )
        if search_authorization == TEXT_SEARCH_AUTHORIZATION_VALUE:
            authorization_basis = TEXT_SEARCH_AUTHORIZATION_BASIS
        elif search_authorization == VOICE_SEARCH_AUTHORIZATION_VALUE:
            authorization_basis = VOICE_SEARCH_AUTHORIZATION_BASIS
        else:
            raise HTTPException(
                status_code=403,
                detail="invalid_web_search_authorization",
            )
        search_capability_manifest = SearchCapabilityManifestV1.create(
            authorization_basis=authorization_basis,
        )
    request_id = str(getattr(req.state, "request_id", "") or uuid4())
    response_language = voice_language_from_request(
        req,
        default=AUTO_VOICE_LANGUAGE,
    )
    for name, value in voice_turn_response_headers(voice_turn_id).items():
        response.headers[name] = value
    if payload.no_store:
        apply_no_store_headers(response)
    stateless = payload.thread_id is None
    thread_id = payload.thread_id or uuid4()
    if not payload.no_store and stateless:
        raise HTTPException(status_code=400, detail="thread_id_required")
    if payload.attachment_ids and (
        payload.no_store
        or payload.thread_id is None
        or payload.attachment_message_id is None
        or search_capability_manifest is not None
    ):
        raise HTTPException(status_code=400, detail="invalid_attachment_context")
    if payload.attachment_message_id is not None and not payload.attachment_ids:
        raise HTTPException(status_code=400, detail="invalid_attachment_context")
    if payload.message_id is not None and (
        payload.no_store or payload.thread_id is None
    ):
        raise HTTPException(status_code=400, detail="invalid_message_context")
    if payload.attachment_ids and payload.message_id is not None and (
        payload.message_id != payload.attachment_message_id
    ):
        raise HTTPException(status_code=400, detail="invalid_message_context")

    zep_prompt_enabled = (
        tentative_zep_eligible
        and ZEP_PROMPT_SETTINGS.enabled_for(owner)
    )
    if tentative_zep_eligible and not zep_prompt_enabled:
        ZEP_MEMORY_RUNTIME.dispatch_retrieval(
            owner_user_id=owner,
            thread_id=thread_id,
        )

    conn = await asyncpg.connect(DSN, command_timeout=90)
    try:
        attachment_context_block = None
        if payload.attachment_ids:
            attachment_rows = await fetch_ready_message_attachments(
                conn,
                owner_user_id=owner,
                thread_id=payload.thread_id,
                message_id=payload.attachment_message_id,
                attachment_ids=payload.attachment_ids,
            )
            if len(attachment_rows) != len(payload.attachment_ids):
                raise HTTPException(status_code=404, detail="attachment_not_found")
            attachment_context_block = build_attachment_context_block_v1(
                rows=attachment_rows,
                request_id=request_id,
                current_message=payload.message,
            )
        openai_client = get_openai_client()
        generation_config = OpenAIChatGenerationConfigV1()
        exclusion_reason = memory_not_applicable_reason(
            no_store=payload.no_store,
            has_attachments=bool(payload.attachment_ids),
            is_voice=voice_turn_id is not None,
            has_web_search=search_capability_manifest is not None,
        )
        zep_eligible = (
            payload.thread_id is not None and exclusion_reason is None
        )
        zep_memory_provider = None
        if zep_eligible:
            if not zep_prompt_enabled:
                raise _no_store_http_exception(
                    503,
                    "zep_prompt_memory_unavailable",
                )
            zep_memory_provider = ZepMemoryChatProviderV1(
                ZEP_MEMORY_RUNTIME,
                logger=logger,
            )
            memory_provider = zep_memory_provider
            memory_lifecycle = None
        else:
            if exclusion_reason is None:
                raise MemoryResponseConfigurationError(
                    "memory_response_exclusion_reason_missing"
                )
            memory_provider = InactiveMemoryContextProviderV1(exclusion_reason)
            memory_lifecycle = memory_provider
        base_composer = ConversationResponseComposer(
            openai_client=openai_client,
            classifier_model=os.getenv("RESSE_CLASSIFIER_MODEL", "gpt-5.1"),
            memory_provider=memory_provider,
            successor_memory_lifecycle=memory_lifecycle,
            generation_config=generation_config,
        )
        command = AuthenticatedResponseCommandV0_2(
            authenticated_actor_user_id=owner,
            thread_id=thread_id,
            request_id=request_id,
            current_message=payload.message,
            request_field_names=tuple(
                sorted(
                    set(payload.model_fields_set)
                    - {
                        "include_inspection",
                        "message_id",
                        "attachment_ids",
                        "attachment_message_id",
                    }
                )
            ),
            stateless=stateless,
            search_capability_manifest=search_capability_manifest,
            response_language=response_language,
            attachment_context_block=attachment_context_block,
        )
        lifeswitch_enabled = LIFESWITCH_CHAT_SETTINGS.enabled_for(owner)
        if lifeswitch_enabled:
            context_provider = LifeSwitchResponseContextProviderV1(
                LazyPostgresRestrictedLifeSwitchReadSessionV1(
                    LIFESWITCH_CHAT_POOL
                )
            )
            prior_provenance_provider = InactivePriorLifeSwitchProvenanceProviderV1()
            lifeswitch_composer = LifeSwitchConversationComposer(
                base_composer=base_composer,
                openai_client=openai_client,
                context_provider=context_provider,
                prior_provenance_provider=prior_provenance_provider,
                generation_config=generation_config,
            )
            execution = await asyncio.wait_for(
                lifeswitch_composer.execute_detailed(conn, command),
                timeout=RESPONSE_QUERY_DEADLINE_SECONDS,
            )
        else:
            execution = await asyncio.wait_for(
                base_composer.execute_detailed(
                    conn,
                    command,
                ),
                timeout=RESPONSE_QUERY_DEADLINE_SECONDS,
            )
        finalized = execution.finalized
        if zep_memory_provider is not None:
            memory_provenance = zep_memory_provider.build_answer_provenance(
                answer_id=finalized.answer_id,
                prompt_sha256=(
                    execution.trusted_plan.assembled_prompt.manifest.assembly_sha256
                ),
                provider_request_sha256=(
                    execution.provider_response.provider_request_sha256
                ),
            ).model_dump(mode="json")
        else:
            memory_provenance = response_memory_provenance_for_mode(
                mode=response_memory_mode,
                memory_provenance=execution.successor_memory_provenance,
            )
        persistence_started_ns = time.monotonic_ns()
        await persist_openai_chat_usage(
            conn,
            owner_user_id=owner,
            answer_id=finalized.answer_id,
            source_channel="voice" if voice_turn_id is not None else "chat",
            provider_response=execution.provider_response,
        )
        if not payload.no_store:
            await persist_conversation_response(
                conn,
                owner_user_id=owner,
                thread_id=thread_id,
                request_id=request_id,
                finalized=finalized,
            )
        if (
            payload.message_id is not None
            and not payload.no_store
            and (zep_eligible or voice_turn_id is not None)
        ):
            ZEP_MEMORY_RUNTIME.dispatch_turn(
                owner_user_id=owner,
                thread_id=thread_id,
                user_message_id=payload.message_id,
                assistant_message_id=finalized.answer_id,
                user_message=payload.message,
                assistant_message=finalized.assistant_text,
            )
        persistence_ms = max(
            0,
            round((time.monotonic_ns() - persistence_started_ns) / 1_000_000),
        )
        result = {
            "answer": finalized.assistant_text,
            "answer_id": str(finalized.answer_id),
            "output_kind": finalized.output_kind.value,
            "memory_provenance": memory_provenance,
            "runtime": (
                "resse_response_v0_4" if lifeswitch_enabled else "resse_response_v0_2"
            ),
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
                if lifeswitch_enabled:
                    inspection = build_response_inspection_v4(
                        trusted_plan=execution.trusted_plan,
                        provider_response=execution.provider_response,
                        finalized=finalized,
                        transcript_persistence=(
                            "skipped" if payload.no_store else "persisted"
                        ),
                        voice_turn_id=voice_turn_id,
                    )
                else:
                    inspection = build_response_inspection_v2(
                        trusted_plan=execution.trusted_plan,
                        provider_response=execution.provider_response,
                        finalized=finalized,
                        transcript_persistence=(
                            "skipped" if payload.no_store else "persisted"
                        ),
                        voice_turn_id=voice_turn_id,
                    )
                result["inspection"] = inspection.model_dump(mode="json")
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


__all__ = [
    "response_memory_provenance_for_mode",
    "router",
    "memory_not_applicable_reason",
]
