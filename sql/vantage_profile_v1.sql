BEGIN;

CREATE SCHEMA IF NOT EXISTS vantage_profile;
ALTER SCHEMA vantage_profile OWNER TO sage;

CREATE TABLE IF NOT EXISTS vantage_profile.registry (
  user_id text NOT NULL,
  vantage_id text NOT NULL,

  profile_id text,
  name text NOT NULL,

  is_default boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT false,

  state jsonb NOT NULL DEFAULT '{}'::jsonb,

  source text NOT NULL DEFAULT 'supabase',
  source_updated_at timestamptz,
  last_applied_at timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (user_id, vantage_id)
);

CREATE INDEX IF NOT EXISTS registry_user_idx
  ON vantage_profile.registry(user_id);

CREATE INDEX IF NOT EXISTS registry_active_idx
  ON vantage_profile.registry(user_id, is_active, updated_at DESC);

CREATE INDEX IF NOT EXISTS registry_default_idx
  ON vantage_profile.registry(user_id, is_default, updated_at DESC);

CREATE INDEX IF NOT EXISTS registry_updated_idx
  ON vantage_profile.registry(updated_at DESC);

COMMIT;
