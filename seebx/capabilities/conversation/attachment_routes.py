from __future__ import annotations

"""Owner-bound HTTP contract for conversation attachment CRUD."""

import hashlib
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.adapters.conversation_attachments import (
    ConversationAttachmentPostgresStore,
)
from seebx.capabilities.conversation.attachments import (
    MAX_ATTACHMENT_BYTES,
    SUPPORTED_ATTACHMENT_MEDIA_TYPES,
)
from seebx.contracts.identifiers import CanonicalJsonUUID
from seebx.core.identity import require_actor


router = APIRouter()


class ChatAttachmentCreateReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: CanonicalJsonUUID
    thread_id: CanonicalJsonUUID
    filename: str = Field(min_length=1, max_length=160)
    media_type: Literal["text/plain", "text/markdown"]
    content: str = Field(min_length=1)
    content_sha256: str

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        name = value.strip()
        if not name or any(
            ch in name for ch in ("/", "\\", "\x00", "\r", "\n")
        ):
            raise ValueError("invalid filename")
        return name

    @field_validator("content_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if len(value) != 64 or any(
            ch not in "0123456789abcdef" for ch in value
        ):
            raise ValueError("invalid content hash")
        return value

    @model_validator(mode="after")
    def exact_content(self) -> "ChatAttachmentCreateReq":
        raw = self.content.encode("utf-8")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds byte limit")
        if hashlib.sha256(raw).hexdigest() != self.content_sha256:
            raise ValueError("attachment content hash mismatch")
        return self


class ChatAttachmentOwnerReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: CanonicalJsonUUID


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except Exception:
        return None


def _store() -> ConversationAttachmentPostgresStore:
    return ConversationAttachmentPostgresStore.from_environment()


@router.post("/attachments")
async def create_chat_attachment(
    body: ChatAttachmentCreateReq,
    req: Request,
):
    owner = UUID(await require_actor(req, str(body.user_id)))
    raw = body.content.encode("utf-8")
    if body.media_type not in SUPPORTED_ATTACHMENT_MEDIA_TYPES:
        return JSONResponse(
            {
                "status": "bad_request",
                "detail": "unsupported_attachment_type",
            },
            status_code=400,
        )
    store = _store()
    async with store.owner_connection(owner) as connection:
        row = await store.create_attachment(
            connection,
            attachment_id=uuid4(),
            owner_user_id=owner,
            thread_id=body.thread_id,
            filename=body.filename,
            media_type=body.media_type,
            content=body.content,
            content_sha256=body.content_sha256,
            byte_size=len(raw),
        )
    if row is None:
        return JSONResponse(
            {"status": "not_found", "detail": "thread_not_found"},
            status_code=404,
        )
    return {
        "status": "ok",
        "attachment": {
            "id": str(row["id"]),
            "thread_id": str(row["thread_id"]),
            "filename": row["filename"],
            "media_type": row["media_type"],
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "processing_status": row["status"],
            "created_at": row["created_at"].isoformat(),
        },
    }


@router.get("/attachments/{attachment_id}")
async def get_chat_attachment_status(
    attachment_id: str,
    user_id: str,
    req: Request,
):
    aid = _parse_uuid(attachment_id)
    if aid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_attachment_id"},
            status_code=400,
        )
    owner = UUID(await require_actor(req, user_id))
    store = _store()
    async with store.owner_connection(owner) as connection:
        row = await store.fetch_attachment_status(
            connection,
            attachment_id=aid,
            owner_user_id=owner,
        )
    if row is None:
        return JSONResponse(
            {"status": "not_found", "detail": "attachment_not_found"},
            status_code=404,
        )
    return {
        "status": "ok",
        "attachment": {
            "id": str(row["id"]),
            "thread_id": str(row["thread_id"]),
            "message_id": (
                str(row["message_id"]) if row["message_id"] else None
            ),
            "filename": row["filename"],
            "media_type": row["media_type"],
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "processing_status": row["status"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "deleted_at": (
                row["deleted_at"].isoformat()
                if row["deleted_at"]
                else None
            ),
        },
    }


@router.post("/attachments/{attachment_id}/retry")
async def retry_chat_attachment(
    attachment_id: str,
    body: ChatAttachmentOwnerReq,
    req: Request,
):
    aid = _parse_uuid(attachment_id)
    if aid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_attachment_id"},
            status_code=400,
        )
    owner = UUID(await require_actor(req, str(body.user_id)))
    store = _store()
    async with store.owner_connection(owner) as connection:
        current = await store.fetch_retry_candidate(
            connection,
            attachment_id=aid,
            owner_user_id=owner,
        )
        if current is None:
            return JSONResponse(
                {
                    "status": "not_found",
                    "detail": "attachment_not_found",
                },
                status_code=404,
            )
        raw = (current["content"] or "").encode("utf-8")
        next_status = (
            "ready"
            if len(raw) == current["byte_size"]
            and hashlib.sha256(raw).hexdigest()
            == current["content_sha256"]
            else "error"
        )
        row = await store.update_retry_status(
            connection,
            attachment_id=aid,
            owner_user_id=owner,
            status=next_status,
        )
    code = 200 if row["status"] == "ready" else 409
    return JSONResponse(
        {
            "status": "ok" if code == 200 else "error",
            "attachment_id": str(row["id"]),
            "processing_status": row["status"],
        },
        status_code=code,
    )


@router.delete("/attachments/{attachment_id}")
async def delete_chat_attachment(
    attachment_id: str,
    user_id: str,
    req: Request,
):
    aid = _parse_uuid(attachment_id)
    if aid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_attachment_id"},
            status_code=400,
        )
    owner = UUID(await require_actor(req, user_id))
    store = _store()
    async with store.owner_connection(owner) as connection:
        row = await store.delete_attachment(
            connection,
            attachment_id=aid,
            owner_user_id=owner,
        )
    if row is None:
        return JSONResponse(
            {"status": "not_found", "detail": "attachment_not_found"},
            status_code=404,
        )
    return {
        "status": "ok",
        "attachment_id": str(row["id"]),
        "deleted_at": row["deleted_at"].isoformat(),
    }


__all__ = [
    "ChatAttachmentCreateReq",
    "ChatAttachmentOwnerReq",
    "router",
]
