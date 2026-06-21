alter table lifeswitch_training.workout_template_exercise
  add column if not exists display_name_snapshot text;

-- Backfill catalog/library names when exercise_id is a catalog UUID.
update lifeswitch_training.workout_template_exercise wte
set display_name_snapshot = e.display_name
from catalog_dev.exercise e
where wte.display_name_snapshot is null
  and wte.exercise_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  and e.exercise_id = wte.exercise_id::uuid;

-- Backfill user/custom names from my_exercise through the owning template.
update lifeswitch_training.workout_template_exercise wte
set display_name_snapshot = me.display_name
from lifeswitch_training.workout_template wt
join lifeswitch_training.my_exercise me
  on me.owner_user_id = wt.owner_user_id
where wte.display_name_snapshot is null
  and wte.workout_template_id = wt.workout_template_id
  and me.exercise_id = wte.exercise_id;
