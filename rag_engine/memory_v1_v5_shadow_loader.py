from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Sequence

import asyncpg

from .memory_v1_v5_shadow_retrieval import V5ShadowRetrievalError


def _claim_ids(values: Sequence[str | uuid.UUID]) -> list[uuid.UUID]:
    if not 1 <= len(values) <= 100:
        raise V5ShadowRetrievalError("claim_ids must contain 1 to 100 values")
    parsed: list[uuid.UUID] = []
    for value in values:
        try:
            parsed.append(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise V5ShadowRetrievalError("claim_ids must contain UUIDs") from exc
    if len(set(parsed)) != len(parsed):
        raise V5ShadowRetrievalError("claim_ids must be unique")
    return parsed


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise V5ShadowRetrievalError(f"{field} must be a JSON object")


async def load_v5_shadow_claims(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    claim_ids: Sequence[str | uuid.UUID],
) -> list[dict[str, Any]]:
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ShadowRetrievalError("actor_user_id must be a UUID") from exc
    requested = _claim_ids(claim_ids)
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(actor),
        )
        rows = await conn.fetch(
            "SELECT * FROM memory.read_v5_shadow_claims($1::uuid[])",
            requested,
        )
    records: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        if uuid.UUID(str(value["owner_user_id"])) != actor:
            raise V5ShadowRetrievalError("database returned a cross-owner V5 claim")
        value["metadata"] = _json_object(value["metadata"], "metadata")
        value["retrieval_policy"] = _json_object(
            value["retrieval_policy"],
            "retrieval_policy",
        )
        value["evidence_by_stance"] = _json_object(
            value["evidence_by_stance"],
            "evidence_by_stance",
        )
        records.append(value)
    return records
