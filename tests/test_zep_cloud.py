from __future__ import annotations

import asyncio
import logging
from collections import deque
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from seebx.adapters.zep_cloud import (
    ZEP_SYNC_MODE_CANARY,
    ZEP_SYNC_MODE_OFF,
    ZEP_SYNC_MODE_ON,
    ZepCloudTransport,
    ZepConfigurationError,
    ZepOwnershipError,
    ZepRuntime,
    ZepSettings,
    ZepSyncPermanentError,
    zep_thread_id,
    zep_user_id,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
THREAD = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
USER_MESSAGE = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ASSISTANT_MESSAGE = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class NotFoundError(RuntimeError):
    status_code = 404


class FakeMessage:
    def __init__(self, **kwargs: object) -> None:
        self.values = kwargs


def transport_with_episode_states(
    states: dict[UUID, bool],
) -> tuple[ZepCloudTransport, AsyncMock, AsyncMock]:
    async def get_episode(message_id: str):
        parsed = UUID(message_id)
        if not states.get(parsed, False):
            raise NotFoundError()
        return SimpleNamespace(
            uuid_=message_id,
            thread_id=zep_thread_id(THREAD),
        )

    async def add_messages(*_: object, **kwargs: object):
        sent = kwargs["messages"]
        ids = [UUID(message.values["uuid_"]) for message in sent]
        for message_id in ids:
            states[message_id] = True
        return SimpleNamespace(message_uuids=[str(value) for value in ids])

    episode_get = AsyncMock(side_effect=get_episode)
    add = AsyncMock(side_effect=add_messages)
    value = object.__new__(ZepCloudTransport)
    value._client = SimpleNamespace(
        graph=SimpleNamespace(episode=SimpleNamespace(get=episode_get)),
        thread=SimpleNamespace(add_messages=add),
    )
    value._message_type = FakeMessage
    return value, episode_get, add


class ZepSettingsTests(unittest.TestCase):
    def test_canonical_sync_settings_default_off(self) -> None:
        settings = ZepSettings.from_environment({})
        self.assertEqual(settings.mode, ZEP_SYNC_MODE_OFF)
        self.assertFalse(settings.enabled_for(OWNER))

    def test_on_and_canary_are_exact_owner_scoped(self) -> None:
        on = ZepSettings.from_environment({"ZEP_SYNC_MODE": "on"})
        self.assertEqual(on.mode, ZEP_SYNC_MODE_ON)
        self.assertTrue(on.enabled_for(OWNER))
        canary = ZepSettings.from_environment(
            {
                "ZEP_SYNC_MODE": "canary",
                "ZEP_SYNC_OWNER_IDS": str(OWNER),
                "ZEP_TIMEOUT_SECONDS": "2.5",
            }
        )
        self.assertEqual(canary.mode, ZEP_SYNC_MODE_CANARY)
        self.assertTrue(canary.enabled_for(OWNER))
        self.assertFalse(canary.enabled_for(OTHER_OWNER))

    def test_legacy_shadow_environment_is_not_runtime_authority(self) -> None:
        settings = ZepSettings.from_environment(
            {"ZEP_SHADOW_MODE": "on", "ZEP_SHADOW_OWNER_IDS": str(OWNER)}
        )
        self.assertEqual(settings.mode, ZEP_SYNC_MODE_OFF)

    def test_canary_requires_owner_allowlist(self) -> None:
        with self.assertRaisesRegex(
            ZepConfigurationError,
            "zep_sync_canary_owner_ids_required",
        ):
            ZepSettings.from_environment({"ZEP_SYNC_MODE": "canary"})


class ZepCloudTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_user_and_thread_are_owner_verified(self) -> None:
        user = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(user_id=zep_user_id(OWNER))
            ),
            add=AsyncMock(),
        )
        thread = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(user_id=zep_user_id(OWNER))
            ),
            create=AsyncMock(),
        )
        transport = object.__new__(ZepCloudTransport)
        transport._client = SimpleNamespace(user=user, thread=thread)
        await transport.ensure_user_and_thread(
            user_id=zep_user_id(OWNER),
            thread_id=zep_thread_id(THREAD),
        )
        user.add.assert_not_awaited()
        thread.create.assert_not_awaited()

    async def test_cross_owner_thread_is_rejected(self) -> None:
        transport = object.__new__(ZepCloudTransport)
        transport._client = SimpleNamespace(
            user=SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(user_id=zep_user_id(OWNER))
                )
            ),
            thread=SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        user_id=zep_user_id(OTHER_OWNER)
                    )
                )
            ),
        )
        with self.assertRaisesRegex(
            ZepOwnershipError,
            "zep_thread_owner_mismatch",
        ):
            await transport.ensure_user_and_thread(
                user_id=zep_user_id(OWNER),
                thread_id=zep_thread_id(THREAD),
            )

    async def test_absent_turn_is_added_and_verified_with_stable_ids(self) -> None:
        transport, episode_get, add = transport_with_episode_states({})
        outcome = await transport.reconcile_turn(
            thread_id=zep_thread_id(THREAD),
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
            user_created_at=NOW,
            assistant_created_at=NOW,
        )
        self.assertEqual(outcome, "added")
        sent = add.await_args.kwargs["messages"]
        self.assertEqual(
            [item.values["uuid_"] for item in sent],
            [str(USER_MESSAGE), str(ASSISTANT_MESSAGE)],
        )
        self.assertEqual(
            [item.values["created_at"] for item in sent],
            ["2026-08-18T12:00:00Z", "2026-08-18T12:00:00Z"],
        )
        self.assertEqual(episode_get.await_count, 4)

    async def test_retry_after_unknown_commit_does_not_add_duplicates(self) -> None:
        states = {USER_MESSAGE: True, ASSISTANT_MESSAGE: True}
        transport, _, add = transport_with_episode_states(states)
        outcome = await transport.reconcile_turn(
            thread_id=zep_thread_id(THREAD),
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
            user_created_at=NOW,
            assistant_created_at=NOW,
        )
        self.assertEqual(outcome, "already_present")
        add.assert_not_awaited()

    async def test_user_only_retry_adds_only_missing_assistant(self) -> None:
        transport, _, add = transport_with_episode_states(
            {USER_MESSAGE: True}
        )
        await transport.reconcile_turn(
            thread_id=zep_thread_id(THREAD),
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
            user_created_at=NOW,
            assistant_created_at=NOW,
        )
        sent = add.await_args.kwargs["messages"]
        self.assertEqual(
            [item.values["uuid_"] for item in sent],
            [str(ASSISTANT_MESSAGE)],
        )

    async def test_assistant_without_user_fails_terminal(self) -> None:
        transport, _, add = transport_with_episode_states(
            {ASSISTANT_MESSAGE: True}
        )
        with self.assertRaisesRegex(
            ZepSyncPermanentError,
            "zep_turn_order_conflict",
        ):
            await transport.reconcile_turn(
                thread_id=zep_thread_id(THREAD),
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
                user_created_at=NOW,
                assistant_created_at=NOW,
            )
        add.assert_not_awaited()

    async def test_message_limit_and_receipt_mismatch_fail_terminal(self) -> None:
        transport, _, _ = transport_with_episode_states({})
        with self.assertRaisesRegex(
            ZepSyncPermanentError,
            "zep_message_too_large",
        ):
            await transport.reconcile_turn(
                thread_id=zep_thread_id(THREAD),
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="x" * 4_097,
                assistant_message="assistant text",
                user_created_at=NOW,
                assistant_created_at=NOW,
            )

    async def test_owner_delete_is_verified_and_idempotent(self) -> None:
        user = SimpleNamespace(
            delete=AsyncMock(return_value=SimpleNamespace(message="ok")),
            get=AsyncMock(side_effect=NotFoundError()),
        )
        transport = object.__new__(ZepCloudTransport)
        transport._client = SimpleNamespace(user=user)
        await transport.delete_owner(user_id=zep_user_id(OWNER))
        user.delete.assert_awaited_once_with(user_id=zep_user_id(OWNER))

    async def test_owner_export_enumerates_messages_and_graph(self) -> None:
        provider_user_id = zep_user_id(OWNER)
        provider_thread_id = zep_thread_id(THREAD)

        class RawEndpoint:
            def __init__(self, pages: list[list[object]]) -> None:
                self.pages = deque(pages)
                self.with_raw_response = self
                self.cursors: list[object] = []

            async def get_by_user_id(self, **kwargs: object):
                page = self.pages.popleft()
                self.cursors.append(kwargs.get("cursor"))
                headers = (
                    {"Zep-Next-Cursor": "next-page"}
                    if self.pages
                    else {}
                )
                return SimpleNamespace(data=page, headers=headers)

        node = SimpleNamespace(uuid_="node-1", name="Owner")
        node_2 = SimpleNamespace(uuid_="node-2", name="Project")
        edge = SimpleNamespace(uuid_="edge-1", fact="likes tea")
        observation = SimpleNamespace(uuid_="observation-1", content="pattern")
        summary = SimpleNamespace(uuid_="summary-1", summary="summary")
        user = SimpleNamespace(
            user_id=provider_user_id,
            model_dump=lambda **_: {"user_id": provider_user_id},
        )
        thread = SimpleNamespace(
            user_id=provider_user_id,
            thread_id=provider_thread_id,
            model_dump=lambda **_: {
                "user_id": provider_user_id,
                "thread_id": provider_thread_id,
            },
        )
        message = SimpleNamespace(
            uuid_=str(USER_MESSAGE),
            model_dump=lambda **_: {
                "uuid": str(USER_MESSAGE),
                "content": "hello",
            }
        )
        message_2 = SimpleNamespace(
            uuid_=str(ASSISTANT_MESSAGE),
            model_dump=lambda **_: {
                "uuid": str(ASSISTANT_MESSAGE),
                "content": "response",
            }
        )
        message_pages = deque(
            (
                SimpleNamespace(
                    user_id=provider_user_id,
                    total_count=2,
                    messages=[message],
                ),
                SimpleNamespace(
                    user_id=provider_user_id,
                    total_count=2,
                    messages=[message_2],
                ),
            )
        )
        client = SimpleNamespace(
            user=SimpleNamespace(
                get=AsyncMock(return_value=user),
                get_threads=AsyncMock(return_value=[thread]),
            ),
            thread=SimpleNamespace(
                get=AsyncMock(side_effect=lambda **_: message_pages.popleft())
            ),
            graph=SimpleNamespace(
                node=RawEndpoint([[node], [node_2]]),
                edge=RawEndpoint([[edge]]),
                observation=RawEndpoint([[observation]]),
                thread_summary=RawEndpoint([[summary]]),
            ),
        )
        transport = object.__new__(ZepCloudTransport)
        transport._client = client
        result = await transport.export_owner(user_id=provider_user_id)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["threads"][0]["messages"][0]["content"],
            "hello",
        )
        self.assertEqual(
            result["threads"][0]["messages"][1]["content"],
            "response",
        )
        self.assertEqual(result["graph"]["nodes"][0]["name"], "Owner")
        self.assertEqual(result["graph"]["nodes"][1]["name"], "Project")
        self.assertEqual(result["graph"]["edges"][0]["fact"], "likes tea")
        self.assertEqual(result["raw_episode_basis"], "thread_messages")
        self.assertEqual(
            result["provider_limitations"],
            ["non_thread_graph_episodes_not_enumerated"],
        )
        self.assertEqual(
            result["counts"],
            {
                "threads": 1, "messages": 2, "nodes": 2, "edges": 1,
                "observations": 1, "thread_summaries": 1,
            },
        )
        self.assertEqual(client.graph.node.cursors, [None, "next-page"])
        self.assertEqual([call.kwargs["cursor"] for call in client.thread.get.await_args_list], [0, 1])

    async def test_absent_owner_exports_explicit_absence(self) -> None:
        transport = object.__new__(ZepCloudTransport)
        transport._client = SimpleNamespace(
            user=SimpleNamespace(get=AsyncMock(side_effect=NotFoundError()))
        )
        result = await transport.export_owner(user_id=zep_user_id(OWNER))
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["threads"], [])


