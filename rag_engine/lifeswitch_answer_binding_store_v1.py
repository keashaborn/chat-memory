from __future__ import annotations

"""Restricted append-only persistence for LifeSwitch answer bindings."""

import json
from typing import Any

import asyncpg

from seebx.capabilities.conversation.lifeswitch_answer_binding import (
    FinalAnswerLifeSwitchBindingV1,
)


LIFESWITCH_BINDING_WRITER_ROLE = "lifeswitch_chat_binding_writer_v1"


class LifeSwitchBindingPersistenceError(RuntimeError):
    pass


async def persist_lifeswitch_binding_on_connection_v1(
    conn: Any,
    binding: FinalAnswerLifeSwitchBindingV1,
) -> None:
    """Persist one binding within the caller's existing transaction."""

    value = FinalAnswerLifeSwitchBindingV1.model_validate_json(
        binding.model_dump_json()
    )
    await conn.execute(
        "select set_config('app.lifeswitch_owner_id', $1, true)",
        str(value.owner_user_id),
    )
    await conn.execute(f"set local role {LIFESWITCH_BINDING_WRITER_ROLE}")
    await conn.execute(
        """
        insert into lifeswitch_chat.final_answer_lifeswitch_binding_v1(
          answer_id,
          authenticated_actor_user_id,
          owner_user_id,
          thread_id,
          request_id_sha256,
          conversation_snapshot_sha256,
          source_assembly_sha256,
          envelope_sha256,
          rendered_content_sha256,
          answer_model_exposed,
          record_count,
          rendered_tokens,
          record_refs,
          created_at,
          binding_manifest_sha256
        ) values (
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15
        )
        on conflict (answer_id) do nothing
        """,
        value.answer_id,
        value.authenticated_actor_user_id,
        value.owner_user_id,
        value.thread_id,
        value.request_id_sha256,
        value.conversation_snapshot_sha256,
        value.source_assembly_sha256,
        value.envelope_sha256,
        value.rendered_content_sha256,
        value.answer_model_exposed,
        value.record_count,
        value.rendered_tokens,
        json.dumps(
            [item.model_dump(mode="json") for item in value.record_refs],
            separators=(",", ":"),
            sort_keys=True,
        ),
        value.created_at,
        value.binding_manifest_sha256,
    )
    stored = await conn.fetchval(
        """
        select binding_manifest_sha256
        from lifeswitch_chat.final_answer_lifeswitch_binding_v1
        where answer_id = $1 and owner_user_id = $2
        """,
        value.answer_id,
        value.owner_user_id,
    )
    if stored != value.binding_manifest_sha256:
        raise ValueError("existing LifeSwitch binding differs")
    await conn.execute("reset role")


class PostgresLifeSwitchBindingStoreV1:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def persist(self, binding: FinalAnswerLifeSwitchBindingV1) -> None:
        try:
            value = FinalAnswerLifeSwitchBindingV1.model_validate_json(
                binding.model_dump_json()
            )
            async with self._pool.acquire() as conn:
                async with conn.transaction(isolation="serializable"):
                    await persist_lifeswitch_binding_on_connection_v1(conn, value)
        except LifeSwitchBindingPersistenceError:
            raise
        except Exception:
            raise LifeSwitchBindingPersistenceError(
                "LifeSwitch answer binding persistence failed"
            ) from None


__all__ = [
    "LifeSwitchBindingPersistenceError",
    "PostgresLifeSwitchBindingStoreV1",
    "persist_lifeswitch_binding_on_connection_v1",
]
