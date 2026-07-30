from __future__ import annotations

"""Exact-record provider for the optional assistant-name preference."""

import asyncio
import json
import os
import threading
import uuid
from typing import Any
from uuid import UUID

from qdrant_client import QdrantClient

from rag_engine.assistant_name_preference_v1 import (
    AssistantNamePreferenceError,
    AssistantNamePreferenceV1,
    normalize_assistant_name_v1,
)
from rag_engine.qdrant_compat import make_qdrant_client
from rag_engine.raw_memory_ownership import (
    assert_raw_payload_owner,
    canonical_owner_user_id,
)


USER_PREFERENCES_MARKER = "RESSE_USER_PREFERENCES_V1\n"
USER_PREFERENCES_VANTAGE_ID = "user_global"
USER_PREFERENCES_KIND = "user_instructions"
USER_PREFERENCES_TOPIC_KEY = "__singleton__"

_CLIENT_LOCK = threading.Lock()
_QDRANT_CLIENT: QdrantClient | None = None


def assistant_name_card_id_v1(owner_user_id: UUID | str) -> UUID:
    owner = canonical_owner_user_id(owner_user_id)
    return uuid.uuid5(
        uuid.NAMESPACE_DNS,
        (
            f"{owner}|{USER_PREFERENCES_VANTAGE_ID}|"
            f"{USER_PREFERENCES_KIND}|{USER_PREFERENCES_TOPIC_KEY}"
        ),
    )


def parse_assistant_name_preference_v1(
    *,
    owner_user_id: UUID,
    source_card_id: UUID,
    text: str,
) -> AssistantNamePreferenceV1:
    if not text.startswith(USER_PREFERENCES_MARKER):
        name = None
    else:
        try:
            payload = json.loads(text[len(USER_PREFERENCES_MARKER) :])
        except Exception as exc:
            raise AssistantNamePreferenceError(
                "user preference record is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AssistantNamePreferenceError(
                "user preference record must be an object"
            )
        name = normalize_assistant_name_v1(payload.get("assistant_name"))
    return AssistantNamePreferenceV1(
        owner_user_id=owner_user_id,
        source_card_id=source_card_id,
        name=name,
    )


def _qdrant_client() -> QdrantClient:
    global _QDRANT_CLIENT
    if _QDRANT_CLIENT is not None:
        return _QDRANT_CLIENT
    with _CLIENT_LOCK:
        if _QDRANT_CLIENT is None:
            qdrant_url = (os.getenv("QDRANT_URL") or "http://127.0.0.1:6333").strip()
            qdrant_api_key = (os.getenv("QDRANT_API_KEY") or "").strip()
            options: dict[str, Any] = {"url": qdrant_url, "timeout": 5.0}
            if qdrant_api_key:
                options["api_key"] = qdrant_api_key
            _QDRANT_CLIENT = make_qdrant_client(**options)
    return _QDRANT_CLIENT


def _load_assistant_name_preference_sync(
    owner_user_id: UUID,
) -> AssistantNamePreferenceV1:
    owner = UUID(canonical_owner_user_id(owner_user_id))
    card_id = assistant_name_card_id_v1(owner)
    points = _qdrant_client().retrieve(
        collection_name="memory_raw",
        ids=[str(card_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return AssistantNamePreferenceV1(
            owner_user_id=owner,
            source_card_id=card_id,
            name=None,
        )
    if len(points) != 1 or UUID(str(points[0].id)) != card_id:
        raise AssistantNamePreferenceError("unexpected user preference record identity")
    payload = points[0].payload or {}
    assert_raw_payload_owner(payload, owner)
    expected = {
        "source": "memory_card",
        "vantage_id": USER_PREFERENCES_VANTAGE_ID,
        "kind": USER_PREFERENCES_KIND,
        "topic_key": USER_PREFERENCES_TOPIC_KEY,
    }
    if any(str(payload.get(key) or "") != value for key, value in expected.items()):
        raise AssistantNamePreferenceError("user preference record scope mismatch")
    return parse_assistant_name_preference_v1(
        owner_user_id=owner,
        source_card_id=card_id,
        text=str(payload.get("text") or ""),
    )


async def load_assistant_name_preference_v1(
    owner_user_id: UUID,
) -> AssistantNamePreferenceV1:
    return await asyncio.to_thread(_load_assistant_name_preference_sync, owner_user_id)


__all__ = [
    "assistant_name_card_id_v1",
    "load_assistant_name_preference_v1",
    "parse_assistant_name_preference_v1",
]
