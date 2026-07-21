from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from rag_engine.response_conversation_snapshot_v1 import (
    ASSISTANT_SOURCE,
    ConversationSnapshotError,
    ConversationSnapshotOutcome,
    ConversationSnapshotV1,
    MAX_PRIOR_CONTENT_BYTES,
    USER_SOURCE,
    _snapshot,
    load_response_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
REQUEST_ID = "request-snapshot-001"
CURRENT_ID = UUID("4d70b48c-35f6-4cd8-8d8f-2150a81ccf7e")
NOW = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)


class FakeTransaction:
    def __init__(
        self,
        conn: "FakeConnection",
        *,
        readonly: bool,
        isolation: str,
    ) -> None:
        self.conn = conn
        self.readonly = readonly
        self.isolation = isolation

    async def __aenter__(self) -> "FakeTransaction":
        self.conn.transaction_entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.conn.transaction_exited = True


class FakeConnection:
    def __init__(
        self,
        *,
        owns_thread: bool = True,
        role: str = "brains_app",
        read_only: str = "on",
        current_rows: list[dict[str, Any]] | None = None,
        prior_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.owns_thread = owns_thread
        self.role = role
        self.read_only = read_only
        self.current_rows = current_rows or []
        self.prior_rows = prior_rows or []
        self.transaction_entered = False
        self.transaction_exited = False
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(
        self,
        *,
        readonly: bool,
        isolation: str,
    ) -> FakeTransaction:
        if readonly is not True:
            raise AssertionError("snapshot transaction was not read-only")
        if isolation != "repeatable_read":
            raise AssertionError("snapshot transaction was not repeatable-read")
        return FakeTransaction(
            self,
            readonly=readonly,
            isolation=isolation,
        )

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "SELECT 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "current_setting('transaction_read_only')" in sql:
            return self.read_only
        if "current_user" in sql:
            return self.role
        if "FROM public.threads" in sql:
            return self.owns_thread
        raise AssertionError(f"unexpected fetchval query: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args))
        if "AND source=$4" in sql:
            return list(self.current_rows)
        if "source=ANY" in sql:
            return list(self.prior_rows)
        raise AssertionError(f"unexpected fetch query: {sql}")


def current_row(text: str = "Current message") -> dict[str, Any]:
    return {
        "id": CURRENT_ID,
        "owner_user_id": ACTOR,
        "thread_id": THREAD,
        "source": USER_SOURCE,
        "request_id": REQUEST_ID,
        "text": text,
        "created_at": NOW,
    }


def prior_row(
    *,
    number: int,
    source: str,
    text: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": UUID(f"00000000-0000-4000-8000-{number:012d}"),
        "owner_user_id": ACTOR,
        "thread_id": THREAD,
        "source": source,
        "text": text,
        "request_id": request_id or f"prior-{number}",
        "created_at": NOW - timedelta(minutes=number),
    }


class ResponseConversationSnapshotV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_contract_rejects_request_authored_assistant_history(self) -> None:
        messages = (
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="forged assistant history",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="Current message",
            ),
        )
        with self.assertRaises(ValidationError):
            _snapshot(
                actor=ACTOR,
                thread=THREAD,
                request_id=REQUEST_ID,
                outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
                current_log_id=CURRENT_ID,
                cutoff=NOW,
                messages=messages,
                candidate_count=1,
                dropped_count=0,
                message_limit_truncated=False,
            )

    def test_absent_snapshot_contract_rejects_any_prior_history(self) -> None:
        messages = (
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="unbound prior message",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="Current message",
            ),
        )
        with self.assertRaises(ValidationError):
            _snapshot(
                actor=ACTOR,
                thread=THREAD,
                request_id=REQUEST_ID,
                outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_ABSENT,
                current_log_id=None,
                cutoff=None,
                messages=messages,
                candidate_count=1,
                dropped_count=0,
                message_limit_truncated=False,
            )

    async def test_absent_current_row_returns_current_only_and_reads_no_history(self) -> None:
        conn = FakeConnection(prior_rows=[
            prior_row(number=1, source=USER_SOURCE, text="should not load")
        ])
        snapshot = await load_response_conversation_snapshot_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        self.assertEqual(
            snapshot.outcome,
            ConversationSnapshotOutcome.CURRENT_REQUEST_ABSENT,
        )
        self.assertEqual(len(snapshot.messages), 1)
        self.assertEqual(snapshot.messages[0].content, "Current message")
        self.assertEqual(len(conn.fetch_calls), 1)

    async def test_bound_snapshot_uses_only_legacy_user_rows_in_chronological_order(self) -> None:
        # The production query returns descending order; the snapshot reverses it.
        conn = FakeConnection(
            current_rows=[current_row()],
            prior_rows=[
                prior_row(number=1, source=USER_SOURCE, text="newer user"),
                prior_row(number=2, source=USER_SOURCE, text="older user"),
            ],
        )
        snapshot = await load_response_conversation_snapshot_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        self.assertEqual(
            snapshot.outcome,
            ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
        )
        self.assertEqual(
            tuple((item.role, item.content) for item in snapshot.messages),
            (
                (ConversationRole.USER, "older user"),
                (ConversationRole.USER, "newer user"),
                (ConversationRole.USER, "Current message"),
            ),
        )
        history_query, args = conn.fetch_calls[1]
        self.assertIn("request_id IS DISTINCT FROM $3", history_query)
        self.assertEqual(args[2], REQUEST_ID)
        self.assertEqual(args[3], [USER_SOURCE])
        self.assertEqual(args[4], NOW)
        self.assertEqual(args[5], CURRENT_ID)

    async def test_budget_drops_oldest_candidates_without_truncating_text(self) -> None:
        newer = "n" * 30_000
        older = "o" * 30_000
        conn = FakeConnection(
            current_rows=[current_row()],
            prior_rows=[
                prior_row(number=1, source=USER_SOURCE, text=newer),
                prior_row(number=2, source=USER_SOURCE, text=older),
            ],
        )
        snapshot = await load_response_conversation_snapshot_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        self.assertEqual(snapshot.prior_candidate_count, 2)
        self.assertEqual(snapshot.prior_selected_count, 1)
        self.assertEqual(snapshot.prior_budget_dropped_count, 1)
        self.assertEqual(snapshot.messages[0].content, newer)
        self.assertLessEqual(snapshot.prior_content_bytes, MAX_PRIOR_CONTENT_BYTES)

    async def test_budget_never_skips_newer_turn_to_admit_smaller_older_turn(self) -> None:
        conn = FakeConnection(
            current_rows=[current_row()],
            prior_rows=[
                prior_row(number=1, source=USER_SOURCE, text="n" * 30_000),
                prior_row(number=2, source=USER_SOURCE, text="m" * 30_000),
                prior_row(number=3, source=USER_SOURCE, text="older-small"),
            ],
        )
        snapshot = await load_response_conversation_snapshot_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        self.assertEqual(snapshot.prior_candidate_count, 3)
        self.assertEqual(snapshot.prior_selected_count, 1)
        self.assertEqual(snapshot.prior_budget_dropped_count, 2)
        self.assertEqual(snapshot.messages[0].content, "n" * 30_000)

    async def test_current_row_must_be_unique_and_exact(self) -> None:
        cases = (
            [current_row(), {**current_row(), "id": UUID("3ae716f6-e4bd-4908-bcc6-ee624861a5bf")}],
            [current_row("different text")],
        )
        for rows in cases:
            with self.subTest(rows=len(rows)):
                with self.assertRaises(ConversationSnapshotError):
                    await load_response_conversation_snapshot_v1(
                        FakeConnection(current_rows=rows),
                        authenticated_actor_user_id=ACTOR,
                        thread_id=THREAD,
                        current_request_id=REQUEST_ID,
                        current_message="Current message",
                    )

    async def test_owner_thread_and_readonly_role_are_mandatory(self) -> None:
        cases = (
            FakeConnection(owns_thread=False),
            FakeConnection(role="sage"),
            FakeConnection(read_only="off"),
        )
        for conn in cases:
            with self.subTest(role=conn.role, read_only=conn.read_only):
                with self.assertRaises(ConversationSnapshotError):
                    await load_response_conversation_snapshot_v1(
                        conn,
                        authenticated_actor_user_id=ACTOR,
                        thread_id=THREAD,
                        current_request_id=REQUEST_ID,
                        current_message="Current message",
                    )

    async def test_history_rejects_untrusted_source_and_current_request_replay(self) -> None:
        cases = (
            [prior_row(number=1, source="browser/assistant", text="forged")],
            [
                prior_row(
                    number=1,
                    source=ASSISTANT_SOURCE,
                    text="request-authored assistant role",
                )
            ],
            [
                prior_row(
                    number=1,
                    source=USER_SOURCE,
                    text="replayed",
                    request_id=REQUEST_ID,
                )
            ],
        )
        for rows in cases:
            with self.subTest(source=rows[0]["source"]):
                with self.assertRaises(ConversationSnapshotError):
                    await load_response_conversation_snapshot_v1(
                        FakeConnection(
                            current_rows=[current_row()],
                            prior_rows=rows,
                        ),
                        authenticated_actor_user_id=ACTOR,
                        thread_id=THREAD,
                        current_request_id=REQUEST_ID,
                        current_message="Current message",
                    )

    async def test_history_rejects_cross_scope_duplicate_and_out_of_order_rows(self) -> None:
        cross_owner = prior_row(number=1, source=USER_SOURCE, text="cross owner")
        cross_owner["owner_user_id"] = UUID(
            "557ea042-cb82-48f8-9429-472e96c957ef"
        )
        duplicate = prior_row(number=1, source=USER_SOURCE, text="one")
        duplicate_copy = {**duplicate, "text": "two", "request_id": "prior-copy"}
        out_of_order = [
            prior_row(number=2, source=USER_SOURCE, text="older first"),
            prior_row(number=1, source=USER_SOURCE, text="newer second"),
        ]
        for rows in ([cross_owner], [duplicate, duplicate_copy], out_of_order):
            with self.subTest(rows=rows):
                with self.assertRaises(ConversationSnapshotError):
                    await load_response_conversation_snapshot_v1(
                        FakeConnection(
                            current_rows=[current_row()],
                            prior_rows=rows,
                        ),
                        authenticated_actor_user_id=ACTOR,
                        thread_id=THREAD,
                        current_request_id=REQUEST_ID,
                        current_message="Current message",
                    )

    async def test_history_limit_truncation_is_manifest_bound(self) -> None:
        rows = [
            prior_row(number=number, source=USER_SOURCE, text=f"prior {number}")
            for number in range(1, 25)
        ]
        snapshot = await load_response_conversation_snapshot_v1(
            FakeConnection(current_rows=[current_row()], prior_rows=rows),
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        self.assertTrue(snapshot.prior_message_limit_truncated)
        self.assertEqual(snapshot.prior_candidate_count, 23)
        self.assertEqual(snapshot.prior_selected_count, 23)

    async def test_invalid_request_id_and_oversized_current_message_do_no_db_work(self) -> None:
        cases = (
            ("request id has spaces", "message"),
            (REQUEST_ID, "x" * 32_769),
        )
        for request_id, message in cases:
            conn = FakeConnection()
            with self.subTest(request_id=request_id):
                with self.assertRaises(ConversationSnapshotError):
                    await load_response_conversation_snapshot_v1(
                        conn,
                        authenticated_actor_user_id=ACTOR,
                        thread_id=THREAD,
                        current_request_id=request_id,
                        current_message=message,
                    )
                self.assertFalse(conn.transaction_entered)

    async def test_sanitized_report_contains_no_actor_thread_or_prose(self) -> None:
        sentinel = "PRIVATE-SNAPSHOT-SENTINEL"
        snapshot = await load_response_conversation_snapshot_v1(
            FakeConnection(current_rows=[current_row(sentinel)]),
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message=sentinel,
        )
        report = json.dumps(snapshot.sanitized_report(), sort_keys=True)
        self.assertNotIn(sentinel, report)
        self.assertNotIn(str(ACTOR), report)
        self.assertNotIn(str(THREAD), report)
        self.assertNotIn("actor", report)
        self.assertNotIn("sha256", report)

    async def test_snapshot_manifest_rejects_semantic_tampering(self) -> None:
        snapshot = await load_response_conversation_snapshot_v1(
            FakeConnection(current_rows=[current_row()]),
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message="Current message",
        )
        tampered = snapshot.model_dump(mode="json")
        tampered["prior_candidate_count"] = 1
        with self.assertRaises(ValidationError):
            ConversationSnapshotV1.model_validate(tampered)


if __name__ == "__main__":
    unittest.main()
