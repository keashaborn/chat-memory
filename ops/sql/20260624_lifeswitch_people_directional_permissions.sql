-- Make LifeSwitch People permissions directional.
-- grantor_user_id = account/data owner granting access
-- grantee_user_id = person receiving access
--
-- Participant validity is enforced in the API router because PostgreSQL CHECK
-- constraints cannot use subqueries against the relationship table.

alter table lifeswitch_people.relationship_permission
  add column if not exists grantor_user_id uuid,
  add column if not exists grantee_user_id uuid;

update lifeswitch_people.relationship_permission rp
set
  grantor_user_id = r.requester_user_id,
  grantee_user_id = r.addressee_user_id,
  updated_at = now()
from lifeswitch_people.relationship r
where rp.relationship_id = r.relationship_id
  and (rp.grantor_user_id is null or rp.grantee_user_id is null);

alter table lifeswitch_people.relationship_permission
  alter column grantor_user_id set not null,
  alter column grantee_user_id set not null;

alter table lifeswitch_people.relationship_permission
  drop constraint if exists relationship_permission_relationship_id_permission_scope_key;

create unique index if not exists ux_lifeswitch_relationship_permission_directional
  on lifeswitch_people.relationship_permission (
    relationship_id,
    grantor_user_id,
    grantee_user_id,
    permission_scope
  );

alter table lifeswitch_people.relationship_permission
  drop constraint if exists ck_relationship_permission_direction_not_self;

alter table lifeswitch_people.relationship_permission
  add constraint ck_relationship_permission_direction_not_self
  check (grantor_user_id <> grantee_user_id);

alter table lifeswitch_people.relationship_permission
  drop constraint if exists relationship_permission_permission_scope_check;

alter table lifeswitch_people.relationship_permission
  add constraint relationship_permission_permission_scope_check
  check (permission_scope in (
    'messages:send',
    'training:view',
    'nutrition:view',
    'measurements:view',
    'measurements:enter',
    'measurements:edit_recent',
    'plan:view',
    'plan:comment',
    'plan:edit',
    'workout_template:share',
    'workout_template:copy'
  ));

create index if not exists ix_lifeswitch_relationship_permission_grantor
  on lifeswitch_people.relationship_permission(grantor_user_id, grantee_user_id, is_enabled, permission_scope);

create index if not exists ix_lifeswitch_relationship_permission_grantee
  on lifeswitch_people.relationship_permission(grantee_user_id, grantor_user_id, is_enabled, permission_scope);
