\set ON_ERROR_STOP on

BEGIN;
CREATE SCHEMA IF NOT EXISTS lifeswitch_plan AUTHORIZATION lifeswitch_owner;
SET ROLE lifeswitch_owner;

REVOKE ALL ON SCHEMA lifeswitch_plan FROM PUBLIC;
GRANT USAGE ON SCHEMA lifeswitch_plan TO lifeswitch_app;

CREATE TABLE lifeswitch_plan.plan_profile (
  plan_profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL UNIQUE,
  phase text NOT NULL DEFAULT 'maintenance'
    CHECK (phase IN ('cut', 'maintenance', 'lean_gain', 'recomp', 'other')),
  phase_label text NOT NULL DEFAULT '',
  primary_goal text NOT NULL DEFAULT '',
  start_date date,
  review_date date,
  review_cadence text NOT NULL DEFAULT 'weekly',
  body_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  nutrition_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  training_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  conditioning_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  activity_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  recovery_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  monitoring_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
  coach_notes text NOT NULL DEFAULT '',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX plan_profile_owner_active_idx
  ON lifeswitch_plan.plan_profile (owner_user_id, is_active);
CREATE INDEX plan_profile_updated_idx
  ON lifeswitch_plan.plan_profile (updated_at DESC);

CREATE TABLE lifeswitch_plan.plan_profile_history (
  plan_profile_history_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_profile_id uuid NOT NULL
    REFERENCES lifeswitch_plan.plan_profile(plan_profile_id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL,
  snapshot_reason text NOT NULL DEFAULT 'manual_update',
  snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX plan_profile_history_owner_created_idx
  ON lifeswitch_plan.plan_profile_history (owner_user_id, created_at DESC);
CREATE INDEX plan_profile_history_profile_created_idx
  ON lifeswitch_plan.plan_profile_history (plan_profile_id, created_at DESC);

CREATE TABLE lifeswitch_plan.plan_comment (
  plan_comment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_profile_id uuid NOT NULL
    REFERENCES lifeswitch_plan.plan_profile(plan_profile_id) ON DELETE CASCADE,
  target_user_id uuid NOT NULL,
  author_user_id uuid NOT NULL,
  comment_text text NOT NULL CHECK (btrim(comment_text) <> ''),
  comment_kind text NOT NULL DEFAULT 'comment',
  is_active boolean NOT NULL DEFAULT true,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX plan_comment_target_created_idx
  ON lifeswitch_plan.plan_comment (target_user_id, created_at DESC);
CREATE INDEX plan_comment_profile_created_idx
  ON lifeswitch_plan.plan_comment (plan_profile_id, created_at DESC);

CREATE FUNCTION lifeswitch_plan.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION lifeswitch_plan.set_updated_at() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lifeswitch_plan.set_updated_at() TO lifeswitch_owner;

CREATE TRIGGER plan_profile_set_updated_at
BEFORE UPDATE ON lifeswitch_plan.plan_profile
FOR EACH ROW EXECUTE FUNCTION lifeswitch_plan.set_updated_at();

CREATE TRIGGER plan_comment_set_updated_at
BEFORE UPDATE ON lifeswitch_plan.plan_comment
FOR EACH ROW EXECUTE FUNCTION lifeswitch_plan.set_updated_at();

ALTER TABLE lifeswitch_plan.plan_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_plan.plan_profile FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_plan.plan_profile
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

ALTER TABLE lifeswitch_plan.plan_profile_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_plan.plan_profile_history FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_plan.plan_profile_history
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_plan.plan_profile parent
    WHERE parent.plan_profile_id = plan_profile_history.plan_profile_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_plan.plan_profile parent
    WHERE parent.plan_profile_id = plan_profile_history.plan_profile_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_plan.plan_comment ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_plan.plan_comment FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_plan.plan_comment
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  target_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_plan.plan_profile parent
    WHERE parent.plan_profile_id = plan_comment.plan_profile_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  target_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND author_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_plan.plan_profile parent
    WHERE parent.plan_profile_id = plan_comment.plan_profile_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

GRANT SELECT, INSERT, UPDATE, DELETE
ON lifeswitch_plan.plan_profile,
   lifeswitch_plan.plan_profile_history,
   lifeswitch_plan.plan_comment
TO lifeswitch_app;

RESET ROLE;
COMMIT;
