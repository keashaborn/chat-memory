from __future__ import annotations

"""Content-free OpenAI usage ledger and owner-approved aggregate reporting."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.openai_chat_provider_v1 import OpenAIChatResponseV1


USAGE_SCHEMA = "admin_usage_summary_v1"
ALLOWED_WINDOWS = frozenset({0, 7, 30, 90})
MAX_TARGET_USERS = 100


class UsageLedgerError(RuntimeError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AdminUsageSummaryRequestV1(_StrictModel):
    target_user_ids: list[UUID] = Field(min_length=1, max_length=MAX_TARGET_USERS)
    window_days: int = Field(default=30)

    @field_validator("target_user_ids")
    @classmethod
    def unique_targets(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("target_user_ids must be unique")
        return value

    @field_validator("window_days")
    @classmethod
    def supported_window(cls, value: int) -> int:
        if value not in ALLOWED_WINDOWS:
            raise ValueError("window_days must be 0, 7, 30, or 90")
        return value


def _as_int(value: Any) -> int:
    return int(value or 0)


async def persist_openai_chat_usage_v1(
    conn: Any,
    *,
    owner_user_id: UUID,
    answer_id: UUID,
    source_channel: str,
    provider_response: OpenAIChatResponseV1,
) -> None:
    if source_channel not in {"chat", "voice"}:
        raise UsageLedgerError("invalid source channel")
    try:
        response = OpenAIChatResponseV1.model_validate_json(
            provider_response.model_dump_json()
        )
        async with conn.transaction():
            await conn.execute(
                "select set_config('app.user_id',$1,true)",
                str(owner_user_id),
            )
            inserted = await conn.fetchrow(
                """
                insert into lifeswitch_usage.ai_usage_event_v1 (
                  owner_user_id, answer_id, provider, operation,
                  source_channel, provider_response_id, requested_model,
                  returned_model, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_output_tokens, total_tokens
                )
                values (
                  $1,$2,'openai','chat_response',$3,$4,$5,$6,$7,$8,$9,$10,$11
                )
                on conflict (provider, provider_response_id) do nothing
                returning
                  owner_user_id, answer_id, source_channel,
                  provider_response_id, requested_model, returned_model,
                  input_tokens, cached_input_tokens, output_tokens,
                  reasoning_output_tokens, total_tokens
                """,
                owner_user_id,
                answer_id,
                source_channel,
                response.response_id,
                response.requested_model,
                response.model,
                response.provider_input_tokens,
                response.provider_cached_input_tokens,
                response.provider_output_tokens,
                response.provider_reasoning_output_tokens,
                response.provider_total_tokens,
            )
            if inserted is not None:
                return

            existing = await conn.fetchrow(
                """
                select
                  owner_user_id, answer_id, source_channel,
                  provider_response_id, requested_model, returned_model,
                  input_tokens, cached_input_tokens, output_tokens,
                  reasoning_output_tokens, total_tokens
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
                "reasoning_output_tokens": (
                    response.provider_reasoning_output_tokens
                ),
                "total_tokens": response.provider_total_tokens,
            }
            if existing is None or dict(existing) != expected:
                raise UsageLedgerError("provider response usage conflict")
    except UsageLedgerError:
        raise
    except Exception:
        raise UsageLedgerError("OpenAI usage persistence failed") from None


