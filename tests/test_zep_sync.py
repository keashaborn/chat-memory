from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch
from uuid import UUID

from seebx.adapters.zep_cloud import ZepSyncPermanentError
from seebx.capabilities.conversation.zep_sync import (
    ZepSyncWorker,
    ZepTurnSyncJob,
    retry_delay_seconds,
)
from seebx.capabilities.conversation.zep_runtime import ZepSyncController


JOB = UUID("11111111-1111-4111-8111-111111111111")
LEASE = UUID("22222222-2222-4222-8222-222222222222")
OWNER = UUID("33333333-3333-4333-8333-333333333333")
THREAD = UUID("44444444-4444-4444-8444-444444444444")
USER_MESSAGE = UUID("55555555-5555-4555-8555-555555555555")
ASSISTANT_MESSAGE = JOB
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def job(attempt_count: int = 1) -> ZepTurnSyncJob:
    return ZepTurnSyncJob(
        job_id=JOB,
        lease_token=LEASE,
        owner_user_id=OWNER,
        thread_id=THREAD,
        user_message_id=USER_MESSAGE,
        assistant_message_id=ASSISTANT_MESSAGE,
        user_message="user text",
        assistant_message="assistant text",
        user_created_at=NOW,
        assistant_created_at=NOW,
        attempt_count=attempt_count,
    )


class FakeRepository:
    def __init__(self, jobs: list[ZepTurnSyncJob]) -> None:
        self.jobs = jobs
        self.completed: list[ZepTurnSyncJob] = []
        self.retries: list[tuple[ZepTurnSyncJob, str, int]] = []
        self.terminal: list[tuple[ZepTurnSyncJob, str]] = []

    async def claim_next(self):
        return self.jobs.pop(0) if self.jobs else None

    async def mark_completed(self, value):
        self.completed.append(value)

    async def mark_retry(self, value, *, error_code, delay_seconds):
        self.retries.append((value, error_code, delay_seconds))

    async def mark_terminal(self, value, *, error_code):
        self.terminal.append((value, error_code))


class FakeSynchronizer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def synchronize_turn(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return "added"


class ZepSyncWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_marks_exact_leased_job_completed(self) -> None:
        repository = FakeRepository([job()])
        synchronizer = FakeSynchronizer()
        worker = ZepSyncWorker(
            repository=repository,
            synchronizer=synchronizer,
            poll_seconds=0.05,
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(repository.completed, [job()])
        self.assertEqual(repository.retries, [])
        self.assertEqual(synchronizer.calls[0]["owner_user_id"], OWNER)
        self.assertEqual(synchronizer.calls[0]["thread_id"], THREAD)

    async def test_transient_failure_is_retried_with_bounded_backoff(self) -> None:
        repository = FakeRepository([job(4)])
        synchronizer = FakeSynchronizer(RuntimeError("provider down"))
        worker = ZepSyncWorker(
            repository=repository,
            synchronizer=synchronizer,
            poll_seconds=0.05,
            logger=logging.getLogger("test.zep-sync-retry"),
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(
            repository.retries,
            [(job(4), "RuntimeError", 16)],
        )
        self.assertEqual(repository.completed, [])

    async def test_permanent_failure_is_visible_and_not_retried(self) -> None:
        repository = FakeRepository([job()])
        synchronizer = FakeSynchronizer(
            ZepSyncPermanentError("zep_turn_order_conflict")
        )
        worker = ZepSyncWorker(
            repository=repository,
            synchronizer=synchronizer,
            poll_seconds=0.05,
            logger=logging.getLogger("test.zep-sync-terminal"),
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(
            repository.terminal,
            [(job(), "zep_turn_order_conflict")],
        )
        self.assertEqual(repository.retries, [])

    async def test_empty_queue_is_not_work(self) -> None:
        worker = ZepSyncWorker(
            repository=FakeRepository([]),
            synchronizer=FakeSynchronizer(),
            poll_seconds=0.05,
        )
        self.assertFalse(await worker.process_once())

    def test_retry_backoff_is_capped(self) -> None:
        self.assertEqual(retry_delay_seconds(1), 2)
        self.assertEqual(retry_delay_seconds(4), 16)
        self.assertEqual(retry_delay_seconds(100), 256)


class ZepSyncControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_controller_allocates_no_resources(self) -> None:
        controller = ZepSyncController(
            SimpleNamespace(sync_configured=False),  # type: ignore[arg-type]
            "",
        )
        with patch(
            "seebx.capabilities.conversation.zep_runtime.ZepSyncWorker"
        ) as worker_type:
            await controller.start()
        worker_type.assert_not_called()
        controller.notify()
        await controller.stop()

    async def test_enabled_controller_requires_postgres_dsn(self) -> None:
        controller = ZepSyncController(
            SimpleNamespace(sync_configured=True),  # type: ignore[arg-type]
            "  ",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "zep_sync_postgres_dsn_required",
        ):
            await controller.start()

    async def test_controller_starts_notifies_and_stops_one_worker(self) -> None:
        runtime = SimpleNamespace(sync_configured=True)
        controller = ZepSyncController(  # type: ignore[arg-type]
            runtime,
            "postgresql://db.invalid/conversation",
        )
        with (
            patch(
                "seebx.capabilities.conversation.zep_runtime."
                "PostgresConnectionProvider"
            ) as provider_type,
            patch(
                "seebx.capabilities.conversation.zep_runtime."
                "PostgresZepSyncRepository"
            ) as repository_type,
            patch(
                "seebx.capabilities.conversation.zep_runtime.ZepSyncWorker"
            ) as worker_type,
        ):
            worker = worker_type.return_value
            worker.stop = AsyncMock()
            await controller.start()
            provider_type.assert_called_once_with(
                "postgresql://db.invalid/conversation"
            )
            repository_type.assert_called_once_with(provider_type.return_value)
            worker_type.assert_called_once_with(
                repository=repository_type.return_value,
                synchronizer=runtime,
                logger=ANY,
            )
            worker.start.assert_called_once_with()
            controller.notify()
            worker.notify.assert_called_once_with()
            with self.assertRaisesRegex(
                RuntimeError,
                "zep_sync_controller_already_started",
            ):
                await controller.start()
            await controller.stop()
            worker.stop.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
