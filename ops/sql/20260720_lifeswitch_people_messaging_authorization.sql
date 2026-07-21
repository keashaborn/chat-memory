-- Backfill the bilateral messaging capability required by the People API.
-- Run this migration before deploying the authorization-enforcing router.

begin;

insert into lifeswitch_people.relationship_permission (
  relationship_id,
  grantor_user_id,
  grantee_user_id,
  permission_scope,
  permission_level,
  is_enabled,
  notes
)
select
  accepted_relationship.relationship_id,
  direction.grantor_user_id,
  direction.grantee_user_id,
  'messages:send',
  'comment',
  true,
  'Default messaging permission for accepted relationship.'
from lifeswitch_people.relationship as accepted_relationship
cross join lateral (
  values
    (
      accepted_relationship.requester_user_id,
      accepted_relationship.addressee_user_id
    ),
    (
      accepted_relationship.addressee_user_id,
      accepted_relationship.requester_user_id
    )
) as direction(grantor_user_id, grantee_user_id)
where accepted_relationship.status='accepted'
on conflict (
  relationship_id,
  grantor_user_id,
  grantee_user_id,
  permission_scope
)
do nothing;

commit;
