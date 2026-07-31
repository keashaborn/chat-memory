from __future__ import annotations

import asyncio
import os

import asyncpg


EXPECTED = {
    "aaaaaaaa-5000-4000-8000-000000000001": (
        "strength",
        "training_session_role_event",
        False,
    ),
    "aaaaaaaa-5000-4000-8000-000000000002": (
        "strength",
        "workout_role_snapshot",
        False,
    ),
    "aaaaaaaa-5000-4000-8000-000000000003": (
        "rehab",
        "exercise_role_snapshot",
        False,
    ),
    "aaaaaaaa-5000-4000-8000-000000000004": (
        "unknown",
        "conflict",
        True,
    ),
    "aaaaaaaa-5000-4000-8000-000000000005": (
        "strength",
        "capture_role",
        False,
    ),
    "aaaaaaaa-5000-4000-8000-000000000006": (
        "unknown",
        "unresolved",
        False,
    ),
    "aaaaaaaa-5000-4000-8000-000000000007": (
        "rehab",
        "training_session_role_event",
        False,
    ),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def main() -> None:
    conn = await asyncpg.connect(
        os.environ["LIFESWITCH_GATEWAY_TEST_DSN"], command_timeout=20
    )
    checks: list[str] = []
    try:
        require(await conn.fetchval("select session_user") == "brains_app", "wrong role")
        rows = await conn.fetch(
            """
            select training_set_log_id::text,effective_role,resolution_source,
                   role_conflict,capture_role,exercise_role_snapshot
            from lifeswitch_training.training_set_effective_role_v1
            where owner_user_id='11111111-1111-4111-8111-111111111111'::uuid
            order by training_set_log_id
            """
        )
        observed = {
            row["training_set_log_id"]: (
                row["effective_role"],
                row["resolution_source"],
                row["role_conflict"],
            )
            for row in rows
        }
        for set_id, expected in EXPECTED.items():
            require(observed.get(set_id) == expected, f"wrong resolution for {set_id}")
        checks.append("precedence_and_conflict_matrix")

        owner_counts = await conn.fetch(
            """
            select owner_user_id::text,count(*)::integer as set_count
            from lifeswitch_training.training_set_effective_role_v1
            group by owner_user_id
            order by owner_user_id
            """
        )
        require([row["set_count"] for row in owner_counts] == [7, 1], "owner join leak")
        checks.append("owner_paired_projection")

        raw_conflict = next(
            row for row in rows
            if row["training_set_log_id"]
            == "aaaaaaaa-5000-4000-8000-000000000004"
        )
        require(raw_conflict["capture_role"] == "strength", "raw capture was rewritten")
        require(
            raw_conflict["exercise_role_snapshot"] == "rehab",
            "raw snapshot was rewritten",
        )
        checks.append("raw_roles_immutable")

        async with conn.transaction():
            await conn.execute("set local role lifeswitch_chat_reader_v1")
            try:
                await conn.fetchval(
                    "select count(*) from "
                    "lifeswitch_training.training_set_effective_role_v1"
                )
            except asyncpg.InsufficientPrivilegeError:
                pass
            else:
                raise AssertionError("chat reader directly selected role projection")
        checks.append("reader_direct_access_denied")

        print(
            "TRAINING_EFFECTIVE_ROLE_CLONE="
            + ",".join(checks)
        )
    finally:
        await conn.close()


asyncio.run(main())
