from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any


class RawMemoryOwnershipError(RuntimeError):
    """Raised when a raw-memory point is missing or violates canonical ownership."""


def canonical_owner_user_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RawMemoryOwnershipError("missing owner_user_id")
    try:
        return str(uuid.UUID(raw))
    except Exception as exc:
        raise RawMemoryOwnershipError("invalid owner_user_id") from exc


def owned_raw_payload(owner_user_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with immutable canonical and compatibility owner fields."""
    owner = canonical_owner_user_id(owner_user_id)
    result = dict(payload)

    existing_owner = result.get("owner_user_id")
    if existing_owner not in (None, ""):
        if canonical_owner_user_id(existing_owner) != owner:
            raise RawMemoryOwnershipError("owner_user_id mismatch")

    existing_legacy = result.get("user_id")
    if existing_legacy not in (None, ""):
        if canonical_owner_user_id(existing_legacy) != owner:
            raise RawMemoryOwnershipError("legacy user_id mismatch")

    result["owner_user_id"] = owner
    result["user_id"] = owner
    return result


def assert_raw_payload_owner(payload: Mapping[str, Any], owner_user_id: Any) -> None:
    owner = canonical_owner_user_id(owner_user_id)
    point_owner = canonical_owner_user_id(payload.get("owner_user_id"))
    legacy_owner = canonical_owner_user_id(payload.get("user_id"))
    if point_owner != owner or legacy_owner != owner:
        raise RawMemoryOwnershipError("raw-memory point owner mismatch")


def assert_raw_points_owner(points: Iterable[Any], owner_user_id: Any) -> None:
    owner = canonical_owner_user_id(owner_user_id)
    for point in points:
        payload = getattr(point, "payload", None) or {}
        assert_raw_payload_owner(payload, owner)