async def _summarize_target(
    conn: asyncpg.Connection,
    *,
    target_user_id: UUID,
    window_days: int,
) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute(
            "select set_config('app.user_id',$1,true)",
            str(target_user_id),
        )
        row = await conn.fetchrow(
            """
            with bounds as (
              select case
                when $2::integer = 0 then null::date
                else (current_date - ($2::integer - 1))
              end as start_day
            ),
            ai as (
              select
                count(*)::bigint as requests,
                coalesce(sum(input_tokens),0)::bigint as input_tokens,
                coalesce(sum(cached_input_tokens),0)::bigint
                  as cached_input_tokens,
                coalesce(sum(output_tokens),0)::bigint as output_tokens,
                coalesce(sum(reasoning_output_tokens),0)::bigint
                  as reasoning_output_tokens,
                coalesce(sum(total_tokens),0)::bigint as total_tokens,
                min(recorded_at) as first_recorded_at,
                max(recorded_at) as last_recorded_at
              from lifeswitch_usage.ai_usage_event_v1, bounds
              where owner_user_id=$1
                and (
                  bounds.start_day is null
                  or recorded_at >= bounds.start_day::timestamptz
                )
            ),
            nutrition as (
              select
                count(distinct d.day) filter (
                  where exists (
                    select 1
                    from lifeswitch_nutrition.nutrition_entry entry_exists
                    where entry_exists.nutrition_day_id=d.nutrition_day_id
                  )
                )::bigint as days_logged,
                count(distinct d.day) filter (
                  where d.completed_at is not null
                )::bigint as days_completed,
                count(e.nutrition_entry_id)::bigint as entries
              from lifeswitch_nutrition.nutrition_day d
              left join lifeswitch_nutrition.nutrition_entry e
                on e.nutrition_day_id=d.nutrition_day_id
              cross join bounds
              where d.owner_user_id=$1
                and (bounds.start_day is null or d.day >= bounds.start_day)
            ),
            strength as (
              select count(*)::bigint as sessions
              from (
                select s.training_session_id
                from lifeswitch_training.training_session_current_v s
                join lifeswitch_training.training_set_log set_log
                  on set_log.training_session_id=s.training_session_id
                 and set_log.owner_user_id=s.owner_user_id
                 and set_log.is_active=true
                cross join bounds
                where s.owner_user_id=$1
                  and s.is_active=true
                  and s.finished_at is not null
                  and (bounds.start_day is null or s.day >= bounds.start_day)
                group by s.training_session_id
              ) completed
            ),
            conditioning as (
              select count(*)::bigint as sessions
              from lifeswitch_training.conditioning_session_current_v c
              cross join bounds
              where c.owner_user_id=$1
                and c.is_active=true
                and (bounds.start_day is null or c.day >= bounds.start_day)
            )
            select
              ai.requests,
              ai.input_tokens,
              ai.cached_input_tokens,
              ai.output_tokens,
              ai.reasoning_output_tokens,
              ai.total_tokens,
              ai.first_recorded_at,
              ai.last_recorded_at,
              coalesce(nutrition.days_logged,0)::bigint as nutrition_days_logged,
              coalesce(nutrition.days_completed,0)::bigint
                as nutrition_days_completed,
              coalesce(nutrition.entries,0)::bigint as nutrition_entries,
              strength.sessions as strength_sessions,
              conditioning.sessions as conditioning_sessions
            from ai
            cross join nutrition
            cross join strength
            cross join conditioning
            """,
            target_user_id,
            window_days,
        )
    if row is None:
        raise UsageLedgerError("usage summary unavailable")
    return {
        "user_id": str(target_user_id),
        "ai": {
            "requests": _as_int(row["requests"]),
            "input_tokens": _as_int(row["input_tokens"]),
            "cached_input_tokens": _as_int(row["cached_input_tokens"]),
            "output_tokens": _as_int(row["output_tokens"]),
            "reasoning_output_tokens": _as_int(
                row["reasoning_output_tokens"]
            ),
            "total_tokens": _as_int(row["total_tokens"]),
            "first_recorded_at": (
                row["first_recorded_at"].isoformat()
                if row["first_recorded_at"]
                else None
            ),
            "last_recorded_at": (
                row["last_recorded_at"].isoformat()
                if row["last_recorded_at"]
                else None
            ),
        },
        "nutrition": {
            "days_logged": _as_int(row["nutrition_days_logged"]),
            "days_completed": _as_int(row["nutrition_days_completed"]),
            "entries": _as_int(row["nutrition_entries"]),
        },
        "training": {
            "strength_sessions": _as_int(row["strength_sessions"]),
            "conditioning_sessions": _as_int(
                row["conditioning_sessions"]
            ),
            "total_sessions": (
                _as_int(row["strength_sessions"])
                + _as_int(row["conditioning_sessions"])
            ),
        },
    }


async def build_admin_usage_summary_v1(
    *,
    dsn: str,
    request: AdminUsageSummaryRequestV1,
) -> dict[str, Any]:
    if not dsn:
        raise UsageLedgerError("usage database is unavailable")
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        users = [
            await _summarize_target(
                conn,
                target_user_id=target,
                window_days=request.window_days,
            )
            for target in request.target_user_ids
        ]
        tracking_values = [
            user["ai"]["first_recorded_at"]
            for user in users
            if user["ai"]["first_recorded_at"]
        ]
        return {
            "ok": True,
            "schema": USAGE_SCHEMA,
            "scope": "requested_users",
            "window_days": request.window_days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ai_tracking_started_at": (
                min(tracking_values) if tracking_values else None
            ),
            "users": users,
        }
    finally:
        await conn.close()


__all__ = [
    "AdminUsageSummaryRequestV1",
    "UsageLedgerError",
    "build_admin_usage_summary_v1",
    "persist_openai_chat_usage_v1",
]
