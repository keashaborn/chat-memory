from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import asyncpg

from .identifiers import uuid7
from .plan_domain import PlanDomainError


SCHEMA = "lifeswitch_agentic"
IdFactory = Callable[[], uuid.UUID]
_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,120}$")
_VERSION_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    event_id: uuid.UUID
    owner_user_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    event_version: int
    payload: Mapping[str, Any]
    claim_token: uuid.UUID
    attempt_count: int


def _name(value: str, *, field: str, pattern: re.Pattern[str] = _NAME_RE) -> str:
    cleaned = value.strip()
    if not pattern.fullmatch(cleaned):
        raise PlanDomainError("invalid_outbox_value", f"{field} is invalid")
    return cleaned


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RuntimeError("outbox payload is not a JSON object")
    return dict(value)


def _request_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 200:
        raise PlanDomainError("invalid_outbox_value", "request_id exceeds 200 characters")
    return cleaned


class OutboxRepository:
    def __init__(self, *, id_factory: IdFactory = uuid7) -> None:
        self._id_factory = id_factory

    async def enqueue(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any],
        event_version: int = 1,
        request_id: str | None = None,
    ) -> uuid.UUID:
        if not conn.is_in_transaction():
            raise RuntimeError("outbox event must be inserted inside the domain transaction")
        if event_version < 1:
            raise PlanDomainError("invalid_outbox_value", "event_version must be positive")
        event_id = self._id_factory()
        await conn.execute(
            f"""
            insert into {SCHEMA}.outbox_events (
              id, owner_user_id, aggregate_type, aggregate_id,
              event_type, event_version, payload, request_id
            )
            values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            """,
            event_id,
            owner_user_id,
            _name(aggregate_type, field="aggregate_type"),
            aggregate_id,
            _name(event_type, field="event_type"),
            event_version,
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
            _request_id(request_id),
        )
        return event_id

    async def claim_batch(
        self,
        conn: asyncpg.Connection,
        *,
        worker_id: str,
        limit: int = 25,
        lease_seconds: int = 120,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        worker = _name(worker_id, field="worker_id")
        if not 1 <= limit <= 100:
            raise PlanDomainError("invalid_outbox_value", "claim limit must be between 1 and 100")
        if not 1 <= lease_seconds <= 3600:
            raise PlanDomainError("invalid_outbox_value", "lease must be between 1 and 3600 seconds")
        claim_token = self._id_factory()
        async with conn.transaction():
            rows = await conn.fetch(
                f"""
                with candidates as (
                  select id
                  from {SCHEMA}.outbox_events
                  where handled_at is null
                    and available_at <= now()
                    and (
                      claimed_by is null
                      or claim_expires_at <= now()
                    )
                  order by available_at, id
                  limit $1
                  for update skip locked
                )
                update {SCHEMA}.outbox_events event
                set claimed_by = $2,
                    claim_token = $3,
                    claim_expires_at = now() + ($4 * interval '1 second'),
                    attempt_count = event.attempt_count + 1,
                    last_error_code = null
                from candidates
                where event.id = candidates.id
                returning event.id, event.owner_user_id,
                          event.aggregate_type, event.aggregate_id,
                          event.event_type, event.event_version,
                          event.payload, event.claim_token,
                          event.attempt_count
                """,
                limit,
                worker,
                claim_token,
                lease_seconds,
            )
        return tuple(
            ClaimedOutboxEvent(
                event_id=row["id"],
                owner_user_id=row["owner_user_id"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                event_type=row["event_type"],
                event_version=int(row["event_version"]),
                payload=_payload(row["payload"]),
                claim_token=row["claim_token"],
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        )

    async def complete_delivery(
        self,
        conn: asyncpg.Connection,
        *,
        event_id: uuid.UUID,
        claim_token: uuid.UUID,
        consumer_name: str,
        handler_version: str,
    ) -> bool:
        consumer = _name(consumer_name, field="consumer_name")
        version = _name(handler_version, field="handler_version", pattern=_VERSION_RE)
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                select handled_at, claim_token
                from {SCHEMA}.outbox_events
                where id = $1
                for update
                """,
                event_id,
            )
            if row is None:
                return False
            if row["handled_at"] is not None:
                delivered = await conn.fetchval(
                    f"""
                    select exists (
                      select 1 from {SCHEMA}.outbox_deliveries
                      where outbox_event_id = $1
                        and consumer_name = $2
                        and handler_version = $3
                    )
                    """,
                    event_id,
                    consumer,
                    version,
                )
                return bool(delivered)
            if row["claim_token"] != claim_token:
                return False
            await conn.execute(
                f"""
                insert into {SCHEMA}.outbox_deliveries (
                  outbox_event_id, consumer_name, handler_version
                )
                values ($1, $2, $3)
                on conflict (outbox_event_id, consumer_name, handler_version)
                  do nothing
                """,
                event_id,
                consumer,
                version,
            )
            status = await conn.execute(
                f"""
                update {SCHEMA}.outbox_events
                set handled_at = now(), claimed_by = null,
                    claim_token = null, claim_expires_at = null,
                    last_error_code = null
                where id = $1 and claim_token = $2 and handled_at is null
                """,
                event_id,
                claim_token,
            )
            if status != "UPDATE 1":
                raise RuntimeError("outbox completion lost its claim")
        return True

    async def release_failed(
        self,
        conn: asyncpg.Connection,
        *,
        event_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_code: str,
        retry_after_seconds: int,
    ) -> bool:
        error = _name(error_code, field="error_code")
        if not 0 <= retry_after_seconds <= 86400:
            raise PlanDomainError(
                "invalid_outbox_value",
                "retry delay must be between 0 and 86400 seconds",
            )
        async with conn.transaction():
            status = await conn.execute(
                f"""
                update {SCHEMA}.outbox_events
                set available_at = now() + ($3 * interval '1 second'),
                    claimed_by = null, claim_token = null,
                    claim_expires_at = null, last_error_code = $4
                where id = $1 and claim_token = $2 and handled_at is null
                """,
                event_id,
                claim_token,
                retry_after_seconds,
                error,
            )
        return status == "UPDATE 1"
