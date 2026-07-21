from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("POSTGRES_DSN", "postgresql://unused")

from fastapi import HTTPException

from rag_engine import lifeswitch_people_router as people


OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
RELATIONSHIP = "33333333-3333-3333-3333-333333333333"
CONVERSATION = "44444444-4444-4444-4444-444444444444"
INVITATION = "55555555-5555-5555-5555-555555555555"


class FakeTransaction:
    def __init__(self):
        self.exit_exception_type = "not-exited"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exit_exception_type = exc_type
        return False


class FakeConnection:
    def __init__(self, *, fetchrow_results=None, fetch_results=None):
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])
        self.fetchrow_calls = []
        self.fetch_calls = []
        self.execute_calls = []
        self.closed = False
        self.tx = FakeTransaction()

    def transaction(self):
        return self.tx

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if not self.fetchrow_results:
            raise AssertionError(f"unexpected fetchrow: {query}")
        return self.fetchrow_results.pop(0)

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if not self.fetch_results:
            raise AssertionError(f"unexpected fetch: {query}")
        return self.fetch_results.pop(0)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"

    async def close(self):
        self.closed = True


def actor(_request, owner_user_id):
    return owner_user_id


class PeopleAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_authorization_is_recipient_to_sender_and_fail_closed(self):
        conn = FakeConnection(fetchrow_results=[None])
        with self.assertRaises(HTTPException) as raised:
            await people._assert_message_send_authorized(conn, OWNER, OTHER)
        self.assertEqual(raised.exception.status_code, 403)
        query, args = conn.fetchrow_calls[0]
        self.assertIn("rp.grantor_user_id=$2::uuid", query)
        self.assertIn("rp.grantee_user_id=$1::uuid", query)
        self.assertIn("rp.permission_scope='messages:send'", query)
        self.assertIn("r.status='accepted'", query)
        self.assertEqual(args, (OWNER, OTHER))

    async def test_relationship_upsert_cannot_mutate_status(self):
        with patch.object(people, "require_actor_matches_owner", actor):
            with self.assertRaises(HTTPException) as raised:
                await people.upsert_relationship(
                    object(), OWNER, OTHER, "blocked", "friend", "", ""
                )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_relationship_upsert_only_updates_existing_accepted_pair(self):
        conn = FakeConnection(fetchrow_results=[None])
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.upsert_relationship(
                    object(), OWNER, OTHER, "accepted", "coach", "Coach", "Notes"
                )
        self.assertEqual(raised.exception.status_code, 409)
        query = conn.fetchrow_calls[0][0].lower()
        self.assertIn("update lifeswitch_people.relationship", query)
        self.assertNotIn("insert into lifeswitch_people.relationship", query)
        self.assertIn("where status='accepted'", query)

    async def test_permission_write_requires_accepted_relationship(self):
        conn = FakeConnection(
            fetchrow_results=[
                {
                    "relationship_id": RELATIONSHIP,
                    "other_user_id": OTHER,
                    "status": "revoked",
                }
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.upsert_relationship_permission(
                    RELATIONSHIP,
                    object(),
                    OWNER,
                    "plan:view",
                    "view",
                    1,
                    {},
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(conn.fetchrow_calls), 1)

    async def test_direct_conversation_requires_permission_before_mutation(self):
        conn = FakeConnection(fetchrow_results=[None])
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.get_or_create_direct_conversation(object(), OWNER, OTHER)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(conn.fetchrow_calls), 1)
        self.assertEqual(conn.execute_calls, [])

    async def test_direct_conversation_response_exposes_can_send(self):
        conn = FakeConnection(
            fetchrow_results=[
                {"relationship_id": RELATIONSHIP},
                {
                    "conversation_id": CONVERSATION,
                    "conversation_kind": "direct",
                    "created_by_user_id": OWNER,
                    "direct_user_low_id": OWNER,
                    "direct_user_high_id": OTHER,
                    "title": "",
                    "is_active": True,
                },
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            response = await people.get_or_create_direct_conversation(
                object(), OWNER, OTHER
            )
        payload = json.loads(response.body)
        self.assertIs(payload["can_send"], True)
        self.assertIn("relationship_permission", conn.fetchrow_calls[0][0])
        self.assertIn("insert into lifeswitch_people.conversation", conn.fetchrow_calls[1][0])

    async def test_conversation_list_exposes_server_computed_can_send(self):
        conn = FakeConnection(
            fetch_results=[
                [
                    {
                        "conversation_id": CONVERSATION,
                        "conversation_kind": "direct",
                        "can_send": False,
                    }
                ]
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            response = await people.list_conversations(object(), OWNER, 50)
        payload = json.loads(response.body)
        self.assertIs(payload[0]["can_send"], False)
        query = conn.fetch_calls[0][0]
        self.assertIn("end as can_send", query)
        self.assertIn("send_recipient.is_active=true", query)
        self.assertIn("send_permission.permission_scope='messages:send'", query)
        self.assertIn("send_relationship.status='accepted'", query)

    async def test_disconnected_conversation_cannot_send_but_history_remains_readable(self):
        send_conn = FakeConnection(
            fetchrow_results=[{"conversation_member_id": "member"}, None],
            fetch_results=[[{"user_id": OTHER}]],
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=send_conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.create_message(
                    CONVERSATION, object(), OWNER, "hello", "plain", {}
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(
            any("insert into lifeswitch_people.message" in q for q, _ in send_conn.fetchrow_calls)
        )

        history_conn = FakeConnection(
            fetchrow_results=[{"conversation_member_id": "member"}],
            fetch_results=[[]],
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=history_conn)),
        ):
            response = await people.list_messages(
                CONVERSATION, object(), OWNER, 100
            )
        self.assertEqual(json.loads(response.body), [])
        self.assertFalse(
            any("relationship_permission" in q for q, _ in history_conn.fetchrow_calls)
        )

    async def test_expired_invitation_status_commits_before_410(self):
        now = dt.datetime.now(dt.timezone.utc)
        conn = FakeConnection(
            fetchrow_results=[
                {
                    "invitation_id": INVITATION,
                    "created_by_user_id": OTHER,
                    "accepted_by_user_id": None,
                    "relationship_kind": "friend",
                    "label": "",
                    "notes": "",
                    "status": "pending",
                    "expires_at": now - dt.timedelta(minutes=1),
                    "created_at": now - dt.timedelta(days=31),
                }
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.accept_invitation(object(), OWNER, "valid-test-token")
        self.assertEqual(raised.exception.status_code, 410)
        self.assertIsNone(conn.tx.exit_exception_type)
        self.assertTrue(any("set status='expired'" in q for q, _ in conn.execute_calls))

    async def test_blocked_relationship_cannot_be_overwritten_by_invite(self):
        now = dt.datetime.now(dt.timezone.utc)
        conn = FakeConnection(
            fetchrow_results=[
                {
                    "invitation_id": INVITATION,
                    "created_by_user_id": OTHER,
                    "accepted_by_user_id": None,
                    "relationship_kind": "friend",
                    "label": "",
                    "notes": "",
                    "status": "pending",
                    "expires_at": now + dt.timedelta(days=1),
                    "created_at": now,
                },
                {"relationship_id": RELATIONSHIP, "status": "blocked", "updated_at": now},
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.accept_invitation(object(), OWNER, "valid-test-token")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(conn.fetchrow_calls), 2)

    async def test_invite_created_before_revoke_cannot_reconnect(self):
        now = dt.datetime.now(dt.timezone.utc)
        conn = FakeConnection(
            fetchrow_results=[
                {
                    "invitation_id": INVITATION,
                    "created_by_user_id": OTHER,
                    "accepted_by_user_id": None,
                    "relationship_kind": "friend",
                    "label": "",
                    "notes": "",
                    "status": "pending",
                    "expires_at": now + dt.timedelta(days=1),
                    "created_at": now - dt.timedelta(hours=2),
                },
                {
                    "relationship_id": RELATIONSHIP,
                    "status": "revoked",
                    "updated_at": now - dt.timedelta(hours=1),
                },
            ]
        )
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await people.accept_invitation(object(), OWNER, "valid-test-token")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(conn.fetchrow_calls), 2)

    async def test_profile_lookup_is_scoped_and_hides_historical_only_email(self):
        conn = FakeConnection(fetch_results=[[]])
        with (
            patch.object(people, "require_actor_matches_owner", actor),
            patch.object(people, "_db", AsyncMock(return_value=conn)),
        ):
            await people.list_profiles(object(), OWNER, OTHER)
        query = conn.fetch_calls[0][0]
        self.assertIn("conversation_member mine", query)
        self.assertIn("accepted_relationship.status='accepted'", query)
        self.assertIn("else null", query)
        self.assertIn("p.user_id=any($2::uuid[])", query)

    async def test_fresh_accept_inserts_only_missing_bilateral_messaging_grants(self):
        conn = FakeConnection()
        await people._grant_default_messaging_permissions(
            conn, RELATIONSHIP, OWNER, OTHER
        )
        query, args = conn.execute_calls[0]
        self.assertEqual(query.count("'messages:send'"), 2)
        self.assertIn("do nothing", query.lower())
        self.assertNotIn("do update set", query.lower())
        self.assertNotIn("set is_enabled", query.lower())
        self.assertNotIn("set permission_level", query.lower())
        self.assertEqual(args, (RELATIONSHIP, OWNER, OTHER))

    def test_migration_backfills_both_directions_without_touching_history(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "ops/sql/20260720_lifeswitch_people_messaging_authorization.sql"
        ).read_text()
        self.assertIn("where accepted_relationship.status='accepted'", migration)
        self.assertIn("accepted_relationship.requester_user_id", migration)
        self.assertIn("accepted_relationship.addressee_user_id", migration)
        self.assertIn("on conflict", migration.lower())
        self.assertIn("do nothing", migration.lower())
        self.assertNotIn("do update set", migration.lower())
        self.assertNotIn("set is_enabled", migration.lower())
        self.assertNotIn("set permission_level", migration.lower())
        self.assertNotIn("delete from lifeswitch_people.message", migration.lower())
        self.assertNotIn("update lifeswitch_people.conversation", migration.lower())


if __name__ == "__main__":
    unittest.main()
