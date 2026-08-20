"""Canonical HTTP boundary for owner-scoped conversation threads."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from seebx.adapters.conversation_history import fetch_thread_message_rows
from seebx.adapters.conversation_threads import (
    archive_thread,
    create_and_select_thread,
    fetch_thread_title_state,
    fetch_thread_title_transcript,
    list_visible_threads,
    rename_thread_manual,
    set_thread_pinned,
    thread_belongs_to_owner,
    update_thread_automatic_title,
)
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.adapters.thread_selection import (
    ActiveThreadSelectionV1Error,
    clear_active_thread_v1,
    get_active_thread_v1,
    select_active_thread_v1,
)
from seebx.capabilities.conversation.thread_title import (
    generate_semantic_title,
    select_first_meaningful_exchange,
)
from seebx.contracts.conversation import WEB_ASSISTANT_SOURCE
from seebx.core.identity import require_actor, require_request_actor


class NewThreadReq(BaseModel):
    user_id: str
    title: Optional[str] = None


class PinThreadReq(BaseModel):
    pinned: bool


class ActiveThreadReq(BaseModel):
    user_id: str
    thread_id: str


class RenameThreadReq(BaseModel):
    title: str
    title_source: Literal["automatic", "manual"] = "manual"


def _parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _actor_user_id(request: Request) -> str | None:
    raw = (request.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw or len(raw) > 128:
        return None
    return raw


def _actor_missing_response() -> JSONResponse:
    return JSONResponse(
        {"status": "unauthorized", "detail": "missing_actor_user_id"},
        status_code=401,
    )


def _owner_mismatch_response() -> JSONResponse:
    return JSONResponse(
        {"status": "forbidden", "detail": "actor_owner_mismatch"},
        status_code=403,
    )


def _identity_error_response(error: HTTPException) -> JSONResponse:
    status = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        503: "unavailable",
    }.get(error.status_code, "error")
    return JSONResponse(
        {"status": status, "detail": str(error.detail)},
        status_code=error.status_code,
    )


async def _require_actor_for_user(
    request: Request,
    requested_user_id: str,
) -> tuple[JSONResponse | None, str | None]:
    actor = _actor_user_id(request)
    if not actor:
        return _actor_missing_response(), None

    actor_uuid = _parse_uuid(actor)
    requested_uuid = _parse_uuid(requested_user_id)
    if actor_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_actor_user_id"},
            status_code=400,
        ), None
    if requested_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_owner_user_id"},
            status_code=400,
        ), None
    if actor_uuid != requested_uuid:
        return _owner_mismatch_response(), str(requested_uuid)

    try:
        verified_actor = await require_actor(request, str(requested_uuid))
    except HTTPException as error:
        return _identity_error_response(error), None
    return None, verified_actor


async def _require_actor_for_thread(
    request: Request,
    thread_id: UUID,
    postgres: PostgresConnectionProvider,
) -> tuple[JSONResponse | None, str | None]:
    actor = _actor_user_id(request)
    if not actor:
        return _actor_missing_response(), None

    actor_uuid = _parse_uuid(actor)
    if actor_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_actor_user_id"},
            status_code=400,
        ), None

    try:
        verified_actor = await require_request_actor(request)
    except HTTPException as error:
        return _identity_error_response(error), None
    if _parse_uuid(verified_actor) != actor_uuid:
        return _owner_mismatch_response(), None

    async with postgres.owner_connection(actor_uuid) as connection:
        found = await thread_belongs_to_owner(
            connection,
            owner_user_id=actor_uuid,
            thread_id=thread_id,
        )
    if not found:
        return JSONResponse(
            {"status": "not_found", "detail": "thread_not_found"},
            status_code=404,
        ), None
    return None, str(actor_uuid)


def create_thread_lifecycle_router(
    postgres: PostgresConnectionProvider,
    *,
    title_client: Any | None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/threads/new")
    async def threads_new(body: NewThreadReq, req: Request):
        user_id_alias = (body.user_id or "").strip() or "anon"
        title = (body.title or "New chat").strip() or "New chat"
        actor_err, user_id = await _require_actor_for_user(
            req, user_id_alias
        )
        if actor_err:
            return actor_err

        async with postgres.owner_connection(user_id) as conn:
            row = await create_and_select_thread(
                conn,
                owner_user_id=user_id,
                title=title,
            )
        return {
            "thread_id": str(row["id"]),
            "title": row["title"],
            "updated_at": row["updated_at"].isoformat(),
        }

    @router.get("/threads/list/{user_id}")
    async def threads_list(
        user_id: str,
        req: Request,
    ):
        user_id_alias = (user_id or "").strip() or "anon"
        actor_err, owner_user_id = await _require_actor_for_user(
            req, user_id_alias
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(owner_user_id) as connection:
            rows = await list_visible_threads(
                connection,
                owner_user_id=owner_user_id,
            )
        return [
            {
                "thread_id": str(row["id"]),
                "title": row["title"],
                "updated_at": row["updated_at"].isoformat(),
                "pinned": bool(row["pinned"]),
                "pinned_at": (
                    row["pinned_at"].isoformat()
                    if row["pinned_at"]
                    else None
                ),
            }
            for row in rows
        ]

    @router.get("/threads/active/{user_id}")
    async def threads_active_get(
        user_id: str,
        req: Request,
    ):
        user_id_alias = (user_id or "").strip() or "anon"
        actor_err, owner_user_id = await _require_actor_for_user(
            req, user_id_alias
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(owner_user_id) as connection:
            selected = await get_active_thread_v1(
                connection,
                owner_user_id=owner_user_id,
            )
        return selected or {"thread_id": None}

    @router.post("/threads/active")
    async def threads_active_select(body: ActiveThreadReq, req: Request):
        user_id_alias = (body.user_id or "").strip() or "anon"
        actor_err, owner_user_id = await _require_actor_for_user(
            req, user_id_alias
        )
        if actor_err:
            return actor_err
        thread_id = _parse_uuid(body.thread_id)
        if thread_id is None:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_thread_id"},
                status_code=400,
            )
        async with postgres.owner_connection(owner_user_id) as connection:
            try:
                return await select_active_thread_v1(
                    connection,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                )
            except ActiveThreadSelectionV1Error as error:
                return JSONResponse(
                    {"status": "not_found", "detail": error.code},
                    status_code=404,
                )

    @router.delete("/threads/active/{user_id}")
    async def threads_active_clear(
        user_id: str,
        req: Request,
    ):
        user_id_alias = (user_id or "").strip() or "anon"
        actor_err, owner_user_id = await _require_actor_for_user(
            req, user_id_alias
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(owner_user_id) as connection:
            await clear_active_thread_v1(
                connection,
                owner_user_id=owner_user_id,
            )
        return {"status": "ok", "thread_id": None}

    @router.get("/threads/{thread_id}/messages")
    async def threads_messages(
        thread_id: str,
        req: Request,
        limit: int = 200,
    ):
        tid = _parse_uuid(thread_id)
        if not tid:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid thread_id"},
                status_code=400,
            )
        actor_err, actor_uid = await _require_actor_for_thread(
            req, tid, postgres
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(actor_uid) as conn:
            rows = await fetch_thread_message_rows(
                conn,
                owner_user_id=actor_uid,
                thread_id=tid,
                limit=limit,
            )
        out = []
        for r in rows:
            src = r["source"] or ""
            message = {
                "id": str(r["id"]),
                "role": "assistant" if "assistant" in src else "user",
                "content": r["text"],
                "created_at": r["created_at"].isoformat(),
                "attachments": r["attachments"] or [],
            }
            if src == WEB_ASSISTANT_SOURCE:
                cited = r["cited_sources"] or []
                admitted = r["admitted_sources"] or []
                if isinstance(cited, str):
                    cited = json.loads(cited)
                if isinstance(admitted, str):
                    admitted = json.loads(admitted)
                message.update(
                    {
                        "web_search": True,
                        "trusted_web_sources": cited,
                        "trusted_web_admitted_sources": admitted,
                    }
                )
            out.append(message)
        return out

    @router.post("/threads/{thread_id}/rename")
    async def threads_rename(
        thread_id: str,
        body: RenameThreadReq,
        req: Request,
    ):
        tid = _parse_uuid(thread_id)
        if not tid:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid thread_id"},
                status_code=400,
            )
        actor_err, actor_uid = await _require_actor_for_thread(
            req, tid, postgres
        )
        if actor_err:
            return actor_err
        title = (body.title or "").strip() or "New chat"
        async with postgres.owner_connection(actor_uid) as connection:
            if body.title_source == "automatic":
                return JSONResponse(
                    {
                        "status": "conflict",
                        "detail": "automatic_title_is_backend_owned",
                    },
                    status_code=409,
                )
            updated = await rename_thread_manual(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
                title=title,
            )
        if not updated:
            return JSONResponse(
                {"status": "not_found", "detail": "thread not found"},
                status_code=404,
            )
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": updated["title"],
            "title_source": updated["title_source"],
            "updated": True,
        }

    @router.post("/threads/{thread_id}/pin")
    async def threads_pin(
        thread_id: str,
        body: PinThreadReq,
        req: Request,
    ):
        tid = _parse_uuid(thread_id)
        if not tid:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_thread_id"},
                status_code=400,
            )
        actor_err, actor_uid = await _require_actor_for_thread(
            req, tid, postgres
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(actor_uid) as connection:
            updated = await set_thread_pinned(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
                pinned=body.pinned,
            )
        if not updated:
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )
        pinned_at = updated["pinned_at"]
        return {
            "status": "ok",
            "thread_id": str(tid),
            "pinned": pinned_at is not None,
            "pinned_at": pinned_at.isoformat() if pinned_at else None,
        }

    @router.post("/threads/{thread_id}/auto-title")
    async def threads_auto_title(thread_id: str, req: Request):
        tid = _parse_uuid(thread_id)
        if not tid:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_thread_id"},
                status_code=400,
            )
        actor_err, actor_uid = await _require_actor_for_thread(
            req, tid, postgres
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(actor_uid) as connection:
            current = await fetch_thread_title_state(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
            )
            if not current:
                return JSONResponse(
                    {"status": "not_found", "detail": "thread_not_found"},
                    status_code=404,
                )
            if current["title_source"] != "placeholder":
                return {
                    "status": "ok",
                    "thread_id": str(tid),
                    "title": current["title"],
                    "title_source": current["title_source"],
                    "updated": False,
                    "skipped": f"{current['title_source']}_title_preserved",
                }
            transcript = await fetch_thread_title_transcript(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
            )

        exchange = select_first_meaningful_exchange(transcript)
        if exchange is None:
            return {
                "status": "ok",
                "thread_id": str(tid),
                "title": current["title"],
                "title_source": "placeholder",
                "updated": False,
                "skipped": "no_meaningful_exchange",
            }
        if title_client is None:
            return JSONResponse(
                {"status": "unavailable", "detail": "title_generation_unavailable"},
                status_code=503,
            )
        title_model = (os.getenv("THREAD_TITLE_MODEL") or "gpt-4.1-mini").strip()
        try:
            title = await asyncio.wait_for(
                asyncio.to_thread(
                    generate_semantic_title,
                    title_client,
                    title_model,
                    exchange[0],
                    exchange[1],
                ),
                timeout=12.0,
            )
        except Exception:
            print(
                "[threads_auto_title] generation unavailable",
                str(getattr(req.state, "request_id", "")),
            )
            return JSONResponse(
                {"status": "unavailable", "detail": "title_generation_unavailable"},
                status_code=503,
            )
        if not title:
            return {
                "status": "ok",
                "thread_id": str(tid),
                "title": current["title"],
                "title_source": "placeholder",
                "updated": False,
                "skipped": "no_meaningful_exchange",
            }
        async with postgres.owner_connection(actor_uid) as connection:
            updated = await update_thread_automatic_title(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
                title=title,
            )
            if updated:
                return {
                    "status": "ok",
                    "thread_id": str(tid),
                    "title": updated["title"],
                    "title_source": updated["title_source"],
                    "updated": True,
                }
            current = await fetch_thread_title_state(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
            )
        if not current:
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": current["title"],
            "title_source": current["title_source"],
            "updated": False,
            "skipped": f"{current['title_source']}_title_preserved",
        }

    @router.post("/threads/{thread_id}/archive")
    async def threads_archive(thread_id: str, req: Request):
        tid = _parse_uuid(thread_id)
        if not tid:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid thread_id"},
                status_code=400,
            )
        actor_err, actor_uid = await _require_actor_for_thread(
            req, tid, postgres
        )
        if actor_err:
            return actor_err
        async with postgres.owner_connection(actor_uid) as connection:
            await archive_thread(
                connection,
                owner_user_id=actor_uid,
                thread_id=tid,
            )
        return {"status": "ok", "thread_id": str(tid), "archived": True}

    return router


__all__ = [
    "ActiveThreadReq",
    "NewThreadReq",
    "PinThreadReq",
    "RenameThreadReq",
    "create_thread_lifecycle_router",
]
