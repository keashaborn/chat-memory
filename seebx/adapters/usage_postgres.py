from __future__ import annotations

"""Content-free OpenAI usage persistence for conversation responses."""

from typing import Any
from uuid import UUID

from seebx.adapters.lifeswitch_openai_chat import OpenAIChatResponseV3
from seebx.adapters.openai_chat import OpenAIChatResponseV1


EVENT_SCHEMA_VERSION = 1
USAGE_HELPER = "openai_chat_completions_v1"
USAGE_WRITER_ROLE = "lifeswitch_usage_writer_v1"


class UsagePersistenceError(RuntimeError):
    pass


async def _set_writer_role(conn: Any) -> None:
    await conn.execute(f"set local role {USAGE_WRITER_ROLE}")


async def persist_openai_chat_usage(
    conn: Any,
    *,
    owner_user_id: UUID,
    answer_id: UUID,
    source_channel: str,
    provider_response: OpenAIChatResponseV1 | OpenAIChatResponseV3,
) -> None:
    try:
        if isinstance(provider_response, OpenAIChatResponseV1):
            response = OpenAIChatResponseV1.model_validate_json(
                provider_response.model_dump_json()
            )
        elif isinstance(provider_response, OpenAIChatResponseV3):
            response = OpenAIChatResponseV3.model_validate_json(
                provider_response.model_dump_json()
            )
        else:
            raise TypeError("unsupported provider response")
    except Exception:
        raise UsagePersistenceError("OpenAI usage response is invalid") from None

    if source_channel not in {"chat", "voice"}:
        raise UsagePersistenceError("invalid source channel")

    try:
        async with conn.transaction():
            await _set_writer_role(conn)
            await conn.execute(
                "select set_config('app.user_id',$1,true)",
                str(owner_user_id),
            )
            inserted = await conn.fetchrow(
                """
                insert into lifeswitch_usage.ai_usage_event_v1 (
                  owner_user_id, answer_id, provider, operation, helper,
                  source_channel, provider_response_id, requested_model,
                  returned_model, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_output_tokens, total_tokens,
                  idempotency_key, event_schema_version
                )
                values (
                  $1,$2,'openai','chat_response',$3,$4,$5,$6,$7,$8,$9,$10,
                  $11,$12,$13,$14
                )
                on conflict (provider, provider_response_id) do nothing
                returning
                  owner_user_id, answer_id, source_channel,
                  provider_response_id, requested_model, returned_model,
                  input_tokens, cached_input_tokens, output_tokens,
                  reasoning_output_tokens, total_tokens, helper,
                  idempotency_key, event_schema_version
                """,
                owner_user_id,
                answer_id,
                USAGE_HELPER,
                source_channel,
                response.response_id,
                response.requested_model,
                response.model,
                response.provider_input_tokens,
                response.provider_cached_input_tokens,
                response.provider_output_tokens,
                response.provider_reasoning_output_tokens,
                response.provider_total_tokens,
                str(answer_id),
                EVENT_SCHEMA_VERSION,
            )
            if inserted is not None:
                return

            existing = await conn.fetchrow(
                """
                select
                  owner_user_id, answer_id, source_channel,
                  provider_response_id, requested_model, returned_model,
                  input_tokens, cached_input_tokens, output_tokens,
                  reasoning_output_tokens, total_tokens, helper,
                  idempotency_key, event_schema_version
                from lifeswitch_usage.ai_usage_event_v1
                where provider='openai' and provider_response_id=$1
                """,
                response.response_id,
            )
            expected = {
                "owner_user_id": owner_user_id,
                "answer_id": answer_id,
                "source_channel": source_channel,
                "provider_response_id": response.response_id,
                "requested_model": response.requested_model,
                "returned_model": response.model,
                "input_tokens": response.provider_input_tokens,
                "cached_input_tokens": response.provider_cached_input_tokens,
                "output_tokens": response.provider_output_tokens,
                "reasoning_output_tokens": response.provider_reasoning_output_tokens,
                "total_tokens": response.provider_total_tokens,
                "helper": USAGE_HELPER,
                "idempotency_key": str(answer_id),
                "event_schema_version": EVENT_SCHEMA_VERSION,
            }
            if existing is None or dict(existing) != expected:
                raise UsagePersistenceError("provider response usage conflict")
    except UsagePersistenceError:
        raise
    except Exception:
        raise UsagePersistenceError("OpenAI usage persistence failed") from None
