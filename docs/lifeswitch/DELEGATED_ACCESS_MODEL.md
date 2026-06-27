# LifeSwitch Delegated Access Model

## Current Convention

Frontend API proxies authenticate the current Supabase user and send that identity to Brains as `owner_user_id`.

In delegated views, the frontend may also send `target_user_id`.

Current meaning in backend LifeSwitch routes:

- `owner_user_id`
  - authenticated actor / viewer
  - the user making the request
- `target_user_id`
  - optional person whose LifeSwitch data is being viewed or edited
  - if absent, target defaults to the actor

This naming is historically imperfect. `owner_user_id` often means actor/viewer at the API boundary. A future cleanup may rename this to `actor_user_id` at the proxy/backend boundary.

## Read Delegation

Delegated read access is enforced server-side.

### Training

Routes with delegated training reads call:

- `_resolve_training_view_target(...)`

Delegated access requires:

- `training:view`

### Nutrition

Delegated nutrition day reads call:

- `_resolve_nutrition_view_target(...)`

Delegated access requires:

- `nutrition:view`

### Measurements

Delegated measurement reads call:

- `_resolve_measurements_view_target(...)`

Delegated access requires:

- `measurements:view`

### Plan

Delegated plan reads call:

- `_resolve_plan_target(...)`

Delegated plan read access requires:

- `plan:view`

## Plan Edit and Comment Delegation

Plan writes are separately gated.

### Plan upsert

Delegated plan upsert requires:

- `plan:edit`

### Comment list

Delegated comment list allows any of:

- `plan:view`
- `plan:comment`
- `plan:edit`

### Comment create

Delegated comment creation allows either:

- `plan:comment`
- `plan:edit`

## Permission Direction

LifeSwitch people permissions are directional.

When a user updates permissions on a relationship:

- current actor becomes `grantor_user_id`
- the other relationship participant becomes `grantee_user_id`

That means users grant access to their own LifeSwitch data. They do not grant themselves access to the other person's data.

## Backend Security Boundary

Frontend read-only UI is a convenience layer only.

Backend routes must remain the enforcement boundary for:

- delegated reads
- delegated edits
- comments
- relationship permission updates

## Current Audit Result

The current source audit confirms backend permission checks for:

- `training:view`
- `nutrition:view`
- `measurements:view`
- `plan:view`
- `plan:comment`
- `plan:edit`

The model is acceptable to build on before adding any richer permission-management UI.

## Future Cleanup

Potential later migration:

- rename frontend/backend boundary parameter from `owner_user_id` to `actor_user_id`
- keep `target_user_id` for delegated target identity
- keep data tables using `owner_user_id` where it means actual data owner
- add shared helper functions to reduce duplicated `_has_people_permission` logic across routers
