"""Canonical HTTP boundary for owner-scoped user transcript ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from seebx.adapters.conversation_persistence import (
    UserTranscriptPersistenceError,
    persist_user_transcript,
)
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.conversation.attachments import MAX_ATTACHMENT_COUNT
from seebx.capabilities.conversation.tagging import infer_transcript_tags
from seebx.core.identity import require_actor
from seebx.core.request_ids import sanitize_request_id
from seebx.core.voice_observability import voice_turn_id_from_request


def _parse_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def create_transcript_ingest_router(
    postgres: PostgresConnectionProvider,
) -> APIRouter:
    if postgres is None:
        raise ValueError("PostgreSQL provider is required")

    router = APIRouter()

    @router.post("/log")
    async def log_chat(req: Request):
        try:
            body: dict[str, Any] = await req.json()
        except Exception:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid json"},
                status_code=400,
            )

        no_store = body.get("no_store", False)
        if type(no_store) is not bool:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_no_store"},
                status_code=400,
            )
        text = body.get("text") or body.get("input") or ""
        source = body.get("source") or "frontend"
        if source == "frontend/identity" and text.startswith("FULL_NAME:"):
            return JSONResponse(
                {
                    "status": "retired",
                    "detail": "legacy_identity_memory_retired",
                },
                status_code=410,
            )

        user_id_alias = await require_actor(req, body.get("user_id") or "")
        if no_store:
            return {
                "status": "no_store",
                "detail": "transcript_and_memory_not_stored",
            }

        voice_turn_id_from_request(req)
        tags = body.get("tags") or []
        vantage_id = (body.get("vantage_id") or "").strip() or "default"
        request_id = sanitize_request_id(
            getattr(req.state, "request_id", None)
        ) or str(uuid.uuid4())
        raw_submission_id = body.get("submission_id")
        submission_id = (
            _parse_uuid(raw_submission_id)
            if raw_submission_id is not None
            else None
        )
        if raw_submission_id is not None and submission_id is None:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_submission_id"},
                status_code=400,
            )

        user_id = user_id_alias
        thread_id = None
        raw_thread_id = body.get("thread_id")
        if raw_thread_id:
            thread_id = _parse_uuid(raw_thread_id)

        raw_attachment_ids = body.get("attachment_ids") or []
        if (
            not isinstance(raw_attachment_ids, list)
            or len(raw_attachment_ids) > MAX_ATTACHMENT_COUNT
        ):
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_attachment_ids"},
                status_code=400,
            )
        attachment_ids: list[uuid.UUID] = []
        for raw_attachment_id in raw_attachment_ids:
            parsed_attachment_id = _parse_uuid(raw_attachment_id)
            if parsed_attachment_id is None:
                return JSONResponse(
                    {"status": "bad_request", "detail": "invalid_attachment_ids"},
                    status_code=400,
                )
            attachment_ids.append(parsed_attachment_id)
        if len(set(attachment_ids)) != len(attachment_ids) or (
            attachment_ids and thread_id is None
        ):
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_attachment_ids"},
                status_code=400,
            )

        if not text.strip():
            return {"status": "empty", "detail": "no text"}

        extra_tags = infer_transcript_tags(text, source=source)
        if extra_tags:
            existing = set(str(item) for item in tags)
            for tag in extra_tags:
                value = str(tag)
                if value not in existing:
                    tags.append(value)
                    existing.add(value)

        message_id = submission_id or uuid.uuid4()
        created_at = datetime.utcnow()

        try:
            async with postgres.owner_connection(user_id) as connection:
                result = await persist_user_transcript(
                    connection,
                    owner_user_id=uuid.UUID(user_id),
                    user_id_alias=user_id_alias,
                    source=source,
                    text=text,
                    tags=tags,
                    thread_id=thread_id,
                    vantage_id=vantage_id,
                    request_id=request_id,
                    message_id=message_id,
                    submission_id=submission_id,
                    created_at=created_at,
                    attachment_ids=attachment_ids,
                )
        except UserTranscriptPersistenceError as error:
            print("pg error:", error.__cause__ or error)
            return JSONResponse(
                {
                    "status": "conflict" if error.conflict else "unavailable",
                    "detail": error.code,
                },
                status_code=409 if error.conflict else 503,
            )
        except Exception as error:
            print("pg error:", error)
            return JSONResponse(
                {"status": "unavailable", "detail": "transcript_write_failed"},
                status_code=503,
            )

        response_payload = {
            "status": "ok",
            "id": str(result.message_id),
            "request_id": request_id,
        }
        if result.replayed:
            response_payload["replayed"] = True
        return response_payload

    return router


__all__ = ["create_transcript_ingest_router"]
