from __future__ import annotations

"""PostgreSQL adapter for the content-free durable Zep turn outbox."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.conversation.zep_sync import ZepTurnSyncJob


ENQUEUE_ZEP_TURN_SQL = """
INSERT INTO conversation_sync_private.zep_turn_outbox(
  job_id, owner_user_id, thread_id, user_message_id, assistant_message_id
)
SELECT assistant.id, $1::uuid, $2::uuid, user_message.id, assistant.id
FROM conversation.chat_log AS user_message
JOIN conversation.chat_log AS assistant
  ON assistant.id=$4::uuid
 AND assistant.owner_user_id=$1::uuid
 AND assistant.thread_id=$2::uuid
WHERE user_message.id=$3::uuid
  AND user_message.owner_user_id=$1::uuid
  AND user_message.thread_id=$2::uuid
  AND user_message.id<>assistant.id
  AND user_message.tags @> ARRAY['user','chat']::text[]
  AND assistant.tags @> ARRAY['assistant','chat','server_attested']::text[]
ON CONFLICT (job_id) DO NOTHING
RETURNING job_id
"""

FETCH_ZEP_TURN_SQL = """
SELECT job_id, owner_user_id, thread_id, user_message_id, assistant_message_id
FROM conversation_sync_private.zep_turn_outbox
WHERE job_id=$1::uuid
"""

CLAIM_NEXT_ZEP_TURN_SQL = """
WITH candidate AS (
  SELECT item.job_id
  FROM conversation_sync_private.zep_turn_outbox AS item
  JOIN conversation.chat_log AS item_user
    ON item_user.id=item.user_message_id
   AND item_user.owner_user_id=item.owner_user_id
   AND item_user.thread_id=item.thread_id
  WHERE item.state IN ('pending','retry','processing')
    AND (
      item.state='pending'
      OR (item.state='retry' AND item.available_at<=clock_timestamp())
      OR (
        item.state='processing'
        AND item.lease_expires_at<=clock_timestamp()
      )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM conversation_sync_private.zep_turn_outbox AS prior
      JOIN conversation.chat_log AS prior_user
        ON prior_user.id=prior.user_message_id
       AND prior_user.owner_user_id=prior.owner_user_id
       AND prior_user.thread_id=prior.thread_id
      WHERE prior.owner_user_id=item.owner_user_id
        AND prior.thread_id=item.thread_id
        AND prior.state IN (
          'pending','retry','processing','failed_terminal'
        )
        AND (
          prior_user.created_at,prior.user_message_id,prior.job_id
        ) < (
          item_user.created_at,item.user_message_id,item.job_id
        )
    )
  ORDER BY item_user.created_at,item.user_message_id,item.job_id
  LIMIT 1
  FOR UPDATE OF item SKIP LOCKED
), claimed AS (
  UPDATE conversation_sync_private.zep_turn_outbox AS item
  SET state='processing',
      attempt_count=item.attempt_count+1,
      lease_token=$1::uuid,
      lease_expires_at=clock_timestamp()+make_interval(secs=>$2::integer),
      last_error_code=NULL,
      updated_at=clock_timestamp()
  FROM candidate
  WHERE item.job_id=candidate.job_id
  RETURNING item.*
)
SELECT claimed.job_id,claimed.lease_token,claimed.owner_user_id,
       claimed.thread_id,claimed.user_message_id,
       claimed.assistant_message_id,claimed.attempt_count,
       user_message.text AS user_message,
       assistant.text AS assistant_message,
       user_message.created_at AS user_created_at,
       assistant.created_at AS assistant_created_at
FROM claimed
JOIN conversation.chat_log AS user_message
  ON user_message.id=claimed.user_message_id
 AND user_message.owner_user_id=claimed.owner_user_id
 AND user_message.thread_id=claimed.thread_id
JOIN conversation.chat_log AS assistant
  ON assistant.id=claimed.assistant_message_id
 AND assistant.owner_user_id=claimed.owner_user_id
 AND assistant.thread_id=claimed.thread_id
"""

MARK_ZEP_TURN_COMPLETED_SQL = """
UPDATE conversation_sync_private.zep_turn_outbox
SET state='completed',lease_token=NULL,lease_expires_at=NULL,
    completed_at=clock_timestamp(),updated_at=clock_timestamp(),
    last_error_code=NULL
WHERE job_id=$1::uuid AND lease_token=$2::uuid AND state='processing'
"""

MARK_ZEP_TURN_RETRY_SQL = """
UPDATE conversation_sync_private.zep_turn_outbox
SET state='retry',lease_token=NULL,lease_expires_at=NULL,
    available_at=clock_timestamp()+make_interval(secs=>$3::integer),
    updated_at=clock_timestamp(),last_error_code=$4::text
WHERE job_id=$1::uuid AND lease_token=$2::uuid AND state='processing'
"""

MARK_ZEP_TURN_TERMINAL_SQL = """
UPDATE conversation_sync_private.zep_turn_outbox
SET state='failed_terminal',lease_token=NULL,lease_expires_at=NULL,
    completed_at=clock_timestamp(),updated_at=clock_timestamp(),
    last_error_code=$3::text
