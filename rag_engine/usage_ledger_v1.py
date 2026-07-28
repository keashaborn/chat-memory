from __future__ import annotations

"""Content-free AI usage persistence and admin aggregate reporting."""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.openai_chat_provider_v1 import OpenAIChatResponseV1


LEGACY_USAGE_SCHEMA = "admin_usage_summary_v1"
OVERVIEW_SCHEMA = "admin_usage_overview_v1"
USERS_SCHEMA = "admin_usage_users_v1"
USER_DETAIL_SCHEMA = "admin_usage_user_detail_v1"
EVENT_SCHEMA_VERSION = 1
USAGE_HELPER = "openai_chat_completions_v1"
USAGE_WRITER_ROLE = "lifeswitch_usage_writer_v1"
USAGE_ADMIN_ROLE = "lifeswitch_usage_admin_v1"
ALLOWED_WINDOWS = frozenset({0, 7, 30, 90})
MAX_TARGET_USERS = 100
DEFAULT_USER_LIMIT = 25
MAX_USER_LIMIT = 50
MAX_CURSOR_LENGTH = 2_048
MAX_QUERY_LENGTH = 64
MAX_SYSTEM_WORKLOADS = 25
USER_ID_QUERY_PATTERN = re.compile(r"^[0-9a-f-]{1,36}$", re.IGNORECASE)
UsageSortV1 = Literal[
    "total_tokens_desc",
    "ai_requests_desc",
    "nutrition_days_desc",
    "training_sessions_desc",
    "last_activity_desc",
]
ALLOWED_SORTS = frozenset(
    {
        "total_tokens_desc",
        "ai_requests_desc",
        "nutrition_days_desc",
        "training_sessions_desc",
        "last_activity_desc",
    }
)
SORT_EXPRESSIONS = {
    "total_tokens_desc": "total_tokens",
    "ai_requests_desc": "ai_requests",
    "nutrition_days_desc": "nutrition_days_logged",
    "training_sessions_desc": "total_sessions",
    "last_activity_desc": "last_activity_epoch",
}


class UsageLedgerError(RuntimeError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AdminUsageSummaryRequestV1(_StrictModel):
    target_user_ids: list[UUID] = Field(min_length=1, max_length=MAX_TARGET_USERS)
    window_days: int = Field(default=30)

    @field_validator("target_user_ids", mode="before")
    @classmethod
    def parse_wire_uuids(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("target_user_ids must be a JSON array")
        parsed: list[UUID] = []
        for item in value:
            if isinstance(item, UUID):
                parsed.append(item)
                continue
            if not isinstance(item, str):
                raise ValueError("target_user_ids must contain UUID strings")
            try:
                parsed.append(UUID(item))
            except ValueError:
                raise ValueError("target_user_ids contains an invalid UUID") from None
        return parsed

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


class AdminUsageUsersRequestV1(_StrictModel):
    window_days: int = 30
    limit: int = Field(default=DEFAULT_USER_LIMIT, ge=1, le=MAX_USER_LIMIT)
    sort: UsageSortV1 = "total_tokens_desc"
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH)
    query: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)

    @field_validator("window_days")
    @classmethod
    def supported_window(cls, value: int) -> int:
        if value not in ALLOWED_WINDOWS:
            raise ValueError("window_days must be 0, 7, 30, or 90")
        return value

    @field_validator("query")
    @classmethod
    def user_id_prefix_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if not USER_ID_QUERY_PATTERN.fullmatch(normalized):
            raise ValueError("query must be a UUID prefix")
        return normalized


def _as_int(value: Any) -> int:
    return int(value or 0)


def _as_iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _query_hash(query: str | None) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()


def _cursor_signing_key(secret: str) -> bytes:
    value = str(secret or "").strip()
    if len(value) < 16:
        raise UsageLedgerError("usage cursor signing is unavailable")
    return hashlib.sha256(
        f"lifeswitch-admin-usage-cursor-v1:{value}".encode("utf-8")
    ).digest()


