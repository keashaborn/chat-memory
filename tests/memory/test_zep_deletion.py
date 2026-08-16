from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.deletion_contracts import (
    ConversationErasureLease,
    ConversationErasureState,
    ConversationErasureTarget,
    DeletionSelectorKind,
    erasure_target_manifest_sha256,
    source_erasure_target_sha256,
)
from rag_engine.governed_memory.runtime.zep_deletion import (
    BoundedZepHttpsTransport,
    ZEP_API_PREFIX,
    ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT,
    ZepConversationDeletionV1,
    ZepDeletionUnavailable,
    ZepHttpResponse,
    zep_thread_id,
    zep_user_id,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OPERATION = UUID("22222222-2222-4222-8222-222222222222")
THREAD = UUID("33333333-3333-4333-8333-333333333333")
LEASE_TOKEN = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def targets(count: int = 1) -> tuple[ConversationErasureTarget, ...]:
    values = []
    for index in range(count):
        message_id = UUID(int=10_000 + index)
        created_at = NOW - timedelta(seconds=index + 1)
        values.append(
            ConversationErasureTarget(
                owner_user_id=OWNER,
                operation_id=OPERATION,
                message_id=message_id,
                thread_id=THREAD,
                source_created_at=created_at,
                target_sha256=source_erasure_target_sha256(
                    owner_user_id=OWNER,
                    operation_id=OPERATION,
                    message_id=message_id,
                    thread_id=THREAD,
                    source_created_at=created_at,
                ),
            )
        )
    return tuple(
        sorted(
            values,
            key=lambda value: (
                value.source_created_at,
                str(value.message_id),
            ),
        )
    )


def lease(
    selector_kind: DeletionSelectorKind,
    target_rows: tuple[ConversationErasureTarget, ...],
) -> ConversationErasureLease:
    return ConversationErasureLease(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        selector_kind=selector_kind,
        selector_sha256="a" * 64,
        target_count=len(target_rows),
        target_manifest_sha256=erasure_target_manifest_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            targets=target_rows,
        ),
        state=ConversationErasureState.GOVERNED_DELETED,
        governed_receipt_sha256="b" * 64,
        lease_token=LEASE_TOKEN,
    )