WHERE job_id=$1::uuid AND lease_token=$2::uuid AND state='processing'
"""


def _uuid(value: object, field: str) -> UUID:
    try:
        result = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"invalid_{field}") from error
    return result


def _command_updated_one(command: object) -> bool:
    return isinstance(command, str) and command == "UPDATE 1"


async def enqueue_zep_turn(
    connection: Any,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
) -> UUID:
    if not all(
        isinstance(value, UUID)
        for value in (
            owner_user_id,
            thread_id,
            user_message_id,
            assistant_message_id,
        )
    ) or user_message_id == assistant_message_id:
        raise ValueError("invalid_zep_sync_turn")
    inserted = await connection.fetchval(
        ENQUEUE_ZEP_TURN_SQL,
        owner_user_id,
        thread_id,
        user_message_id,
        assistant_message_id,
    )
    if inserted is not None:
        return _uuid(inserted, "zep_sync_job_id")
    existing = await connection.fetchrow(
        FETCH_ZEP_TURN_SQL,
        assistant_message_id,
    )
    expected = (
        assistant_message_id,
        owner_user_id,
        thread_id,
        user_message_id,
        assistant_message_id,
    )
    if existing is None or tuple(existing[field] for field in (
        "job_id",
        "owner_user_id",
        "thread_id",
        "user_message_id",
        "assistant_message_id",
    )) != expected:
        raise RuntimeError("zep_sync_enqueue_failed")
    return assistant_message_id


class PostgresZepSyncRepository:
    def __init__(
        self,
        provider: PostgresConnectionProvider,
        *,
        lease_seconds: int = 60,
    ) -> None:
        if not 15 <= lease_seconds <= 300:
            raise ValueError("invalid lease_seconds")
        self._provider = provider
        self._lease_seconds = lease_seconds

    async def claim_next(self) -> ZepTurnSyncJob | None:
        lease_token = uuid4()
        async with self._provider.connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    CLAIM_NEXT_ZEP_TURN_SQL,
                    lease_token,
                    self._lease_seconds,
                )
        if row is None:
            return None
        attempt_count = row["attempt_count"]
        if type(attempt_count) is not int or attempt_count < 1:
            raise RuntimeError("invalid_zep_sync_attempt_count")
        for field in (
            "user_message",
            "assistant_message",
        ):
            if not isinstance(row[field], str) or not row[field].strip():
                raise RuntimeError("invalid_zep_sync_message")
        for field in ("user_created_at", "assistant_created_at"):
            if not isinstance(row[field], datetime):
                raise RuntimeError("invalid_zep_sync_timestamp")
        return ZepTurnSyncJob(
            job_id=_uuid(row["job_id"], "zep_sync_job_id"),
            lease_token=_uuid(row["lease_token"], "zep_sync_lease_token"),
            owner_user_id=_uuid(row["owner_user_id"], "zep_sync_owner"),
            thread_id=_uuid(row["thread_id"], "zep_sync_thread"),
            user_message_id=_uuid(
                row["user_message_id"], "zep_sync_user_message"
            ),
            assistant_message_id=_uuid(
                row["assistant_message_id"], "zep_sync_assistant_message"
            ),
            user_message=row["user_message"],
            assistant_message=row["assistant_message"],
            user_created_at=row["user_created_at"],
            assistant_created_at=row["assistant_created_at"],
            attempt_count=attempt_count,
        )

    async def _transition(self, sql: str, *arguments: object) -> None:
        async with self._provider.connection() as connection:
            command = await connection.execute(sql, *arguments)
        if not _command_updated_one(command):
            raise RuntimeError("zep_sync_lease_lost")

    async def mark_completed(self, job: ZepTurnSyncJob) -> None:
        await self._transition(
            MARK_ZEP_TURN_COMPLETED_SQL,
            job.job_id,
            job.lease_token,
        )

    async def mark_retry(
        self,
        job: ZepTurnSyncJob,
        *,
        error_code: str,
        delay_seconds: int,
    ) -> None:
        if not isinstance(error_code, str) or not 1 <= len(error_code) <= 120:
            raise ValueError("invalid error_code")
        if type(delay_seconds) is not int or not 1 <= delay_seconds <= 300:
            raise ValueError("invalid delay_seconds")
        await self._transition(
            MARK_ZEP_TURN_RETRY_SQL,
            job.job_id,
            job.lease_token,
            delay_seconds,
            error_code,
        )

    async def mark_terminal(
        self,
        job: ZepTurnSyncJob,
        *,
        error_code: str,
    ) -> None:
        if not isinstance(error_code, str) or not 1 <= len(error_code) <= 120:
            raise ValueError("invalid error_code")
        await self._transition(
            MARK_ZEP_TURN_TERMINAL_SQL,
            job.job_id,
            job.lease_token,
            error_code,
        )


__all__ = [
    "PostgresZepSyncRepository",
    "enqueue_zep_turn",
]
