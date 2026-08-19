from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
import unittest
from uuid import UUID

from seebx.capabilities.conversation.erasure import (
    ChatHistoryClearResultV1,
    ConversationErasureService,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
AUTHORIZATION = "Bearer aaa.bbb.ccc"
ROOT = Path(__file__).resolve().parents[1]


def result() -> ChatHistoryClearResultV1:
    return ChatHistoryClearResultV1(
        operation_id=OPERATION,
        scope="all",
        deleted_message_count=4,
        deleted_thread_count=2,
        deleted_outbox_count=1,
        receipt_sha256="a" * 64,
        completed_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


class FakeRepository:
    def __init__(
        self,
        events: list[str],
        failures: list[Exception] | None = None,
    ) -> None:
        self.events = events
        self.failures = list(failures or [])
        self.calls = 0

    async def clear_history(self, **kwargs):
        self.events.append("postgres.clear")
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.kwargs = kwargs
        return result()


class FakeZepRuntime:
    def __init__(
        self,
        events: list[str],
        failures: list[Exception] | None = None,
    ) -> None:
        self.events = events
        self.failures = list(failures or [])
        self.delete_calls = 0

    @asynccontextmanager
    async def owner_erasure_barrier(self, owner_user_id: UUID):
        self.events.append("zep.barrier.enter")
        try:
            yield
        finally:
            self.events.append("zep.barrier.exit")

    async def delete_owner_memory(self, owner_user_id: UUID) -> None:
        self.events.append("zep.delete")
        self.delete_calls += 1
        if self.failures:
            raise self.failures.pop(0)


class ConversationErasureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_zep_delete_precedes_postgres_clear(self) -> None:
        events: list[str] = []
        repository = FakeRepository(events)
        zep = FakeZepRuntime(events)
        observed = await ConversationErasureService(
            repository=repository,  # type: ignore[arg-type]
            zep_runtime=zep,  # type: ignore[arg-type]
        ).clear_all_chat_and_memory(
            owner_user_id=OWNER,
            authorization=AUTHORIZATION,
            operation_id=OPERATION,
        )
        self.assertEqual(observed, result())
        self.assertEqual(
            events,
            [
                "zep.barrier.enter",
                "zep.delete",
                "postgres.clear",
                "zep.barrier.exit",
            ],
        )
        self.assertEqual(repository.kwargs["scope"], "all")

    async def test_zep_outage_never_reaches_postgres_clear(self) -> None:
        events: list[str] = []
        repository = FakeRepository(events)
        zep = FakeZepRuntime(events, [TimeoutError("offline")])
        service = ConversationErasureService(
            repository=repository,  # type: ignore[arg-type]
            zep_runtime=zep,  # type: ignore[arg-type]
        )
        with self.assertRaises(TimeoutError):
            await service.clear_all_chat_and_memory(
                owner_user_id=OWNER,
                authorization=AUTHORIZATION,
                operation_id=OPERATION,
            )
        self.assertEqual(repository.calls, 0)
        self.assertEqual(
            events,
            ["zep.barrier.enter", "zep.delete", "zep.barrier.exit"],
        )

    async def test_postgres_retry_repeats_idempotent_zep_delete(self) -> None:
        events: list[str] = []
        repository = FakeRepository(events, [RuntimeError("database offline")])
        zep = FakeZepRuntime(events)
        service = ConversationErasureService(
            repository=repository,  # type: ignore[arg-type]
            zep_runtime=zep,  # type: ignore[arg-type]
        )
        with self.assertRaises(RuntimeError):
            await service.clear_all_chat_and_memory(
                owner_user_id=OWNER,
                authorization=AUTHORIZATION,
                operation_id=OPERATION,
            )
        observed = await service.clear_all_chat_and_memory(
            owner_user_id=OWNER,
            authorization=AUTHORIZATION,
            operation_id=OPERATION,
        )
        self.assertEqual(observed, result())
        self.assertEqual(zep.delete_calls, 2)
        self.assertEqual(repository.calls, 2)

    def test_route_cannot_reach_protected_lifeswitch_postgres(self) -> None:
        source = (
            ROOT / "seebx/capabilities/conversation/erasure_routes.py"
        ).read_text()
        route = source.index(
            '@router.delete("/memory/chat-and-zep/clear")'
        )
        block = source[route : route + 3_500]
        self.assertIn("_require_verified_deletion_actor", block)
        helper_start = source.index(
            "async def _require_verified_deletion_actor"
        )
        helper = source[helper_start : helper_start + 1_000]
        self.assertIn("require_verified_supabase_request_identity", helper)
        self.assertIn("clear_all_chat_and_memory", block)
        self.assertNotIn("LIFESWITCH_POSTGRES_DSN", block)
        self.assertNotIn("lifeswitch_nutrition", block)
        self.assertNotIn("lifeswitch_training", block)
        self.assertNotIn("lifeswitch_measurement_entries", block)
        self.assertNotIn("lifeswitch_snapshot", block)


if __name__ == "__main__":
    unittest.main()
