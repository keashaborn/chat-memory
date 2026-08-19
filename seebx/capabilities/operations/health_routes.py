from __future__ import annotations

"""Operational liveness and readiness HTTP boundary."""

import time
from typing import Protocol

from fastapi import APIRouter
from fastapi.responses import JSONResponse


class ReadinessProvider(Protocol):
    async def readiness_value(self) -> object: ...


def create_operational_health_router(
    postgres: ReadinessProvider,
    *,
    zep_prompt_mode: str,
) -> APIRouter:
    if not str(zep_prompt_mode or "").strip():
        raise ValueError("zep_prompt_mode_required")

    router = APIRouter()

    @router.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "time": time.time(),
            "memory": {
                "provider": "zep",
                "prompt_mode": zep_prompt_mode,
                "chat_history_store": "postgres",
                "chat_deletion_retains_memory": True,
                "full_erasure_route": "/memory/chat-and-zep/clear",
                "retired_governed_memory": {
                    "capture": "disabled",
                    "response_fallback": "disabled",
                    "lifecycle_commands": "disabled",
                    "erasure_proxy": "disabled",
                    "postgres_access": "disabled",
                },
            },
        }

    @router.get("/readyz", include_in_schema=False)
    async def readyz():
        try:
            if await postgres.readiness_value() != 1:
                raise RuntimeError("postgres select 1 failed")
        except Exception as error:
            return JSONResponse(
                {"ok": False, "postgres": str(error)},
                status_code=503,
            )
        return {"ok": True, "postgres": True}

    return router


__all__ = ["ReadinessProvider", "create_operational_health_router"]
