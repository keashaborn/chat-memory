from __future__ import annotations

import os
import unittest
import uuid

import asyncpg
from fastapi import HTTPException, Request

from lifeswitch_agentic.app_adapter import LifeSwitchPlanAppAdapter


def request(
    *,
    actor_user_id: str | None,
    target_user_id: str | None = None,
    owner_timezone: str | None = None,
) -> Request:
    query = (
        f"target_user_id={target_user_id}".encode("ascii") if target_user_id else b""
    )
    headers: list[tuple[bytes, bytes]] = []
    if actor_user_id is not None:
        headers.append((b"x-vs-actor-user-id", actor_user_id.encode("ascii")))
    if owner_timezone is not None:
        headers.append((b"x-vs-owner-timezone", owner_timezone.encode("ascii")))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/lifeswitch/plan/active",
            "raw_path": b"/lifeswitch/plan/active",
            "query_string": query,
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


class LifeSwitchPlanAppAdapterIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dsn = os.getenv("LIFESWITCH_AGENTIC_TEST_DSN", "").strip()
        if not self.dsn:
            self.skipTest("LIFESWITCH_AGENTIC_TEST_DSN is not configured")
        self.adapter = LifeSwitchPlanAppAdapter(dsn=self.dsn)
        self.conn = await asyncpg.connect(self.dsn)
        await self.conn.execute("create schema if not exists lifeswitch_people")
        await self.conn.execute(
            """
            create table if not exists lifeswitch_people.relationship (
              relationship_id uuid primary key,
              requester_user_id uuid not null,
              addressee_user_id uuid not null,
              status text not null
            )
            """
        )
        await self.conn.execute(
            """
            create table if not exists lifeswitch_people.relationship_permission (
              relationship_permission_id uuid primary key,
              relationship_id uuid not null references lifeswitch_people.relationship,
              grantor_user_id uuid not null,
              grantee_user_id uuid not null,
              permission_scope text not null,
              permission_level text not null,
              is_enabled boolean not null
            )
            """
        )

    async def asyncTearDown(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            await conn.close()

    async def test_owner_context_comes_only_from_trusted_actor_header(self) -> None:
        owner = uuid.uuid4()
        context = await self.adapter.actor_context(
            request(
                actor_user_id=str(owner),
                owner_timezone="America/Chicago",
            )
        )
        self.assertEqual(context.actor_user_id, owner)
        self.assertEqual(context.owner_user_id, owner)
        self.assertTrue(context.is_owner)
        self.assertEqual(context.owner_timezone, "America/Chicago")

    async def test_missing_or_invalid_actor_fails_closed(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            await self.adapter.actor_context(request(actor_user_id=None))
        self.assertEqual(missing.exception.status_code, 401)

        with self.assertRaises(HTTPException) as invalid:
            await self.adapter.actor_context(request(actor_user_id="not-a-uuid"))
        self.assertEqual(invalid.exception.status_code, 400)

    async def test_invalid_owner_timezone_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.adapter.actor_context(
                request(
                    actor_user_id=str(uuid.uuid4()),
                    owner_timezone="not/a/real/timezone",
                )
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "invalid_owner_timezone")

    async def test_accepted_directional_edit_permission_implies_view(self) -> None:
        owner = uuid.uuid4()
        coach = uuid.uuid4()
        relationship_id = uuid.uuid4()
        await self.conn.execute(
            """
            insert into lifeswitch_people.relationship (
              relationship_id, requester_user_id, addressee_user_id, status
            ) values ($1, $2, $3, 'accepted')
            """,
            relationship_id,
            owner,
            coach,
        )
        await self.conn.execute(
            """
            insert into lifeswitch_people.relationship_permission (
              relationship_permission_id, relationship_id,
              grantor_user_id, grantee_user_id,
              permission_scope, permission_level, is_enabled
            ) values ($1, $2, $3, $4, 'plan:edit', 'edit', true)
            """,
            uuid.uuid4(),
            relationship_id,
            owner,
            coach,
        )

        context = await self.adapter.actor_context(
            request(actor_user_id=str(coach), target_user_id=str(owner))
        )
        self.assertFalse(context.is_owner)
        self.assertEqual(context.owner_user_id, owner)
        self.assertTrue(context.permits("plan:edit"))
        self.assertTrue(context.permits("plan:view"))
        self.assertTrue(context.permits("plan:comment"))
        self.assertEqual(context.owner_timezone, "UTC")

    async def test_disabled_or_unaccepted_permission_grants_nothing(self) -> None:
        owner = uuid.uuid4()
        coach = uuid.uuid4()
        relationship_id = uuid.uuid4()
        await self.conn.execute(
            """
            insert into lifeswitch_people.relationship (
              relationship_id, requester_user_id, addressee_user_id, status
            ) values ($1, $2, $3, 'pending')
            """,
            relationship_id,
            owner,
            coach,
        )
        await self.conn.execute(
            """
            insert into lifeswitch_people.relationship_permission (
              relationship_permission_id, relationship_id,
              grantor_user_id, grantee_user_id,
              permission_scope, permission_level, is_enabled
            ) values ($1, $2, $3, $4, 'plan:view', 'view', true)
            """,
            uuid.uuid4(),
            relationship_id,
            owner,
            coach,
        )
        context = await self.adapter.actor_context(
            request(actor_user_id=str(coach), target_user_id=str(owner))
        )
        self.assertFalse(context.permits("plan:view"))


if __name__ == "__main__":
    unittest.main()
