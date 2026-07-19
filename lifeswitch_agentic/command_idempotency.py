from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
import re
import uuid
from typing import Any, Mapping

import asyncpg

from .plan_domain import PlanDomainError


SCHEMA = "lifeswitch_agentic"
_COMMAND_RE = re.compile(r"^[a-z0-9_.]{1,80}$")
_KEY_RE = re.compile(r"^[!-~]{1,128}$")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise PlanDomainError(
        "invalid_idempotency_input",
        f"unsupported idempotency input type {type(value).__name__}",
    )


def command_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_command_identity(command_name: str, idempotency_key: str) -> tuple[str, str]:
    command = command_name.strip()
    key = idempotency_key.strip()
    if not _COMMAND_RE.fullmatch(command):
        raise PlanDomainError("invalid_command_name", "command name is invalid")
    if not _KEY_RE.fullmatch(key):
        raise PlanDomainError(
            "invalid_idempotency_key",
            "idempotency key must contain 1-128 visible ASCII characters",
        )
    return command, key


async def begin_command(
    conn: asyncpg.Connection,
    *,
    owner_user_id: uuid.UUID,
    command_name: str,
    idempotency_key: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    if not conn.is_in_transaction():
        raise RuntimeError("idempotency receipt must be reserved inside a transaction")
    command, key = validate_command_identity(command_name, idempotency_key)
    inserted = await conn.fetchval(
        f"""
        insert into {SCHEMA}.command_receipts (
          owner_user_id, command_name, idempotency_key,
          request_sha256, state
        )
        values ($1, $2, $3, $4, 'started')
        on conflict (owner_user_id, command_name, idempotency_key)
          do nothing
        returning true
        """,
        owner_user_id,
        command,
        key,
        request_sha256,
    )
    if inserted:
        return None

    row = await conn.fetchrow(
        f"""
        select request_sha256, state, response
        from {SCHEMA}.command_receipts
        where owner_user_id = $1
          and command_name = $2
          and idempotency_key = $3
        """,
        owner_user_id,
        command,
        key,
    )
    if row is None:
        raise RuntimeError("idempotency receipt disappeared after conflict")
    if row["request_sha256"] != request_sha256:
        raise PlanDomainError(
            "idempotency_conflict",
            "idempotency key was already used with different input",
        )
    if row["state"] != "completed" or row["response"] is None:
        raise PlanDomainError("idempotency_in_progress", "matching command is still in progress")
    response = row["response"]
    if isinstance(response, str):
        response = json.loads(response)
    if not isinstance(response, Mapping):
        raise RuntimeError("completed idempotency receipt has invalid response")
    return dict(response)


async def complete_command(
    conn: asyncpg.Connection,
    *,
    owner_user_id: uuid.UUID,
    command_name: str,
    idempotency_key: str,
    request_sha256: str,
    response: Mapping[str, Any],
) -> None:
    if not conn.is_in_transaction():
        raise RuntimeError("idempotency receipt must complete inside a transaction")
    command, key = validate_command_identity(command_name, idempotency_key)
    status = await conn.execute(
        f"""
        update {SCHEMA}.command_receipts
        set state = 'completed', response = $5::jsonb, completed_at = now()
        where owner_user_id = $1
          and command_name = $2
          and idempotency_key = $3
          and request_sha256 = $4
          and state = 'started'
        """,
        owner_user_id,
        command,
        key,
        request_sha256,
        json.dumps(_json_value(response), sort_keys=True, separators=(",", ":")),
    )
    if status != "UPDATE 1":
        raise RuntimeError("idempotency receipt completion failed")
