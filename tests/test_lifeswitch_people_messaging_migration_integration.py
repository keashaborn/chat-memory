from __future__ import annotations

import os
import unittest
from pathlib import Path

import asyncpg


RUN_FLAG = "LIFESWITCH_PEOPLE_MIGRATION_INTEGRATION"


def _migration_insert_statement() -> str:
    migration = (
        Path(__file__).resolve().parents[1]
        / "ops/sql/20260720_lifeswitch_people_messaging_authorization.sql"
    ).read_text()
    lowered = migration.lower()
    start = lowered.index("insert into lifeswitch_people.relationship_permission")
    end = lowered.index("\ncommit;", start)
    statement = migration[start:end].strip()
    if not statement.endswith(";"):
        raise AssertionError("messaging migration INSERT is not terminated")
    return statement


class PeopleMessagingMigrationIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if os.getenv(RUN_FLAG, "").strip() != "1":
            self.skipTest(f"{RUN_FLAG}=1 is required")
        self.dsn = os.getenv("POSTGRES_DSN", "").strip()
        if not self.dsn:
            self.skipTest("POSTGRES_DSN is not configured")

    async def test_migration_inserts_missing_grant_preserves_explicit_denial_and_rolls_back(
        self,
    ) -> None:
        migration_insert = _migration_insert_statement()
        conn = await asyncpg.connect(self.dsn, command_timeout=10)
        relationship_id = None
        baseline_count = None
        baseline_rows = None
        try:
            relationship = await conn.fetchrow(
                """
                select relationship_id, requester_user_id, addressee_user_id
                from lifeswitch_people.relationship
                where status='accepted'
                order by relationship_id
                limit 1
                """
            )
            if not relationship:
                self.skipTest("no accepted LifeSwitch relationship is available")

            relationship_id = relationship["relationship_id"]
            requester = relationship["requester_user_id"]
            addressee = relationship["addressee_user_id"]
            baseline_count = await conn.fetchval(
                "select count(*) from lifeswitch_people.relationship_permission"
            )
            baseline_rows = await conn.fetch(
                """
                select grantor_user_id, grantee_user_id, permission_scope,
                       permission_level, is_enabled, notes
                from lifeswitch_people.relationship_permission
                where relationship_id=$1
                order by grantor_user_id, grantee_user_id, permission_scope
                """,
                relationship_id,
            )

            transaction = conn.transaction()
            await transaction.start()
            try:
                await conn.execute("set local lock_timeout='3s'")
                await conn.execute("set local statement_timeout='10s'")
                await conn.fetchrow(
                    """
                    select relationship_id
                    from lifeswitch_people.relationship
                    where relationship_id=$1 and status='accepted'
                    for update
                    """,
                    relationship_id,
                )

                # One direction is deliberately missing so the migration must insert it.
                await conn.execute(
                    """
                    delete from lifeswitch_people.relationship_permission
                    where relationship_id=$1
                      and grantor_user_id=$2
                      and grantee_user_id=$3
                      and permission_scope='messages:send'
                    """,
                    relationship_id,
                    requester,
                    addressee,
                )

                # The reverse direction is an explicit denial and must not be changed.
                await conn.execute(
                    """
                    insert into lifeswitch_people.relationship_permission (
                      relationship_id, grantor_user_id, grantee_user_id,
                      permission_scope, permission_level, is_enabled, notes
                    )
                    values ($1, $2, $3, 'messages:send', 'none', false,
                            'integration probe explicit denial')
                    on conflict (
                      relationship_id, grantor_user_id, grantee_user_id,
                      permission_scope
                    )
                    do update set
                      permission_level='none',
                      is_enabled=false,
                      notes='integration probe explicit denial'
                    """,
                    relationship_id,
                    addressee,
                    requester,
                )

                await conn.execute(migration_insert)
                inserted = await conn.fetchrow(
                    """
                    select permission_level, is_enabled
                    from lifeswitch_people.relationship_permission
                    where relationship_id=$1
                      and grantor_user_id=$2
                      and grantee_user_id=$3
                      and permission_scope='messages:send'
                    """,
                    relationship_id,
                    requester,
                    addressee,
                )
                denied = await conn.fetchrow(
                    """
                    select permission_level, is_enabled, notes
                    from lifeswitch_people.relationship_permission
                    where relationship_id=$1
                      and grantor_user_id=$2
                      and grantee_user_id=$3
                      and permission_scope='messages:send'
                    """,
                    relationship_id,
                    addressee,
                    requester,
                )
                self.assertEqual(inserted["permission_level"], "comment")
                self.assertIs(inserted["is_enabled"], True)
                self.assertEqual(denied["permission_level"], "none")
                self.assertIs(denied["is_enabled"], False)
                self.assertEqual(denied["notes"], "integration probe explicit denial")

                # A second exact execution proves the ON CONFLICT target is compatible
                # with the live directional unique index and remains idempotent.
                await conn.execute(migration_insert)
                directional_count = await conn.fetchval(
                    """
                    select count(*)
                    from lifeswitch_people.relationship_permission
                    where relationship_id=$1
                      and permission_scope='messages:send'
                      and (
                        (grantor_user_id=$2 and grantee_user_id=$3)
                        or
                        (grantor_user_id=$3 and grantee_user_id=$2)
                      )
                    """,
                    relationship_id,
                    requester,
                    addressee,
                )
                self.assertEqual(directional_count, 2)
            finally:
                await transaction.rollback()
        finally:
            await conn.close()

        verification = await asyncpg.connect(self.dsn, command_timeout=10)
        try:
            self.assertEqual(
                await verification.fetchval(
                    "select count(*) from lifeswitch_people.relationship_permission"
                ),
                baseline_count,
            )
            self.assertEqual(
                await verification.fetch(
                    """
                    select grantor_user_id, grantee_user_id, permission_scope,
                           permission_level, is_enabled, notes
                    from lifeswitch_people.relationship_permission
                    where relationship_id=$1
                    order by grantor_user_id, grantee_user_id, permission_scope
                    """,
                    relationship_id,
                ),
                baseline_rows,
            )
        finally:
            await verification.close()


if __name__ == "__main__":
    unittest.main()