class FakeTransport:
    def __init__(self, responses: list[ZepHttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    def request(self, *, method: str, path: str) -> ZepHttpResponse:
        self.requests.append((method, path))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        return self.responses.pop(0)


def response(status: int, value: object | None = None) -> ZepHttpResponse:
    body = b"" if value is None else json.dumps(value).encode("utf-8")
    return ZepHttpResponse(status=status, body=body)


class ZepDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_conversations_deletes_owner_graph_and_threads(self) -> None:
        target_rows = targets()
        transport = FakeTransport([response(204)])
        await ZepConversationDeletionV1(transport=transport).erase(
            lease=lease(DeletionSelectorKind.ALL_CONVERSATIONS, target_rows),
            targets=target_rows,
        )
        self.assertEqual(
            transport.requests,
            [
                (
                    "DELETE",
                    f"{ZEP_API_PREFIX}/users/lifeswitch-user-{OWNER}",
                )
            ],
        )

    async def test_thread_delete_is_idempotent_and_deduplicated(self) -> None:
        target_rows = targets(2)
        transport = FakeTransport([response(404)])
        await ZepConversationDeletionV1(transport=transport).erase(
            lease=lease(DeletionSelectorKind.THREAD, target_rows),
            targets=target_rows,
        )
        self.assertEqual(
            transport.requests,
            [
                (
                    "DELETE",
                    f"{ZEP_API_PREFIX}/threads/lifeswitch-thread-{THREAD}",
                )
            ],
        )

    async def test_partial_delete_resolves_lifeswitch_metadata_to_episode(self) -> None:
        target_rows = targets()
        episode_id = "zep-generated-episode-id"
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "messages": [
                            {
                                "uuid": episode_id,
                                "metadata": {
                                    "lifeswitch_message_id": str(
                                        target_rows[0].message_id
                                    )
                                },
                            }
                        ],
                        "total_count": 1,
                    },
                ),
                response(204),
            ]
        )
        await ZepConversationDeletionV1(transport=transport).erase(
            lease=lease(DeletionSelectorKind.MESSAGE_TAIL, target_rows),
            targets=target_rows,
        )
        self.assertEqual(
            transport.requests,
            [
                (
                    "GET",
                    f"{ZEP_API_PREFIX}/threads/lifeswitch-thread-{THREAD}"
                    "/messages?limit=100&cursor=0",
                ),
                (
                    "DELETE",
                    f"{ZEP_API_PREFIX}/graph/episodes/{episode_id}",
                ),
            ],
        )

    async def test_missing_old_message_mapping_is_already_absent(self) -> None:
        target_rows = targets()
        transport = FakeTransport(
            [response(200, {"messages": [], "total_count": 0})]
        )
        await ZepConversationDeletionV1(transport=transport).erase(
            lease=lease(DeletionSelectorKind.RECENT, target_rows),
            targets=target_rows,
        )
        self.assertEqual(len(transport.requests), 1)

    async def test_provider_outage_is_retryable(self) -> None:
        target_rows = targets()
        for status in (429, 500):
            with self.subTest(status=status):
                transport = FakeTransport([response(status)])
                with self.assertRaisesRegex(
                    ZepDeletionUnavailable,
                    "zep_deletion_unavailable",
                ):
                    await ZepConversationDeletionV1(
                        transport=transport
                    ).erase(
                        lease=lease(
                            DeletionSelectorKind.ALL_CONVERSATIONS,
                            target_rows,
                        ),
                        targets=target_rows,
                    )

    async def test_authorization_failure_is_manual_review_contract_error(self) -> None:
        target_rows = targets()
        transport = FakeTransport([response(401)])
        with self.assertRaisesRegex(
            ContractViolation,
            "zep_deletion_authorization_failed",
        ):
            await ZepConversationDeletionV1(transport=transport).erase(
                lease=lease(
                    DeletionSelectorKind.ALL_CONVERSATIONS,
                    target_rows,
                ),
                targets=target_rows,
            )

    async def test_partial_deletion_is_bounded_and_retries_remaining_work(self) -> None:
        target_rows = targets(ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT + 1)
        messages = [
            {
                "uuid": f"episode-{index}",
                "metadata": {
                    "lifeswitch_message_id": str(target.message_id)
                },
            }
            for index, target in enumerate(target_rows)
        ]
        transport = FakeTransport(
            [
                response(
                    200,
                    {"messages": messages, "total_count": len(messages)},
                ),
                *[
                    response(204)
                    for _ in range(ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT)
                ],
            ]
        )
        with self.assertRaisesRegex(
            ZepDeletionUnavailable,
            "zep_deletion_more_work",
        ):
            await ZepConversationDeletionV1(transport=transport).erase(
                lease=lease(DeletionSelectorKind.RECENT, target_rows),
                targets=target_rows,
            )
        delete_requests = [
            item for item in transport.requests if item[0] == "DELETE"
        ]
        self.assertEqual(
            len(delete_requests),
            ZEP_DELETE_MAX_EPISODES_PER_ATTEMPT,
        )


class ZepDeletionValidationTests(unittest.TestCase):
    def test_identifiers_are_stable_and_transport_rejects_bad_secrets(self) -> None:
        self.assertEqual(zep_user_id(OWNER), f"lifeswitch-user-{OWNER}")
        self.assertEqual(zep_thread_id(THREAD), f"lifeswitch-thread-{THREAD}")
        with self.assertRaisesRegex(ContractViolation, "invalid_zep_api_key"):
            BoundedZepHttpsTransport(api_key=" key-with-spaces ")
        with self.assertRaisesRegex(ContractViolation, "invalid_zep_timeout"):
            BoundedZepHttpsTransport(api_key="valid-key", timeout_seconds=31)


if __name__ == "__main__":
    unittest.main()
