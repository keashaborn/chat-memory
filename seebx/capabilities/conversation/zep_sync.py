from __future__ import annotations

"""Restart-safe coordinator for the PostgreSQL-to-Zep turn outbox."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from seebx.adapters.zep_cloud import ZepSyncPermanentError


@dataclass(frozen=True)
class ZepTurnSyncJob:
    job_id: UUID
    lease_token: UUID
    owner_user_id: UUID
    thread_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    user_message: str
    assistant_message: str
    user_created_at: datetime
    assistant_created_at: datetime
    attempt_count: int


class ZepTurnSyncRepository(Protocol):
    async def claim_next(self) -> ZepTurnSyncJob | None: ...

    async def mark_completed(self, job: ZepTurnSyncJob) -> None: ...

    async def mark_retry(
        self,
        job: ZepTurnSyncJob,
        *,
        error_code: str,
        delay_seconds: int,
    ) -> None: ...

    async def mark_terminal(
        self,
        job: ZepTurnSyncJob,
        *,
        error_code: str,
    ) -> None: ...


class ZepTurnSynchronizer(Protocol):
    async def synchronize_turn(
        self,
        *,
        owner_user_id: UUID,
        thread_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
        user_created_at: datetime,
        assistant_created_at: datetime,
    ) -> str: ...


def retry_delay_seconds(attempt_count: int) -> int:
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    return min(300, 2 ** min(attempt_count, 8))


class ZepSyncWorker:
    def __init__(
        self,
        *,
        repository: ZepTurnSyncRepository,
        synchronizer: ZepTurnSynchronizer,
        poll_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if not 0.05 <= poll_seconds <= 60.0:
            raise ValueError("invalid poll_seconds")
        self._repository = repository
        self._synchronizer = synchronizer
        self._poll_seconds = poll_seconds
        self._logger = logger or logging.getLogger("uvicorn.error")
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("zep_sync_worker_already_started")
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        self._wake.set()

    def notify(self) -> None:
        if self._task is not None:
            self._wake.set()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self._wake.set()
        await task
        self._task = None

    async def process_once(self) -> bool:
        job = await self._repository.claim_next()
        if job is None:
            return False
        try:
            outcome = await self._synchronizer.synchronize_turn(
                owner_user_id=job.owner_user_id,
                thread_id=job.thread_id,
                user_message_id=job.user_message_id,
                assistant_message_id=job.assistant_message_id,
                user_message=job.user_message,
                assistant_message=job.assistant_message,
                user_created_at=job.user_created_at,
                assistant_created_at=job.assistant_created_at,
            )
        except ZepSyncPermanentError as error:
            await self._repository.mark_terminal(
                job,
                error_code=str(error),
            )
            self._logger.error(
                "[zep_sync] job_terminal job_id=%s error_code=%s",
                job.job_id,
                str(error),
            )
        except Exception as error:
            await self._repository.mark_retry(
                job,
                error_code=type(error).__name__,
                delay_seconds=retry_delay_seconds(job.attempt_count),
            )
            self._logger.warning(
                "[zep_sync] job_retry job_id=%s error_type=%s attempts=%s",
                job.job_id,
                type(error).__name__,
                job.attempt_count,
            )
        else:
            await self._repository.mark_completed(job)
            self._logger.info(
                "[zep_sync] job_completed job_id=%s outcome=%s attempts=%s",
                job.job_id,
                outcome,
                job.attempt_count,
            )
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                processed = False
                self._logger.error(
                    "[zep_sync] worker_cycle_failed error_type=%s",
                    type(error).__name__,
                )
            if processed:
                continue
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll_seconds,
                )
            except asyncio.TimeoutError:
                pass


__all__ = [
    "ZepSyncWorker",
    "ZepTurnSyncJob",
    "retry_delay_seconds",
]
