from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID

import asyncpg

from rag_engine.lifeswitch_chat_runtime_v1 import (
    LazyPostgresRestrictedLifeSwitchReadSessionV1,
    LifeSwitchChatPoolManagerV1,
    LifeSwitchChatRuntimeSettingsV1,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchResponseContextProviderV1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
THREAD = UUID("33333333-3333-4333-8333-333333333333")


async def main() -> None:
    dsn = os.environ["LIFESWITCH_GATEWAY_TEST_DSN"]
    factory_calls: list[dict[str, Any]] = []

    async def factory(**kwargs: Any) -> asyncpg.Pool:
        factory_calls.append(kwargs)
        return await asyncpg.create_pool(**kwargs)

    manager = LifeSwitchChatPoolManagerV1(
        LifeSwitchChatRuntimeSettingsV1(mode="on", dsn=dsn, pool_max_size=2),
        pool_factory=factory,
    )
    provider = LifeSwitchResponseContextProviderV1(
        LazyPostgresRestrictedLifeSwitchReadSessionV1(manager)
    )
    try:
        off = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=OWNER,
                thread_id=THREAD,
                current_request_id="clone-off-request",
                current_message="Who won America's Next Top Model in 2015?",
            ),
        )
        if off.status != "OFF" or off.database_accessed or factory_calls:
            raise AssertionError("OFF created a pool or accessed LifeSwitch data")

        active = await provider.prepare(
            authenticated_actor_user_id=OWNER,
            conversation_snapshot=create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=OWNER,
                thread_id=THREAD,
                current_request_id="clone-active-request",
                current_message="How am I doing with my current plan?",
            ),
        )
        if not active.database_accessed or len(factory_calls) != 1:
            raise AssertionError("active request did not use one dedicated pool")
        if active.status not in {"EMPTY", "SELECTED", "PARTIAL"}:
            raise AssertionError("active request returned an invalid status")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "off_database_accessed": off.database_accessed,
                    "off_pool_calls": 0,
                    "active_status": active.status,
                    "active_database_accessed": active.database_accessed,
                    "dedicated_pool_calls": len(factory_calls),
                    "active_record_count": (
                        sum(section.record_count for section in active.envelope.sections)
                        if active.envelope is not None
                        else 0
                    ),
                },
                sort_keys=True,
            )
        )
    finally:
        await manager.close()


asyncio.run(main())