class FakeTransport:
    def __init__(self) -> None:
        self.provisioned: list[tuple[str, str]] = []
        self.sync_calls = 0
        self.deleted: list[str] = []
        self.context = "prompt context"
        self.closed = False
        self.export = {"status": "complete", "provider": "zep"}

    async def ensure_user_and_thread(self, *, user_id: str, thread_id: str):
        self.provisioned.append((user_id, thread_id))

    async def reconcile_turn(self, **_: object) -> str:
        self.sync_calls += 1
        return "added"

    async def search_owner_context(self, *, user_id: str, query: str) -> str:
        self.search = (user_id, query)
        return self.context

    async def delete_owner(self, *, user_id: str) -> None:
        self.deleted.append(user_id)

    async def export_owner(self, *, user_id: str):
        self.exported_user = user_id
        return self.export

    async def close(self) -> None:
        self.closed = True


def canary_settings() -> ZepSettings:
    return ZepSettings(
        mode=ZEP_SYNC_MODE_CANARY,
        owner_user_ids=frozenset((OWNER,)),
        timeout_seconds=1.0,
    )


class ZepRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_is_awaited_and_owner_bound(self) -> None:
        transport = FakeTransport()
        runtime = ZepRuntime(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
        )
        outcome = await runtime.synchronize_turn(
            owner_user_id=OWNER,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
            user_created_at=NOW,
            assistant_created_at=NOW,
        )
        self.assertEqual(outcome, "added")
        self.assertEqual(transport.sync_calls, 1)
        self.assertEqual(
            transport.provisioned,
            [(zep_user_id(OWNER), zep_thread_id(THREAD))],
        )
        await runtime.close()

    async def test_prompt_retrieval_is_owner_scoped_and_content_free(self) -> None:
        transport = FakeTransport()
        logger = logging.getLogger("test.zep.prompt")
        runtime = ZepRuntime(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
            logger=logger,
        )
        with self.assertLogs(logger, level="INFO") as captured:
            context = await runtime.retrieve_prompt_context(
                owner_user_id=OWNER,
                thread_id=THREAD,
                current_message="What do you remember?",
            )
        self.assertEqual(context, "prompt context")
        self.assertNotIn("prompt context", "\n".join(captured.output))
        self.assertEqual(
            transport.search,
            (zep_user_id(OWNER), "What do you remember?"),
        )
        await runtime.close()

    async def test_erasure_barrier_blocks_new_owner_io(self) -> None:
        transport = FakeTransport()
        runtime = ZepRuntime(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
        )
        async with runtime.owner_erasure_barrier(OWNER):
            with self.assertRaisesRegex(
                ZepConfigurationError,
                "zep_owner_erasure_active",
            ):
                await runtime.retrieve_prompt_context(
                    owner_user_id=OWNER,
                    thread_id=THREAD,
                    current_message="query",
                )
            await runtime.delete_owner_memory(OWNER)
        self.assertEqual(transport.deleted, [zep_user_id(OWNER)])
        await runtime.close()

    async def test_barrier_drains_inflight_sync_before_delete(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        transport = FakeTransport()

        async def reconcile(**_: object) -> str:
            started.set()
            await release.wait()
            return "added"

        transport.reconcile_turn = reconcile  # type: ignore[method-assign]
        runtime = ZepRuntime(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
        )
        sync = asyncio.create_task(
            runtime.synchronize_turn(
                owner_user_id=OWNER,
                thread_id=THREAD,
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
                user_created_at=NOW,
                assistant_created_at=NOW,
            )
        )
        await started.wait()
        barrier_entered = asyncio.Event()

        async def erase() -> None:
            async with runtime.owner_erasure_barrier(OWNER):
                barrier_entered.set()
                await runtime.delete_owner_memory(OWNER)

        erase_task = asyncio.create_task(erase())
        await asyncio.sleep(0)
        self.assertFalse(barrier_entered.is_set())
        release.set()
        await sync
        await erase_task
        self.assertTrue(barrier_entered.is_set())
        await runtime.close()

    async def test_export_is_owner_scoped_and_content_free_in_logs(self) -> None:
        transport = FakeTransport()
        logger = logging.getLogger("test.zep.export")
        runtime = ZepRuntime(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
            logger=logger,
        )
        with self.assertLogs(logger, level="INFO") as captured:
            result = await runtime.export_owner_memory(OWNER)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(transport.exported_user, zep_user_id(OWNER))
        self.assertNotIn(str(OWNER), "\n".join(captured.output))
        await runtime.close()


if __name__ == "__main__":
    unittest.main()
