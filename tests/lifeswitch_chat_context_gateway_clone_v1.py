from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import asyncpg


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
THREAD = UUID("33333333-3333-4333-8333-333333333333")
HASH_A = "a" * 64
HASH_B = "b" * 64
TEST_DAY = dt.date(2026, 7, 29)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@asynccontextmanager
async def reader_transaction(conn: asyncpg.Connection):
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("set local role lifeswitch_chat_reader_v1")
        yield


async def create_context(conn: asyncpg.Connection, owner: UUID) -> UUID:
    async with conn.transaction():
        await conn.execute("select set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "select set_config('app.lifeswitch_owner_id',$1,true)", str(owner)
        )
        value = await conn.fetchval(
            "select lifeswitch_chat.begin_owner_read_context_v1($1,$2,$3,$4)",
            owner,
            THREAD,
            HASH_A,
            HASH_B,
        )
    require(isinstance(value, UUID), "context binder did not return UUID")
    return value


async def end_context(conn: asyncpg.Connection, context_id: UUID) -> None:
    async with conn.transaction():
        removed = await conn.fetchval(
            "select lifeswitch_chat.end_owner_read_context_v1($1)", context_id
        )
    require(removed is True, "context cleanup failed")


async def expect_denied(awaitable, label: str) -> None:
    try:
        await awaitable
    except (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError):
        return
    raise AssertionError(f"unauthorized access unexpectedly succeeded: {label}")