def _encode_cursor(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(
        _cursor_signing_key(secret),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = (
        base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )
    return f"{body}.{encoded_signature}"


def _decode_cursor(
    cursor: str,
    *,
    secret: str,
    window_days: int,
    sort: str,
    query: str | None,
) -> tuple[int, UUID]:
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH or cursor.count(".") != 1:
        raise UsageLedgerError("invalid usage cursor")
    body, encoded_signature = cursor.split(".", 1)
    expected_signature = hmac.new(
        _cursor_signing_key(secret),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        supplied_signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
    except Exception:
        raise UsageLedgerError("invalid usage cursor") from None
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise UsageLedgerError("invalid usage cursor")
    try:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(decoded)
    except Exception:
        raise UsageLedgerError("invalid usage cursor") from None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("window_days") != window_days
        or payload.get("sort") != sort
        or payload.get("query_sha256") != _query_hash(query)
        or type(payload.get("last_value")) is not int
        or not isinstance(payload.get("last_user_id"), str)
    ):
        raise UsageLedgerError("usage cursor does not match the request")
    try:
        last_user_id = UUID(payload["last_user_id"])
    except ValueError:
        raise UsageLedgerError("invalid usage cursor") from None
    return payload["last_value"], last_user_id


async def _set_writer_role(conn: Any) -> None:
    await conn.execute(f"set local role {USAGE_WRITER_ROLE}")


async def _set_admin_role(conn: Any) -> None:
    await conn.execute(f"set local role {USAGE_ADMIN_ROLE}")


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
                "reasoning_output_tokens": (
                    response.provider_reasoning_output_tokens
                ),
                "total_tokens": response.provider_total_tokens,
                "helper": USAGE_HELPER,
                "idempotency_key": str(answer_id),
                "event_schema_version": EVENT_SCHEMA_VERSION,
            }
            if existing is None or dict(existing) != expected:
                raise UsageLedgerError("provider response usage conflict")
    except UsageLedgerError:
        raise
    except Exception:
        raise UsageLedgerError("OpenAI usage persistence failed") from None


_METRICS_CTES = """
with bounds as (
  select case
    when $1::integer = 0 then null::date
    else (current_date - ($1::integer - 1))
  end as start_day
),
ai as (
  select
    event.owner_user_id,
    count(*)::bigint as ai_requests,
    coalesce(sum(event.input_tokens),0)::bigint as input_tokens,
    coalesce(sum(event.cached_input_tokens),0)::bigint as cached_input_tokens,
    coalesce(sum(event.output_tokens),0)::bigint as output_tokens,
    coalesce(sum(event.reasoning_output_tokens),0)::bigint
      as reasoning_output_tokens,
    coalesce(sum(event.total_tokens),0)::bigint as total_tokens,
    min(event.recorded_at) as first_recorded_at,
    max(event.recorded_at) as last_ai_at
  from lifeswitch_usage.ai_usage_event_v1 event
  cross join bounds
  where (
      bounds.start_day is null
      or event.recorded_at >= bounds.start_day::timestamptz
    )
    and not exists (
      select 1
      from lifeswitch_usage.ai_actor_registry_v1 registry
      where registry.actor_user_id=event.owner_user_id
    )
  group by event.owner_user_id
),
nutrition as (
  select
    d.owner_user_id,
    count(distinct d.day) filter (
      where e.nutrition_entry_id is not null
    )::bigint as nutrition_days_logged,
    count(distinct d.day) filter (
      where d.completed_at is not null
    )::bigint as nutrition_days_completed,
    count(e.nutrition_entry_id)::bigint as nutrition_entries,
    max(d.day) as last_nutrition_day
  from lifeswitch_nutrition.nutrition_day d
  left join lifeswitch_nutrition.nutrition_entry e
    on e.nutrition_day_id=d.nutrition_day_id
  cross join bounds
  where bounds.start_day is null or d.day >= bounds.start_day
  group by d.owner_user_id
),
strength as (
  select
    completed.owner_user_id,
    count(*)::bigint as strength_sessions,
    max(completed.day) as last_strength_day
  from (
    select s.owner_user_id,s.training_session_id,s.day
    from lifeswitch_training.training_session_current_v s
    join lifeswitch_training.training_set_log set_log
      on set_log.training_session_id=s.training_session_id
     and set_log.owner_user_id=s.owner_user_id
     and set_log.is_active=true
    cross join bounds
    where s.is_active=true
      and s.finished_at is not null
      and (bounds.start_day is null or s.day >= bounds.start_day)
    group by s.owner_user_id,s.training_session_id,s.day
  ) completed
  group by completed.owner_user_id
),
conditioning as (
  select
    c.owner_user_id,
    count(*)::bigint as conditioning_sessions,
    max(c.day) as last_conditioning_day
  from lifeswitch_training.conditioning_session_current_v c
  cross join bounds
  where c.is_active=true
    and (bounds.start_day is null or c.day >= bounds.start_day)
  group by c.owner_user_id
),
subjects as (
  select owner_user_id from ai
  union
  select owner_user_id from nutrition
  union
  select owner_user_id from strength
  union
  select owner_user_id from conditioning
),
metrics as (
  select
    subjects.owner_user_id,
    coalesce(ai.ai_requests,0)::bigint as ai_requests,
    coalesce(ai.input_tokens,0)::bigint as input_tokens,
    coalesce(ai.cached_input_tokens,0)::bigint as cached_input_tokens,
    coalesce(ai.output_tokens,0)::bigint as output_tokens,
    coalesce(ai.reasoning_output_tokens,0)::bigint as reasoning_output_tokens,
    coalesce(ai.total_tokens,0)::bigint as total_tokens,
    ai.first_recorded_at,
    ai.last_ai_at,
    coalesce(nutrition.nutrition_days_logged,0)::bigint
      as nutrition_days_logged,
    coalesce(nutrition.nutrition_days_completed,0)::bigint
      as nutrition_days_completed,
    coalesce(nutrition.nutrition_entries,0)::bigint as nutrition_entries,
    coalesce(strength.strength_sessions,0)::bigint as strength_sessions,
    coalesce(conditioning.conditioning_sessions,0)::bigint
      as conditioning_sessions,
    (
      coalesce(strength.strength_sessions,0)
      + coalesce(conditioning.conditioning_sessions,0)
    )::bigint as total_sessions,
    greatest(
      ai.last_ai_at,
      nutrition.last_nutrition_day::timestamptz,
      strength.last_strength_day::timestamptz,
      conditioning.last_conditioning_day::timestamptz
    ) as last_activity_at,
    coalesce(
      extract(
        epoch from greatest(
          ai.last_ai_at,
          nutrition.last_nutrition_day::timestamptz,
          strength.last_strength_day::timestamptz,
          conditioning.last_conditioning_day::timestamptz
        )
      )::bigint,
      0
    ) as last_activity_epoch
  from subjects
  left join ai using (owner_user_id)
  left join nutrition using (owner_user_id)
  left join strength using (owner_user_id)
  left join conditioning using (owner_user_id)
)
"""


def _metric_row(row: Any, *, include_user_id: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ai": {
            "requests": _as_int(row["ai_requests"]),
            "input_tokens": _as_int(row["input_tokens"]),
            "cached_input_tokens": _as_int(row["cached_input_tokens"]),
            "output_tokens": _as_int(row["output_tokens"]),
            "reasoning_output_tokens": _as_int(
                row["reasoning_output_tokens"]
            ),
            "total_tokens": _as_int(row["total_tokens"]),
            "first_recorded_at": _as_iso(row["first_recorded_at"]),
            "last_recorded_at": _as_iso(row["last_ai_at"]),
        },
        "nutrition": {
            "days_logged": _as_int(row["nutrition_days_logged"]),
            "days_completed": _as_int(row["nutrition_days_completed"]),
            "entries": _as_int(row["nutrition_entries"]),
        },
        "training": {
            "strength_sessions": _as_int(row["strength_sessions"]),
            "conditioning_sessions": _as_int(row["conditioning_sessions"]),
            "total_sessions": _as_int(row["total_sessions"]),
        },
        "last_activity_at": _as_iso(row["last_activity_at"]),
    }
    if include_user_id:
        result["user_id"] = str(row["owner_user_id"])
    return result


