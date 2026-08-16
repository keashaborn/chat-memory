from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class OwnerSurface:
    table: str
    owner_filter: str


SURFACES = (
    OwnerSurface("lifeswitch_nutrition.meal", "row.owner_user_id::text = $1"),
    OwnerSurface(
        "lifeswitch_nutrition.meal_item",
        "EXISTS (SELECT 1 FROM lifeswitch_nutrition.meal p "
        "WHERE p.meal_id=row.meal_id AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface("lifeswitch_nutrition.meal_plan", "row.owner_user_id::text = $1"),
    OwnerSurface(
        "lifeswitch_nutrition.meal_plan_item",
        "EXISTS (SELECT 1 FROM lifeswitch_nutrition.meal_plan p "
        "WHERE p.meal_plan_id=row.meal_plan_id AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface("lifeswitch_nutrition.my_food", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_nutrition.my_food_override", "row.owner_user_id::text = $1"),
    OwnerSurface(
        "lifeswitch_nutrition.my_food_serving",
        "EXISTS (SELECT 1 FROM lifeswitch_nutrition.my_food p "
        "WHERE p.my_food_id=row.my_food_id AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface("lifeswitch_nutrition.nutrition_day", "row.owner_user_id::text = $1"),
    OwnerSurface(
        "lifeswitch_nutrition.nutrition_day_completion_event",
        "row.owner_user_id::text = $1",
    ),
    OwnerSurface(
        "lifeswitch_nutrition.nutrition_entry",
        "EXISTS (SELECT 1 FROM lifeswitch_nutrition.nutrition_day p "
        "WHERE p.nutrition_day_id=row.nutrition_day_id AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface("lifeswitch_training.conditioning_session_log", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.my_conditioning_prescription", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.my_exercise", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.my_exercise_role_event", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_observation_event", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_session", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_session_current_v", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_session_role_event", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_set_effective_role_v1", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_set_log", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.training_set_log_segment", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.workout_template", "row.owner_user_id::text = $1"),
    OwnerSurface(
        "lifeswitch_training.workout_template_exercise",
        "EXISTS (SELECT 1 FROM lifeswitch_training.workout_template p "
        "WHERE p.workout_template_id=row.workout_template_id AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface(
        "lifeswitch_training.workout_template_exercise_segment",
        "EXISTS (SELECT 1 FROM lifeswitch_training.workout_template_exercise e "
        "JOIN lifeswitch_training.workout_template p "
        "ON p.workout_template_id=e.workout_template_id "
        "WHERE e.workout_template_exercise_id=row.workout_template_exercise_id "
        "AND p.owner_user_id::text=$1)",
    ),
    OwnerSurface("lifeswitch_training.workout_template_role_event", "row.owner_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.workout_template_share", "row.created_by_user_id::text = $1"),
    OwnerSurface("lifeswitch_training.conditioning_session_current_v", "row.owner_user_id::text = $1"),
    OwnerSurface("public.lifeswitch_measurement_entries", "row.owner_user_id = $1"),
)


OWNER_SQL = """
SELECT DISTINCT owner_id
FROM (
  SELECT owner_user_id::text AS owner_id FROM lifeswitch_nutrition.meal
  UNION SELECT owner_user_id::text FROM lifeswitch_nutrition.meal_plan
  UNION SELECT owner_user_id::text FROM lifeswitch_nutrition.my_food
  UNION SELECT owner_user_id::text FROM lifeswitch_nutrition.nutrition_day
  UNION SELECT owner_user_id::text FROM lifeswitch_training.training_session
  UNION SELECT owner_user_id::text FROM lifeswitch_training.workout_template
  UNION SELECT owner_user_id::text FROM public.lifeswitch_measurement_entries
) owners
WHERE owner_id IS NOT NULL AND owner_id <> ''
ORDER BY owner_id
"""


async def _expect_cross_owner_denial(
    app: asyncpg.Connection,
    actor: str,
    other: str,
    statement: str,
) -> None:
    tx = app.transaction()
    await tx.start()
    try:
        await app.execute("select set_config('app.user_id',$1,false)", actor)
        try:
            await app.execute(statement, other)
        except asyncpg.InsufficientPrivilegeError:
            return
        raise AssertionError("cross-owner write unexpectedly succeeded")
    finally:
        await tx.rollback()


async def main() -> None:
    admin = await asyncpg.connect(
        host="127.0.0.1",
        port=55433,
        database="lifeswitch",
        user="lifeswitch_bootstrap",
        password=os.environ["LIFESWITCH_BOOTSTRAP_PASSWORD"],
    )
    app = await asyncpg.connect(
        host="127.0.0.1",
        port=55433,
        database="lifeswitch",
        user="lifeswitch_app_login",
        password=os.environ["LIFESWITCH_APP_PASSWORD"],
    )
    try:
        owners = [str(row["owner_id"]) for row in await admin.fetch(OWNER_SQL)]
        if not owners:
            raise AssertionError("no retained LifeSwitch owners found")

        comparisons = 0
        for owner in owners:
            await app.execute(
                "select set_config('app.user_id',$1,false), "
                "set_config('app.lifeswitch_owner_id',$1,false)",
                owner,
            )
            for surface in SURFACES:
                expected = await admin.fetchval(
                    f"SELECT count(*) FROM {surface.table} row WHERE {surface.owner_filter}",
                    owner,
                )
                actual = await app.fetchval(f"SELECT count(*) FROM {surface.table}")
                if actual != expected:
                    raise AssertionError(
                        f"owner-visible count mismatch for {surface.table}: "
                        f"expected={expected} actual={actual}"
                    )
                comparisons += 1

        other = owners[1] if len(owners) > 1 else "00000000-0000-4000-8000-000000000099"
        await _expect_cross_owner_denial(
            app,
            owners[0],
            other,
            "INSERT INTO lifeswitch_nutrition.meal "
            "(meal_id,owner_user_id,name,meal_type) VALUES "
            "('00000000-0000-4000-8000-00000000c201',$1::uuid,'rls denial','other')",
        )
        await _expect_cross_owner_denial(
            app,
            owners[0],
            other,
            "INSERT INTO lifeswitch_training.workout_template "
            "(workout_template_id,owner_user_id,name,notes,workout_role) VALUES "
            "('00000000-0000-4000-8000-00000000c202',$1::uuid,'rls denial','rollback','strength')",
        )
        await _expect_cross_owner_denial(
            app,
            owners[0],
            other,
            "INSERT INTO public.lifeswitch_measurement_entries "
            "(measurement_entry_id,owner_user_id,local_date,weight_value,weight_unit,"
            "measurement_unit,source,entry_kind) VALUES "
            "('00000000-0000-4000-8000-00000000c203',$1,DATE '2099-12-30',"
            "1,'lb','in','rls_denial','verification')",
        )

        try:
            await app.fetchval("SELECT count(*) FROM lifeswitch_snapshot.analysis_source_row")
        except asyncpg.InsufficientPrivilegeError:
            pass
        else:
            raise AssertionError("runtime app retained direct snapshot access")

        catalog_rows = await app.fetchval("SELECT count(*) FROM catalog_dev.food")
        if catalog_rows is None:
            raise AssertionError("catalog read failed")

        print(
            "OWNER_RLS_PASS "
            f"owners={len(owners)} surfaces={len(SURFACES)} "
            f"comparisons={comparisons} cross_owner_writes=3 snapshot_denied=1"
        )
    finally:
        await app.close()
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
