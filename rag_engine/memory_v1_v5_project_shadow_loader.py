from __future__ import annotations

import uuid
from typing import Any

import asyncpg


class V5ProjectShadowLoaderError(RuntimeError):
    pass


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ProjectShadowLoaderError(f"{field} must be a UUID") from exc


async def load_v5_shadow_project_knowledge(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    thread_id: str | uuid.UUID,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    actor = _uuid(actor_user_id, "actor_user_id")
    thread = _uuid(thread_id, "thread_id")
    if isinstance(limit, bool) or not 1 <= int(limit) <= 8:
        raise V5ProjectShadowLoaderError("limit must be between 1 and 8")

    async with conn.transaction(readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(actor),
        )
        rows = await conn.fetch(
            "SELECT * FROM memory.read_v5_shadow_project_knowledge($1,$2)",
            thread,
            int(limit),
        )

    records: list[dict[str, Any]] = []
    expected_scope: tuple[str, str, str] | None = None
    for row in rows:
        value = dict(row)
        if _uuid(value.get("owner_user_id"), "owner_user_id") != actor:
            raise V5ProjectShadowLoaderError("database returned a cross-owner row")
        if _uuid(value.get("thread_id"), "thread_id") != thread:
            raise V5ProjectShadowLoaderError("database returned a cross-thread row")
        scope = (
            str(_uuid(value.get("project_id"), "project_id")),
            str(_uuid(value.get("component_id"), "component_id")),
            str(value.get("component_key") or ""),
        )
        if not scope[2]:
            raise V5ProjectShadowLoaderError("component_key is absent")
        if expected_scope is None:
            expected_scope = scope
        elif scope != expected_scope:
            raise V5ProjectShadowLoaderError("database returned mixed project scopes")
        evidence_ids = [str(_uuid(item, "evidence_id")) for item in value.get("evidence_ids") or []]
        observation_ids = [
            str(_uuid(item, "observation_id"))
            for item in value.get("observation_ids") or []
        ]
        if not evidence_ids or not observation_ids:
            raise V5ProjectShadowLoaderError("governed provenance is absent")
        value["evidence_ids"] = evidence_ids
        value["observation_ids"] = observation_ids
        records.append(value)
    return records
