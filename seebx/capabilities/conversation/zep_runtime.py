from __future__ import annotations

"""Shared Zep runtime and restart-safe synchronization lifecycle."""

import logging
import os

from seebx.capabilities.conversation.zep_memory import ZepPromptSettingsV1
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.adapters.zep_cloud import ZepRuntime
from seebx.adapters.zep_sync_postgres import PostgresZepSyncRepository
from seebx.capabilities.conversation.zep_sync import ZepSyncWorker


logger = logging.getLogger("uvicorn.error")

ZEP_MEMORY_RUNTIME = ZepRuntime.from_environment(os.environ, logger=logger)
ZEP_PROMPT_SETTINGS = ZepPromptSettingsV1.from_environment(os.environ)


class ZepSyncController:
    def __init__(self, runtime: ZepRuntime, dsn: str) -> None:
        self._runtime = runtime
        self._dsn = dsn.strip() if isinstance(dsn, str) else ""
        self._worker: ZepSyncWorker | None = None

    async def start(self) -> None:
        if not self._runtime.sync_configured:
            return
        if self._worker is not None:
            raise RuntimeError("zep_sync_controller_already_started")
        if not self._dsn:
            raise RuntimeError("zep_sync_postgres_dsn_required")
        repository = PostgresZepSyncRepository(
            PostgresConnectionProvider(self._dsn)
        )
        self._worker = ZepSyncWorker(
            repository=repository,
            synchronizer=self._runtime,
            logger=logger,
        )
        self._worker.start()

    def notify(self) -> None:
        if self._worker is not None:
            self._worker.notify()

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await worker.stop()
        self._worker = None


ZEP_SYNC_CONTROLLER = ZepSyncController(
    ZEP_MEMORY_RUNTIME,
    os.getenv("POSTGRES_DSN", ""),
)


__all__ = [
    "ZEP_MEMORY_RUNTIME",
    "ZEP_PROMPT_SETTINGS",
    "ZEP_SYNC_CONTROLLER",
    "ZepSyncController",
]