async def _summarize_target(
    conn: asyncpg.Connection,
    *,
    target_user_id: UUID,
    window_days: int,
) -> dict[str, Any]:
    async with conn.transaction():
        await _set_admin_role(conn)
        row = await conn.fetchrow(
            _METRICS_CTES
            + """
            select *
            from metrics
            where owner_user_id=$2
            """,
            window_days,
            target_user_id,
        )
    if row is None:
        return {
            "user_id": str(target_user_id),
            "ai": {
                "requests": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "first_recorded_at": None,
                "last_recorded_at": None,
            },
            "nutrition": {
                "days_logged": 0,
                "days_completed": 0,
                "entries": 0,
            },
            "training": {
                "strength_sessions": 0,
                "conditioning_sessions": 0,
                "total_sessions": 0,
            },
            "last_activity_at": None,
        }
    return _metric_row(row)


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
            "schema": LEGACY_USAGE_SCHEMA,
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


async def build_admin_usage_overview_v1(
    *,
    dsn: str,
    window_days: int,
) -> dict[str, Any]:
    if not dsn:
        raise UsageLedgerError("usage database is unavailable")
    if window_days not in ALLOWED_WINDOWS:
        raise UsageLedgerError("invalid usage window")
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        async with conn.transaction():
            await _set_admin_role(conn)
            row = await conn.fetchrow(
                _METRICS_CTES
                + """
                select
                  count(*)::bigint as active_users,
                  coalesce(sum(ai_requests),0)::bigint as ai_requests,
                  coalesce(sum(input_tokens),0)::bigint as input_tokens,
                  coalesce(sum(cached_input_tokens),0)::bigint
                    as cached_input_tokens,
                  coalesce(sum(output_tokens),0)::bigint as output_tokens,
                  coalesce(sum(reasoning_output_tokens),0)::bigint
                    as reasoning_output_tokens,
                  coalesce(sum(total_tokens),0)::bigint as total_tokens,
                  coalesce(sum(nutrition_days_logged),0)::bigint
                    as nutrition_days_logged,
                  coalesce(sum(nutrition_days_completed),0)::bigint
                    as nutrition_days_completed,
                  coalesce(sum(strength_sessions),0)::bigint
                    as strength_sessions,
                  coalesce(sum(conditioning_sessions),0)::bigint
                    as conditioning_sessions,
                  (
                    select min(event.recorded_at)
                    from lifeswitch_usage.ai_usage_event_v1 event
                    where not exists (
                      select 1
                      from lifeswitch_usage.ai_actor_registry_v1 registry
                      where registry.actor_user_id=event.owner_user_id
                    )
                  ) as ai_tracking_started_at,
                  max(last_activity_at) as last_activity_at
                from metrics
                """,
                window_days,
            )
            system_rows = await conn.fetch(
                """
                with bounds as (
                  select case
                    when $1::integer = 0 then null::date
                    else (current_date - ($1::integer - 1))
                  end as start_day
                )
                select
                  registry.actor_kind,
                  registry.workload_key,
                  registry.display_label,
                  count(event.ai_usage_event_id)::bigint as requests,
                  coalesce(sum(event.input_tokens),0)::bigint as input_tokens,
                  coalesce(sum(event.cached_input_tokens),0)::bigint
                    as cached_input_tokens,
                  coalesce(sum(event.output_tokens),0)::bigint as output_tokens,
                  coalesce(sum(event.reasoning_output_tokens),0)::bigint
                    as reasoning_output_tokens,
                  coalesce(sum(event.total_tokens),0)::bigint as total_tokens,
                  min(event.recorded_at) as first_recorded_at,
                  max(event.recorded_at) as last_recorded_at
                from lifeswitch_usage.ai_actor_registry_v1 registry
                cross join bounds
                left join lifeswitch_usage.ai_usage_event_v1 event
                  on event.owner_user_id=registry.actor_user_id
                 and (
                   bounds.start_day is null
                   or event.recorded_at >= bounds.start_day::timestamptz
                 )
                group by
                  registry.actor_kind,
                  registry.workload_key,
                  registry.display_label
                having count(event.ai_usage_event_id) > 0
                order by total_tokens desc,registry.workload_key asc
                limit $2
                """,
                window_days,
                MAX_SYSTEM_WORKLOADS,
            )
        if row is None:
            raise UsageLedgerError("usage overview unavailable")
        system_workloads = [
            {
                "actor_kind": str(workload["actor_kind"]),
                "workload_key": str(workload["workload_key"]),
                "display_label": str(workload["display_label"]),
                "requests": _as_int(workload["requests"]),
                "input_tokens": _as_int(workload["input_tokens"]),
                "cached_input_tokens": _as_int(
                    workload["cached_input_tokens"]
                ),
                "output_tokens": _as_int(workload["output_tokens"]),
                "reasoning_output_tokens": _as_int(
                    workload["reasoning_output_tokens"]
                ),
                "total_tokens": _as_int(workload["total_tokens"]),
                "first_recorded_at": _as_iso(workload["first_recorded_at"]),
                "last_recorded_at": _as_iso(workload["last_recorded_at"]),
            }
            for workload in system_rows
        ]
        system_first_values = [
            workload["first_recorded_at"]
            for workload in system_rows
            if workload["first_recorded_at"] is not None
        ]
        system_last_values = [
            workload["last_recorded_at"]
            for workload in system_rows
            if workload["last_recorded_at"] is not None
        ]
        return {
            "ok": True,
            "schema": OVERVIEW_SCHEMA,
            "window_days": window_days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ai_tracking_started_at": _as_iso(row["ai_tracking_started_at"]),
            "last_activity_at": _as_iso(row["last_activity_at"]),
            "active_users": _as_int(row["active_users"]),
            "ai": {
                "requests": _as_int(row["ai_requests"]),
                "input_tokens": _as_int(row["input_tokens"]),
                "cached_input_tokens": _as_int(row["cached_input_tokens"]),
                "output_tokens": _as_int(row["output_tokens"]),
                "reasoning_output_tokens": _as_int(
                    row["reasoning_output_tokens"]
                ),
                "total_tokens": _as_int(row["total_tokens"]),
            },
            "system_ai": {
                "requests": sum(
                    workload["requests"] for workload in system_workloads
                ),
                "input_tokens": sum(
                    workload["input_tokens"] for workload in system_workloads
                ),
                "cached_input_tokens": sum(
                    workload["cached_input_tokens"]
                    for workload in system_workloads
                ),
                "output_tokens": sum(
                    workload["output_tokens"] for workload in system_workloads
                ),
                "reasoning_output_tokens": sum(
                    workload["reasoning_output_tokens"]
                    for workload in system_workloads
                ),
                "total_tokens": sum(
                    workload["total_tokens"] for workload in system_workloads
                ),
                "first_recorded_at": (
                    _as_iso(min(system_first_values))
                    if system_first_values
                    else None
                ),
                "last_recorded_at": (
                    _as_iso(max(system_last_values))
                    if system_last_values
                    else None
                ),
                "workloads": system_workloads,
            },
            "coverage": {
                "product_chat": "recorded",
                "registered_system_workloads": "recorded",
                "corpus_evaluation": "not_instrumented",
                "external_agent_usage": "not_instrumented",
            },
            "nutrition": {
                "days_logged": _as_int(row["nutrition_days_logged"]),
                "days_completed": _as_int(row["nutrition_days_completed"]),
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
    except UsageLedgerError:
        raise
    except Exception:
        raise UsageLedgerError("usage overview unavailable") from None
    finally:
        await conn.close()


async def build_admin_usage_users_v1(
    *,
    dsn: str,
    request: AdminUsageUsersRequestV1,
    cursor_secret: str,
) -> dict[str, Any]:
    if not dsn:
        raise UsageLedgerError("usage database is unavailable")
    sort_expression = SORT_EXPRESSIONS.get(request.sort)
    if sort_expression is None:
        raise UsageLedgerError("invalid usage sort")
    cursor_value: int | None = None
    cursor_user_id: UUID | None = None
    if request.cursor:
        cursor_value, cursor_user_id = _decode_cursor(
            request.cursor,
            secret=cursor_secret,
            window_days=request.window_days,
            sort=request.sort,
            query=request.query,
        )
    sql = (
        _METRICS_CTES
        + f"""
        , ranked as (
          select metrics.*, {sort_expression}::bigint as sort_value
          from metrics
          where (
            $2::text is null
            or owner_user_id::text like ($2::text || '%')
          )
        )
        select *
        from ranked
        where (
          $3::bigint is null
          or sort_value < $3
          or (sort_value = $3 and owner_user_id > $4::uuid)
        )
        order by sort_value desc,owner_user_id asc
        limit $5
        """
    )
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        async with conn.transaction():
            await _set_admin_role(conn)
            rows = await conn.fetch(
                sql,
                request.window_days,
                request.query,
                cursor_value,
                cursor_user_id,
                request.limit + 1,
            )
        has_more = len(rows) > request.limit
        visible_rows = list(rows[: request.limit])
        items = [_metric_row(row) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(
                {
                    "v": 1,
                    "window_days": request.window_days,
                    "sort": request.sort,
                    "query_sha256": _query_hash(request.query),
                    "last_value": _as_int(last["sort_value"]),
                    "last_user_id": str(last["owner_user_id"]),
                },
                cursor_secret,
            )
        return {
            "ok": True,
            "schema": USERS_SCHEMA,
            "window_days": request.window_days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sort": request.sort,
            "query": request.query,
            "limit": request.limit,
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
    except UsageLedgerError:
        raise
    except Exception:
        raise UsageLedgerError("usage users unavailable") from None
    finally:
        await conn.close()


async def build_admin_usage_user_detail_v1(
    *,
    dsn: str,
    target_user_id: UUID,
    window_days: int,
) -> dict[str, Any]:
    if not dsn:
        raise UsageLedgerError("usage database is unavailable")
    if window_days not in ALLOWED_WINDOWS:
        raise UsageLedgerError("invalid usage window")
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        async with conn.transaction():
            await _set_admin_role(conn)
            metrics = await conn.fetchrow(
                _METRICS_CTES
                + """
                select *
                from metrics
                where owner_user_id=$2
                """,
                window_days,
                target_user_id,
            )
            by_model = await conn.fetch(
                """
                with bounds as (
                  select case
                    when $2::integer = 0 then null::date
                    else (current_date - ($2::integer - 1))
                  end as start_day
                )
                select
                  returned_model as model,
                  count(*)::bigint as requests,
                  sum(input_tokens)::bigint as input_tokens,
                  sum(cached_input_tokens)::bigint as cached_input_tokens,
                  sum(output_tokens)::bigint as output_tokens,
                  sum(reasoning_output_tokens)::bigint
                    as reasoning_output_tokens,
                  sum(total_tokens)::bigint as total_tokens
                from lifeswitch_usage.ai_usage_event_v1,bounds
                where owner_user_id=$1
                  and not exists (
                    select 1
                    from lifeswitch_usage.ai_actor_registry_v1 registry
                    where registry.actor_user_id=$1
                  )
                  and (
                    bounds.start_day is null
                    or recorded_at >= bounds.start_day::timestamptz
                  )
                group by returned_model
                order by total_tokens desc,returned_model asc
                """,
                target_user_id,
                window_days,
            )
            by_channel = await conn.fetch(
                """
                with bounds as (
                  select case
                    when $2::integer = 0 then null::date
                    else (current_date - ($2::integer - 1))
                  end as start_day
                )
                select
                  source_channel as channel,
                  count(*)::bigint as requests,
                  sum(total_tokens)::bigint as total_tokens
                from lifeswitch_usage.ai_usage_event_v1,bounds
                where owner_user_id=$1
                  and not exists (
                    select 1
                    from lifeswitch_usage.ai_actor_registry_v1 registry
                    where registry.actor_user_id=$1
                  )
                  and (
                    bounds.start_day is null
                    or recorded_at >= bounds.start_day::timestamptz
                  )
                group by source_channel
                order by source_channel
                """,
                target_user_id,
                window_days,
            )
            daily = await conn.fetch(
                """
                with bounds as (
                  select case
                    when $2::integer = 0 then null::date
                    else (current_date - ($2::integer - 1))
                  end as start_day
                ),
                ai as (
                  select
                    recorded_at::date as day,
                    count(*)::bigint as ai_requests,
                    sum(total_tokens)::bigint as total_tokens
                  from lifeswitch_usage.ai_usage_event_v1,bounds
                  where owner_user_id=$1
                    and not exists (
                      select 1
                      from lifeswitch_usage.ai_actor_registry_v1 registry
                      where registry.actor_user_id=$1
                    )
                    and (
                      bounds.start_day is null
                      or recorded_at >= bounds.start_day::timestamptz
                    )
                  group by recorded_at::date
                ),
                nutrition as (
                  select
                    d.day,
                    (count(e.nutrition_entry_id) > 0)::integer
                      as nutrition_day_logged,
                    (d.completed_at is not null)::integer
                      as nutrition_day_completed
                  from lifeswitch_nutrition.nutrition_day d
                  left join lifeswitch_nutrition.nutrition_entry e
                    on e.nutrition_day_id=d.nutrition_day_id
                  cross join bounds
                  where d.owner_user_id=$1
                    and (bounds.start_day is null or d.day >= bounds.start_day)
                  group by d.day,d.completed_at
                ),
                strength as (
                  select
                    completed.day,
                    count(*)::bigint as strength_sessions
                  from (
                    select s.training_session_id,s.day
                    from lifeswitch_training.training_session_current_v s
                    join lifeswitch_training.training_set_log set_log
                      on set_log.training_session_id=s.training_session_id
                     and set_log.owner_user_id=s.owner_user_id
                     and set_log.is_active=true
                    cross join bounds
                    where s.owner_user_id=$1
                      and s.is_active=true
                      and s.finished_at is not null
                      and (
                        bounds.start_day is null
                        or s.day >= bounds.start_day
                      )
                    group by s.training_session_id,s.day
                  ) completed
                  group by completed.day
                ),
                conditioning as (
                  select c.day,count(*)::bigint as conditioning_sessions
                  from lifeswitch_training.conditioning_session_current_v c
                  cross join bounds
                  where c.owner_user_id=$1
                    and c.is_active=true
                    and (
                      bounds.start_day is null
                      or c.day >= bounds.start_day
                    )
                  group by c.day
                ),
                days as (
                  select day from ai
                  union select day from nutrition
                  union select day from strength
                  union select day from conditioning
                )
                select
                  days.day,
                  coalesce(ai.ai_requests,0)::bigint as ai_requests,
                  coalesce(ai.total_tokens,0)::bigint as total_tokens,
                  coalesce(nutrition.nutrition_day_logged,0)::bigint
                    as nutrition_day_logged,
                  coalesce(nutrition.nutrition_day_completed,0)::bigint
                    as nutrition_day_completed,
                  coalesce(strength.strength_sessions,0)::bigint
                    as strength_sessions,
                  coalesce(conditioning.conditioning_sessions,0)::bigint
                    as conditioning_sessions
                from days
                left join ai using (day)
                left join nutrition using (day)
                left join strength using (day)
                left join conditioning using (day)
                order by days.day desc
                """,
                target_user_id,
                window_days,
            )
        summary = (
            _metric_row(metrics, include_user_id=False)
            if metrics is not None
            else {
                "ai": {
                    "requests": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 0,
                    "first_recorded_at": None,
                    "last_recorded_at": None,
                },
                "nutrition": {
                    "days_logged": 0,
                    "days_completed": 0,
                    "entries": 0,
                },
                "training": {
                    "strength_sessions": 0,
                    "conditioning_sessions": 0,
                    "total_sessions": 0,
                },
                "last_activity_at": None,
            }
        )
        summary["ai"]["by_model"] = [
            {
                "model": str(row["model"]),
                "requests": _as_int(row["requests"]),
                "input_tokens": _as_int(row["input_tokens"]),
                "cached_input_tokens": _as_int(row["cached_input_tokens"]),
                "output_tokens": _as_int(row["output_tokens"]),
                "reasoning_output_tokens": _as_int(
                    row["reasoning_output_tokens"]
                ),
                "total_tokens": _as_int(row["total_tokens"]),
            }
            for row in by_model
        ]
        summary["ai"]["by_channel"] = [
            {
                "channel": str(row["channel"]),
                "requests": _as_int(row["requests"]),
                "total_tokens": _as_int(row["total_tokens"]),
            }
            for row in by_channel
        ]
        return {
            "ok": True,
            "schema": USER_DETAIL_SCHEMA,
            "window_days": window_days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(target_user_id),
            "summary": summary,
            "daily": [
                {
                    "day": row["day"].isoformat(),
                    "ai_requests": _as_int(row["ai_requests"]),
                    "total_tokens": _as_int(row["total_tokens"]),
                    "nutrition_day_logged": _as_int(
                        row["nutrition_day_logged"]
                    ),
                    "nutrition_day_completed": _as_int(
                        row["nutrition_day_completed"]
                    ),
                    "strength_sessions": _as_int(row["strength_sessions"]),
                    "conditioning_sessions": _as_int(
                        row["conditioning_sessions"]
                    ),
                }
                for row in daily
            ],
        }
    except UsageLedgerError:
        raise
    except Exception:
        raise UsageLedgerError("usage user detail unavailable") from None
    finally:
        await conn.close()


__all__ = [
    "AdminUsageSummaryRequestV1",
    "AdminUsageUsersRequestV1",
    "UsageLedgerError",
    "build_admin_usage_overview_v1",
    "build_admin_usage_summary_v1",
    "build_admin_usage_user_detail_v1",
    "build_admin_usage_users_v1",
    "persist_openai_chat_usage_v1",
]
