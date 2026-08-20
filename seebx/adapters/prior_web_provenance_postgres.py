from __future__ import annotations

"""Owner-bound PostgreSQL reads for prior trusted-web provenance."""

from datetime import datetime
from typing import Any
from uuid import UUID


_LOAD_CANDIDATES_SQL = """
    SELECT log.id AS log_id,log.owner_user_id,log.thread_id,
           log.text AS assistant_text,log.created_at,
           web.response_id,web.assistant_chat_log_id,
           web.search_id,web.route,web.policy_version,web.decision,
           web.answer_sha256,web.cited_sources,web.admitted_sources
    FROM public.chat_log AS log
    JOIN trusted_web.response_transcript_v1 AS web
      ON web.owner_user_id=log.owner_user_id
     AND web.thread_id=log.thread_id
     AND web.assistant_chat_log_id=log.id
     AND web.response_id=log.id
    WHERE log.owner_user_id=$1
      AND log.thread_id=$2
      AND log.source=$3
      AND (log.created_at,log.id)<($4,$5)
    ORDER BY log.created_at DESC,log.id DESC
    LIMIT $6
"""


class PriorWebProvenanceRepositoryError(RuntimeError):
    """Stable content-free failure from the restricted provenance reader."""


class PostgresPriorWebProvenanceRepository:
    def __init__(self, connection: Any) -> None:
        required = ("transaction", "execute", "fetchval", "fetch")
        if connection is None or any(
            not callable(getattr(connection, name, None)) for name in required
        ):
            raise ValueError("a PostgreSQL provenance connection is required")
        self._connection = connection

    async def load_candidates(
        self,
        *,
        authenticated_actor_user_id: UUID,
        thread_id: UUID,
        assistant_source: str,
        cutoff_created_at: datetime,
        current_log_id: UUID,
        limit: int,
    ) -> tuple[Any, ...]:
        try:
            async with self._connection.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                await self._connection.execute(
                    "SELECT set_config('app.user_id',$1,true)",
                    str(authenticated_actor_user_id),
                )
                role = str(
                    await self._connection.fetchval("SELECT current_user")
                )
                read_only = str(
                    await self._connection.fetchval(
                        "SELECT current_setting('transaction_read_only')"
                    )
                )
                if role != "brains_app" or read_only != "on":
                    raise PriorWebProvenanceRepositoryError(
                        "provenance requires brains_app in a read-only transaction"
                    )
                owns_thread = bool(
                    await self._connection.fetchval(
                        """
                        SELECT EXISTS(
                          SELECT 1 FROM public.threads
                          WHERE owner_user_id=$1 AND id=$2
                        )
                        """,
                        authenticated_actor_user_id,
                        thread_id,
                    )
                )
                if not owns_thread:
                    raise PriorWebProvenanceRepositoryError(
                        "owner thread is absent"
                    )
                rows = await self._connection.fetch(
                    _LOAD_CANDIDATES_SQL,
                    authenticated_actor_user_id,
                    thread_id,
                    assistant_source,
                    cutoff_created_at,
                    current_log_id,
                    limit,
                )
                return tuple(rows)
        except PriorWebProvenanceRepositoryError:
            raise
        except Exception:
            raise PriorWebProvenanceRepositoryError(
                "prior web provenance read failed"
            ) from None


__all__ = [
    "PostgresPriorWebProvenanceRepository",
    "PriorWebProvenanceRepositoryError",
]
