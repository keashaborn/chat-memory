from __future__ import annotations

"""Owner-scoped PostgreSQL export of canonical conversation data."""

from typing import Any
from uuid import UUID

from seebx.adapters.postgres import PostgresConnectionProvider


MAX_EXPORT_THREADS = 10_000
MAX_EXPORT_MESSAGES = 100_000
MAX_EXPORT_ATTACHMENTS = 10_000
MAX_EXPORT_WEB_TRANSCRIPTS = 100_000


class ConversationExportRepositoryError(RuntimeError):
    pass


class PostgresConversationExportRepository:
    def __init__(self, provider: PostgresConnectionProvider) -> None:
        self._provider = provider

    async def export_owner(self, owner_user_id: UUID) -> dict[str, Any]:
        if not isinstance(owner_user_id, UUID):
            raise ConversationExportRepositoryError("invalid_export_owner")
        try:
            async with self._provider.owner_connection(owner_user_id) as conn:
                async with conn.transaction(
                    isolation="repeatable_read",
                    readonly=True,
                ):
                    role = str(await conn.fetchval("SELECT current_user"))
                    read_only = str(
                        await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )
                    )
                    if role != "brains_app" or read_only != "on":
                        raise ConversationExportRepositoryError(
                            "conversation_export_read_boundary_invalid"
                        )
                    counts = dict(
                        await conn.fetchrow(
                            """
                            SELECT
                              (SELECT count(*) FROM public.threads
                               WHERE owner_user_id=$1) AS threads,
                              (SELECT count(*) FROM public.chat_log
                               WHERE owner_user_id=$1) AS messages,
                              (SELECT count(*) FROM public.chat_attachments
                               WHERE owner_user_id=$1) AS attachments,
                              (SELECT count(*)
                               FROM trusted_web.response_transcript_v1
                               WHERE owner_user_id=$1) AS web_transcripts
                            """,
                            owner_user_id,
                        )
                    )
                    limits = {
                        "threads": MAX_EXPORT_THREADS,
                        "messages": MAX_EXPORT_MESSAGES,
                        "attachments": MAX_EXPORT_ATTACHMENTS,
                        "web_transcripts": MAX_EXPORT_WEB_TRANSCRIPTS,
                    }
                    if any(
                        type(counts.get(name)) is not int
                        or counts[name] < 0
                        or counts[name] > limit
                        for name, limit in limits.items()
                    ):
                        raise ConversationExportRepositoryError(
                            "conversation_export_size_limit_exceeded"
                        )
                    threads = await conn.fetch(
                        """
                        SELECT thread.id,thread.title,thread.title_source,
                               thread.created_at,thread.updated_at,
                               thread.archived,thread.pinned_at,
                               (selection.thread_id IS NOT NULL) AS active
                        FROM public.threads AS thread
                        LEFT JOIN public.active_thread_selection AS selection
                          ON selection.owner_user_id=thread.owner_user_id
                         AND selection.thread_id=thread.id
                        WHERE thread.owner_user_id=$1
                        ORDER BY thread.created_at,thread.id
                        """,
                        owner_user_id,
                    )
                    messages = await conn.fetch(
                        """
                        SELECT id,thread_id,source,text,tags,request_id,
                               created_at
                        FROM public.chat_log
                        WHERE owner_user_id=$1
                        ORDER BY created_at,id
                        """,
                        owner_user_id,
                    )
                    attachments = await conn.fetch(
                        """
                        SELECT id,thread_id,message_id,filename,media_type,
                               content,content_sha256,byte_size,status,
                               created_at,updated_at,deleted_at
                        FROM public.chat_attachments
                        WHERE owner_user_id=$1
                        ORDER BY created_at,id
                        """,
                        owner_user_id,
                    )
                    web_transcripts = await conn.fetch(
                        """
                        SELECT response_id,thread_id,user_chat_log_id,
                               assistant_chat_log_id,request_id,search_id,
                               route,policy_version,decision,query_sha256,
                               answer_sha256,cited_sources,admitted_sources,
                               consulted_source_count,created_at
                        FROM trusted_web.response_transcript_v1
                        WHERE owner_user_id=$1
                        ORDER BY created_at,response_id
                        """,
                        owner_user_id,
                    )
        except ConversationExportRepositoryError:
            raise
        except Exception:
            raise ConversationExportRepositoryError(
                "conversation_export_read_failed"
            ) from None

        observed = {
            "threads": len(threads),
            "messages": len(messages),
            "attachments": len(attachments),
            "web_transcripts": len(web_transcripts),
        }
        if observed != counts:
            raise ConversationExportRepositoryError(
                "conversation_export_snapshot_count_mismatch"
            )
        return {
            "status": "complete",
            "counts": counts,
            "threads": [dict(row) for row in threads],
            "messages": [dict(row) for row in messages],
            "attachments": [dict(row) for row in attachments],
            "trusted_web_transcripts": [
                dict(row) for row in web_transcripts
            ],
        }


__all__ = [
    "ConversationExportRepositoryError",
    "PostgresConversationExportRepository",
]
