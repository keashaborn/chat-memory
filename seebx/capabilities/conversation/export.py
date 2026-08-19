from __future__ import annotations

"""Canonical, complete owner export across Postgres and Zep."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID

from seebx.adapters.conversation_export import (
    ConversationExportRepositoryError,
)
from seebx.adapters.zep_cloud import ZepConfigurationError


EXPORT_SCHEMA_VERSION = "lifeswitch-conversation-memory-export-v1"
MAX_EXPORT_BYTES = 128 * 1024 * 1024


class ConversationExportRepository(Protocol):
    async def export_owner(self, owner_user_id: UUID) -> Mapping[str, Any]: ...


class OwnerMemoryExportRuntime(Protocol):
    async def export_owner_memory(
        self,
        owner_user_id: UUID,
    ) -> Mapping[str, Any]: ...


class ConversationExportError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ConversationExportArtifact:
    body: bytes
    sha256: str
    generated_at: str


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class ConversationExportService:
    def __init__(
        self,
        *,
        repository: ConversationExportRepository,
        memory_runtime: OwnerMemoryExportRuntime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._memory_runtime = memory_runtime
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def build(self, owner_user_id: UUID) -> ConversationExportArtifact:
        if not isinstance(owner_user_id, UUID):
            raise ConversationExportError(
                "invalid_export_owner",
                status_code=400,
            )
        try:
            conversation = await self._repository.export_owner(owner_user_id)
        except ConversationExportRepositoryError as error:
            status = (
                413
                if str(error) == "conversation_export_size_limit_exceeded"
                else 503
            )
            raise ConversationExportError(str(error), status_code=status) from None
        try:
            memory = await self._memory_runtime.export_owner_memory(owner_user_id)
        except ZepConfigurationError as error:
            raise ConversationExportError(
                str(error),
                status_code=503,
            ) from None
        except Exception:
            raise ConversationExportError(
                "zep_export_failed",
                status_code=503,
            ) from None

        if conversation.get("status") != "complete" or memory.get("status") not in {
            "complete",
            "absent",
        }:
            raise ConversationExportError(
                "conversation_export_incomplete",
                status_code=503,
            )
        generated = self._clock()
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        generated_at = generated.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        payload = _json_value(
            {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "generated_at": generated_at,
                "owner_user_id": owner_user_id,
                "completeness": {
                    "conversation_postgres": "complete",
                    "zep_memory": memory["status"],
                },
                "conversation": conversation,
                "memory": memory,
                "excluded_internal_data": [
                    "zep_sync_retry_state",
                    "service_credentials",
                    "provider_token_usage_accounting",
                ],
            }
        )
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(body) > MAX_EXPORT_BYTES:
            raise ConversationExportError(
                "conversation_export_size_limit_exceeded",
                status_code=413,
            )
        return ConversationExportArtifact(
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            generated_at=generated_at,
        )


__all__ = [
    "ConversationExportArtifact",
    "ConversationExportError",
    "ConversationExportService",
    "EXPORT_SCHEMA_VERSION",
]