async def main() -> None:
    dsn = os.environ["LIFESWITCH_GATEWAY_TEST_DSN"]
    conn = await asyncpg.connect(dsn, command_timeout=20)
    second = await asyncpg.connect(dsn, command_timeout=20)
    context_a: UUID | None = None
    context_b: UUID | None = None
    checks: list[str] = []
    try:
        require(await conn.fetchval("select session_user") == "brains_app", "wrong test role")
        async with conn.transaction():
            await conn.execute("select set_config('app.user_id',$1,true)", str(OWNER_A))
            await conn.execute(
                "select set_config('app.lifeswitch_owner_id',$1,true)", str(OWNER_A)
            )
            await expect_denied(
                conn.fetchval(
                    "select lifeswitch_chat.begin_owner_read_context_v1($1,$2,$3,$4)",
                    OWNER_B,
                    THREAD,
                    HASH_A,
                    HASH_B,
                ),
                "binder owner mismatch",
            )
        checks.append("binder_owner_mismatch_denied")
        context_a = await create_context(conn, OWNER_A)

        async with reader_transaction(conn):
            await conn.execute(
                "select set_config('app.user_id',$1,true)", str(OWNER_B)
            )
            await conn.execute(
                "select set_config('app.lifeswitch_owner_id',$1,true)", str(OWNER_B)
            )
            plan = await conn.fetchrow(
                "select * from lifeswitch_chat.read_plan_v1($1)", context_a
            )
            nutrition = await conn.fetchrow(
                "select * from lifeswitch_chat.read_nutrition_daily_v1($1,$2,$3)",
                context_a,
                TEST_DAY,
                TEST_DAY,
            )
            timezone = await conn.fetchrow(
                "select * from lifeswitch_chat.read_owner_timezone_v1($1)",
                context_a,
            )
            resistance = await conn.fetchrow(
                "select * from lifeswitch_chat.read_resistance_sessions_v1($1,$2,$3)",
                context_a,
                TEST_DAY,
                TEST_DAY,
            )
            conditioning = await conn.fetchrow(
                "select * from lifeswitch_chat.read_conditioning_sessions_v1($1,$2,$3)",
                context_a,
                TEST_DAY,
                TEST_DAY,
            )
            measurement = await conn.fetchrow(
                "select * from lifeswitch_chat.read_measurement_observations_v1($1,$2,$3)",
                context_a,
                TEST_DAY,
                TEST_DAY,
            )
            training_day = await conn.fetchrow(
                "select * from lifeswitch_chat.read_training_day_v1($1,$2)",
                context_a,
                TEST_DAY,
            )
            progression = await conn.fetchrow(
                "select * from lifeswitch_chat.read_exercise_progression_v1($1,$2,$3,$4)",
                context_a,
                TEST_DAY,
                TEST_DAY,
                "squat",
            )
            frequency = await conn.fetch(
                "select * from lifeswitch_chat.read_exercise_frequency_v1($1,$2,$3)",
                context_a,
                TEST_DAY - dt.timedelta(days=30),
                TEST_DAY,
            )
            lifting_summary = await conn.fetch(
                "select * from lifeswitch_chat.read_lifting_progression_summary_v1($1,$2,$3)",
                context_a,
                TEST_DAY - dt.timedelta(days=30),
                TEST_DAY,
            )
            require(plan["plan_source"] == "agentic_active", "wrong plan source")
            document_value = plan["document"]
            document = (
                json.loads(document_value)
                if isinstance(document_value, str)
                else dict(document_value)
            )
            require(document["primary_goal"] == "Owner A goal", "owner A plan not selected")
            require("coach_notes" not in document, "coach notes escaped whitelist")
            require("body_state" not in document, "body state escaped whitelist")
            require("monitoring_rules" not in document, "monitoring rules escaped whitelist")
            require(
                "macro_notes" not in document.get("nutrition_targets", {}),
                "nested private plan field escaped whitelist",
            )
            require(
                "private_note"
                not in document["nutrition_targets"]["calorie_target"],
                "deep calorie-target field escaped whitelist",
            )
            require(
                "private_note"
                not in document["nutrition_targets"]["protein_target"],
                "deep protein-target field escaped whitelist",
            )
            require(float(nutrition["kcal"]) == 100.0, "cross-owner nutrition leakage")
            require(timezone["timezone_name"] == "America/Chicago", "wrong owner timezone")
            require(resistance["active_set_count"] == 1, "cross-owner resistance leakage")
            require(
                resistance["strength_set_count"] == 1,
                "reviewed strength role was not resolved",
            )
            require(
                resistance["unknown_role_set_count"] == 0,
                "reviewed role remained unknown",
            )
            require(conditioning["name"] == "Owner A walk", "cross-owner conditioning leakage")
            require(float(measurement["weight_value"]) == 200.0, "cross-owner measurement leakage")
            require(training_day["name"] == "Owner A workout", "cross-owner training-day leakage")
            require(float(progression["max_load"]) == 200.0, "cross-owner progression leakage")
            squat_frequency = next(
                row for row in frequency if row["exercise_id"] == "squat"
            )
            require(squat_frequency["set_count"] == 2, "wrong owner A squat frequency")
            require(squat_frequency["session_count"] == 2, "wrong owner A squat exposure")
            require(
                not squat_frequency["role_conflict"],
                "owner A squat was incorrectly marked conflicting",
            )
            squat_summary = next(
                row for row in lifting_summary if row["exercise_id"] == "squat"
            )
            require(float(squat_summary["first_max_load"]) == 180.0, "wrong first load")
            require(float(squat_summary["latest_max_load"]) == 200.0, "wrong latest load")
        checks.extend(
            [
                "guc_forgery_blocked",
                "plan_whitelist",
                "owner_a_isolation",
                "all_gateway_projections",
                "exercise_frequency_projection",
                "lifting_progression_projection",
            ]
        )

        for relation in (
            "lifeswitch_agentic.plan_versions",
            "lifeswitch_agentic.plan_owner_state",
            "lifeswitch_plan.plan_profile",
            "lifeswitch_nutrition.nutrition_day",
            "lifeswitch_nutrition.nutrition_entry",
            "lifeswitch_nutrition.my_food",
            "lifeswitch_nutrition.my_food_serving",
            "lifeswitch_nutrition.meal_item",
            "lifeswitch_training.training_session_current_v",
            "lifeswitch_training.training_session",
            "lifeswitch_training.training_set_log",
            "lifeswitch_training.training_session_role_event",
            "lifeswitch_training.training_set_effective_role_v1",
            "lifeswitch_training.conditioning_session_current_v",
            "public.lifeswitch_measurement_entries",
            "lifeswitch_chat.owner_read_context_v1",
        ):
            async with conn.transaction():
                await conn.execute("set local role lifeswitch_chat_reader_v1")
                await expect_denied(conn.fetchval(f"select count(*) from {relation}"), relation)
        checks.append("direct_source_access_denied")

        async with reader_transaction(conn):
            await expect_denied(
                conn.fetchrow(
                    "select * from lifeswitch_chat.read_plan_v1($1)", uuid4()
                ),
                "forged context",
            )
        checks.append("forged_context_denied")

        async with reader_transaction(second):
            await expect_denied(
                second.fetchrow(
                    "select * from lifeswitch_chat.read_plan_v1($1)", context_a
                ),
                "cross-backend context",
            )
        checks.append("backend_pid_binding")

        context_b = await create_context(conn, OWNER_B)
        async with reader_transaction(conn):
            plan_b = await conn.fetchrow(
                "select * from lifeswitch_chat.read_plan_v1($1)", context_b
            )
            nutrition_b = await conn.fetchrow(
                "select * from lifeswitch_chat.read_nutrition_daily_v1($1,$2,$3)",
                context_b,
                TEST_DAY,
                TEST_DAY,
            )
            lifting_b = await conn.fetch(
                "select * from lifeswitch_chat.read_lifting_progression_summary_v1($1,$2,$3)",
                context_b,
                TEST_DAY - dt.timedelta(days=30),
                TEST_DAY,
            )
            plan_b_value = plan_b["document"]
            plan_b_document = (
                json.loads(plan_b_value)
                if isinstance(plan_b_value, str)
                else dict(plan_b_value)
            )
            require(plan_b_document["primary_goal"] == "Owner B goal", "owner B plan missing")
            require(float(nutrition_b["kcal"]) == 900.0, "owner B nutrition missing")
            require(len(lifting_b) == 1, "owner B lifting summary leaked another owner")
            require(float(lifting_b[0]["latest_max_load"]) == 400.0, "owner B lift missing")
        checks.append("owner_b_isolation")

        ended_context_a = context_a
        await end_context(conn, context_a)
        context_a = None
        async with reader_transaction(conn):
            await expect_denied(
                conn.fetchrow(
                    "select * from lifeswitch_chat.read_plan_v1($1)",
                    ended_context_a,
                ),
                "expired context",
            )
        checks.append("ended_context_denied")

        print(json.dumps({"status": "passed", "checks": checks}, sort_keys=True))
    finally:
        if context_a is not None:
            await end_context(conn, context_a)
        if context_b is not None:
            await end_context(conn, context_b)
        await second.close()
        await conn.close()


asyncio.run(main())
