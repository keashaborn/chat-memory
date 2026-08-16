from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from rag_engine.zep_shadow_memory_v1 import (
    ZEP_SHADOW_MODE_CANARY,
    ZEP_SHADOW_MODE_OFF,
    ZEP_SHADOW_MODE_ON,
    ZepCloudShadowTransportV1,
    ZepShadowConfigurationError,
    ZepShadowOwnershipError,
    ZepShadowRuntimeV1,
    ZepShadowSettingsV1,
    zep_thread_id_v1,
    zep_user_id_v1,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
THREAD = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
USER_MESSAGE = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ASSISTANT_MESSAGE = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(
        self,
        *,
        fail: bool = False,
        context: str = "shadow context",
    ) -> None:
        self.fail = fail
        self.context = context
        self.provisioned: list[tuple[str, str]] = []
        self.turns: list[tuple[str, UUID, UUID, str, str]] = []
        self.context_requests: list[tuple[str, str]] = []
        self.closed = False

    async def ensure_user_and_thread(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        self.provisioned.append((user_id, thread_id))

    async def add_turn(
        self,
        *,
        thread_id: str,
        user_message_id: UUID,
        assistant_message_id: UUID,
        user_message: str,
        assistant_message: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        self.turns.append(
            (
                thread_id,
                user_message_id,
                assistant_message_id,
                user_message,
                assistant_message,
            )
        )

    async def get_owner_context(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> str:
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        self.context_requests.append((user_id, thread_id))
        return self.context

    async def close(self) -> None:
        self.closed = True


def canary_settings() -> ZepShadowSettingsV1:
    return ZepShadowSettingsV1(
        mode=ZEP_SHADOW_MODE_CANARY,
        owner_user_ids=frozenset((OWNER,)),
        timeout_seconds=1.0,
    )


class ZepShadowSettingsTests(unittest.TestCase):
    def test_defaults_off(self) -> None:
        settings = ZepShadowSettingsV1.from_environment({})
        self.assertEqual(settings.mode, ZEP_SHADOW_MODE_OFF)
        self.assertFalse(settings.enabled_for(OWNER))

    def test_canary_requires_valid_owner_allowlist(self) -> None:
        with self.assertRaisesRegex(
            ZepShadowConfigurationError,
            "zep_shadow_canary_owner_ids_required",
        ):
            ZepShadowSettingsV1.from_environment(
                {"ZEP_SHADOW_MODE": "canary"}
            )
        with self.assertRaisesRegex(
            ZepShadowConfigurationError,
            "invalid_zep_shadow_owner_ids",
        ):
            ZepShadowSettingsV1.from_environment(
                {
                    "ZEP_SHADOW_MODE": "canary",
                    "ZEP_SHADOW_OWNER_IDS": "not-a-uuid",
                }
            )

    def test_canary_is_exact_owner_scoped(self) -> None:
        settings = ZepShadowSettingsV1.from_environment(
            {
                "ZEP_SHADOW_MODE": "canary",
                "ZEP_SHADOW_OWNER_IDS": str(OWNER),
                "ZEP_SHADOW_TIMEOUT_SECONDS": "2.5",
            }
        )
        self.assertTrue(settings.enabled_for(OWNER))
        self.assertFalse(settings.enabled_for(OTHER_OWNER))
        self.assertEqual(settings.timeout_seconds, 2.5)

    def test_on_mode_accepts_every_verified_owner_without_an_allowlist(
        self,
    ) -> None:
        settings = ZepShadowSettingsV1.from_environment(
            {"ZEP_SHADOW_MODE": "on"}
        )
        self.assertEqual(settings.mode, ZEP_SHADOW_MODE_ON)
        self.assertTrue(settings.enabled_for(OWNER))
        self.assertTrue(settings.enabled_for(OTHER_OWNER))

    def test_identifiers_are_stable_and_domain_separated(self) -> None:
        self.assertEqual(zep_user_id_v1(OWNER), f"lifeswitch-user-{OWNER}")
        self.assertEqual(
            zep_thread_id_v1(THREAD),
            f"lifeswitch-thread-{THREAD}",
        )

    def test_route_uses_authenticated_owner_after_transcript_persistence(
        self,
    ) -> None:
        route = (ROOT / "rag_engine/resse_response_router.py").read_text()
        dispatch = route.index("ZEP_SHADOW_RUNTIME.dispatch_turn(")
        self.assertLess(
            route.index("await persist_finalized_response_v3("),
            dispatch,
        )
        self.assertLess(
            route.index("await persist_finalized_response_v1("),
            dispatch,
        )
        dispatch_block = route[dispatch : dispatch + 400]
        self.assertIn("owner_user_id=owner", dispatch_block)
        self.assertNotIn("owner_user_id=payload.user_id", dispatch_block)
        self.assertIn("user_message_id=payload.message_id", dispatch_block)
        self.assertIn("assistant_message_id=finalized.answer_id", dispatch_block)

    def test_retrieval_is_owner_bound_and_has_no_prompt_binding(self) -> None:
        adapter = (ROOT / "rag_engine/zep_shadow_memory_v1.py").read_text()
        route = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("get_user_context", adapter)
        self.assertIn("user.get_threads", adapter)
        self.assertNotIn(".graph.search(", adapter)
        self.assertIn("prompt_bound=false", adapter)
        retrieval = route.index("ZEP_SHADOW_RUNTIME.dispatch_retrieval(")
        self.assertLess(route.index("owner = actor_context.owner_user_id"), retrieval)
        self.assertLess(retrieval, route.index("openai_client = get_openai_client()"))
        retrieval_block = route[retrieval : retrieval + 220]
        self.assertIn("owner_user_id=owner", retrieval_block)
        self.assertIn("thread_id=thread_id", retrieval_block)
        command_start = route.index("command = AuthenticatedResponseCommandV0_2(")
        command_block = route[command_start : command_start + 1_200]
        self.assertNotIn("zep", command_block.casefold())


class ZepCloudShadowTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(
        self,
        *,
        threads: list[object],
        context: object = "owner context",
    ) -> tuple[ZepCloudShadowTransportV1, AsyncMock]:
        get_user_context = AsyncMock(
            return_value=SimpleNamespace(context=context)
        )
        client = SimpleNamespace(
            user=SimpleNamespace(
                get_threads=AsyncMock(return_value=threads),
            ),
            thread=SimpleNamespace(get_user_context=get_user_context),
        )
        transport = object.__new__(ZepCloudShadowTransportV1)
        transport._client = client
        return transport, get_user_context

    async def test_owner_thread_match_is_required_before_context_read(
        self,
    ) -> None:
        user_id = zep_user_id_v1(OWNER)
        thread_id = zep_thread_id_v1(THREAD)
        transport, get_user_context = self.transport(
            threads=[SimpleNamespace(user_id=user_id, thread_id=thread_id)]
        )
        self.assertEqual(
            await transport.get_owner_context(
                user_id=user_id,
                thread_id=thread_id,
            ),
            "owner context",
        )
        get_user_context.assert_awaited_once_with(thread_id=thread_id)

    async def test_foreign_missing_and_duplicate_owner_bindings_fail_closed(
        self,
    ) -> None:
        user_id = zep_user_id_v1(OWNER)
        thread_id = zep_thread_id_v1(THREAD)
        cases = (
            [],
            [SimpleNamespace(user_id="foreign-user", thread_id=thread_id)],
            [
                SimpleNamespace(user_id=user_id, thread_id=thread_id),
                SimpleNamespace(user_id=user_id, thread_id=thread_id),
            ],
        )
        for threads in cases:
            with self.subTest(threads=threads):
                transport, get_user_context = self.transport(threads=threads)
                with self.assertRaisesRegex(
                    ZepShadowOwnershipError,
                    "zep_thread_owner_mismatch",
                ):
                    await transport.get_owner_context(
                        user_id=user_id,
                        thread_id=thread_id,
                    )
                get_user_context.assert_not_awaited()

    async def test_invalid_or_oversized_context_fails_closed(self) -> None:
        user_id = zep_user_id_v1(OWNER)
        thread_id = zep_thread_id_v1(THREAD)
        threads = [SimpleNamespace(user_id=user_id, thread_id=thread_id)]
        for context, expected in (
            (None, "invalid_zep_context"),
            ("x" * 1_048_577, "zep_context_too_large"),
        ):
            with self.subTest(expected=expected):
                transport, _ = self.transport(
                    threads=threads,
                    context=context,
                )
                with self.assertRaisesRegex(
                    ZepShadowConfigurationError,
                    expected,
                ):
                    await transport.get_owner_context(
                        user_id=user_id,
                        thread_id=thread_id,
                    )


class ZepShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_mode_rejects_non_uuid_authority_before_transport(self) -> None:
        calls = []

        def factory(api_key: str) -> FakeTransport:
            calls.append(api_key)
            return FakeTransport()

        runtime = ZepShadowRuntimeV1(
            settings=ZepShadowSettingsV1(
                mode=ZEP_SHADOW_MODE_ON,
                owner_user_ids=frozenset(),
                timeout_seconds=1.0,
            ),
            api_key="present",
            transport_factory=factory,
        )
        self.assertEqual(
            runtime.dispatch_turn(
                owner_user_id="not-a-uuid",  # type: ignore[arg-type]
                thread_id=THREAD,
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
            ),
            "excluded",
        )
        await runtime.close()
        self.assertEqual(calls, [])

    async def test_off_mode_never_constructs_transport(self) -> None:
        calls = []

        def factory(api_key: str) -> FakeTransport:
            calls.append(api_key)
            return FakeTransport()

        runtime = ZepShadowRuntimeV1(
            settings=ZepShadowSettingsV1(
                mode=ZEP_SHADOW_MODE_OFF,
                owner_user_ids=frozenset(),
                timeout_seconds=1.0,
            ),
            api_key="present",
            transport_factory=factory,
        )
        outcome = runtime.dispatch_turn(
            owner_user_id=OWNER,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
        )
        await runtime.close()
        self.assertEqual(outcome, "disabled")
        self.assertEqual(calls, [])

    async def test_canary_writes_pair_after_dispatch(self) -> None:
        transport = FakeTransport()
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
        )
        outcome = runtime.dispatch_turn(
            owner_user_id=OWNER,
            thread_id=THREAD,
            user_message_id=USER_MESSAGE,
            assistant_message_id=ASSISTANT_MESSAGE,
            user_message="user text",
            assistant_message="assistant text",
        )
        await runtime.close()
        self.assertEqual(outcome, "scheduled")
        self.assertEqual(
            transport.provisioned,
            [(zep_user_id_v1(OWNER), zep_thread_id_v1(THREAD))],
        )
        self.assertEqual(
            transport.turns,
            [
                (
                    zep_thread_id_v1(THREAD),
                    USER_MESSAGE,
                    ASSISTANT_MESSAGE,
                    "user text",
                    "assistant text",
                )
            ],
        )
        self.assertTrue(transport.closed)

    async def test_retrieval_is_owner_and_thread_scoped_but_content_free_in_logs(
        self,
    ) -> None:
        context = "SENSITIVE SHADOW CONTEXT"
        transport = FakeTransport(context=context)
        logger = logging.getLogger("test.zep-shadow-retrieval")
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
            logger=logger,
        )
        with self.assertLogs(logger, level="INFO") as captured:
            outcome = runtime.dispatch_retrieval(
                owner_user_id=OWNER,
                thread_id=THREAD,
            )
            await runtime.close()
        self.assertEqual(outcome, "scheduled")
        self.assertEqual(
            transport.context_requests,
            [(zep_user_id_v1(OWNER), zep_thread_id_v1(THREAD))],
        )
        rendered = "\n".join(captured.output)
        self.assertIn("retrieval_succeeded", rendered)
        self.assertIn("prompt_bound=false", rendered)
        self.assertIn("context_bytes=24", rendered)
        self.assertNotIn(context, rendered)
        self.assertTrue(transport.closed)

    async def test_foreign_owner_retrieval_never_reaches_transport(self) -> None:
        transport = FakeTransport()
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
        )
        self.assertEqual(
            runtime.dispatch_retrieval(
                owner_user_id=OTHER_OWNER,
                thread_id=THREAD,
            ),
            "disabled",
        )
        await runtime.close()
        self.assertEqual(transport.context_requests, [])

    async def test_missing_key_and_foreign_owner_fail_closed(self) -> None:
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="",
            transport_factory=lambda _: FakeTransport(),
            logger=logging.getLogger("test.zep-shadow"),
        )
        self.assertEqual(
            runtime.dispatch_turn(
                owner_user_id=OWNER,
                thread_id=THREAD,
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
            ),
            "misconfigured",
        )
        self.assertEqual(
            runtime.dispatch_turn(
                owner_user_id=OTHER_OWNER,
                thread_id=THREAD,
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
            ),
            "disabled",
        )
        await runtime.close()

    async def test_transport_failure_never_escapes_background_task(self) -> None:
        transport = FakeTransport(fail=True)
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
            logger=logging.getLogger("test.zep-shadow"),
        )
        self.assertEqual(
            runtime.dispatch_turn(
                owner_user_id=OWNER,
                thread_id=THREAD,
                user_message_id=USER_MESSAGE,
                assistant_message_id=ASSISTANT_MESSAGE,
                user_message="user text",
                assistant_message="assistant text",
            ),
            "scheduled",
        )
        await runtime.close()
        self.assertTrue(transport.closed)

    async def test_retrieval_failure_never_changes_request_control_flow(self) -> None:
        transport = FakeTransport(fail=True)
        logger = logging.getLogger("test.zep-shadow-retrieval-failure")
        runtime = ZepShadowRuntimeV1(
            settings=canary_settings(),
            api_key="test-key",
            transport_factory=lambda _: transport,
            logger=logger,
        )
        with self.assertLogs(logger, level="ERROR") as captured:
            self.assertEqual(
                runtime.dispatch_retrieval(
                    owner_user_id=OWNER,
                    thread_id=THREAD,
                ),
                "scheduled",
            )
            await runtime.close()
        rendered = "\n".join(captured.output)
        self.assertIn("retrieval_failed", rendered)
        self.assertIn("prompt_bound=false", rendered)
        self.assertNotIn("synthetic transport failure", rendered)


if __name__ == "__main__":
    unittest.main()
