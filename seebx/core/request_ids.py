"""Canonical content-free request-correlation identifiers."""

from __future__ import annotations

import uuid

from fastapi import Request


MAX_REQUEST_ID_LENGTH = 128


def sanitize_request_id(raw: object | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > MAX_REQUEST_ID_LENGTH:
        return None
    return value


def get_request_id(request: Request) -> str:
    request_id = sanitize_request_id(
        request.headers.get("x-request-id")
    ) or sanitize_request_id(request.headers.get("x-correlation-id"))
    return request_id or str(uuid.uuid4())


__all__ = [
    "MAX_REQUEST_ID_LENGTH",
    "get_request_id",
    "sanitize_request_id",
]
