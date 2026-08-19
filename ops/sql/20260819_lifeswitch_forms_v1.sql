\set ON_ERROR_STOP on

BEGIN;

CREATE SCHEMA IF NOT EXISTS lifeswitch_forms AUTHORIZATION lifeswitch_owner;
REVOKE ALL ON SCHEMA lifeswitch_forms FROM PUBLIC;
GRANT USAGE ON SCHEMA lifeswitch_forms TO lifeswitch_app;

SET ROLE lifeswitch_owner;
SET search_path = lifeswitch_forms, pg_catalog;

CREATE TABLE form_template (
  form_template_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'published', 'archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (form_template_id, owner_user_id)
);

CREATE TABLE form_version (
  form_version_id uuid PRIMARY KEY,
  form_template_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  json_schema jsonb NOT NULL CHECK (jsonb_typeof(json_schema) = 'object'),
  ui_schema jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(ui_schema) = 'object'),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (form_template_id, version),
  UNIQUE (form_version_id, owner_user_id),
  FOREIGN KEY (form_template_id, owner_user_id)
    REFERENCES form_template (form_template_id, owner_user_id)
    ON DELETE CASCADE
);

CREATE TABLE form_entry (
  form_entry_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  subject_id text NOT NULL
    CHECK (char_length(btrim(subject_id)) BETWEEN 1 AND 200),
  form_version_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (form_version_id, owner_user_id)
    REFERENCES form_version (form_version_id, owner_user_id)
    ON DELETE CASCADE
);

CREATE INDEX form_template_owner_created_idx
  ON form_template (owner_user_id, created_at DESC);
CREATE INDEX form_version_template_version_idx
  ON form_version (form_template_id, version DESC);
CREATE INDEX form_entry_owner_time_idx
  ON form_entry (owner_user_id, occurred_at DESC);
CREATE INDEX form_entry_owner_subject_time_idx
  ON form_entry (owner_user_id, subject_id, occurred_at DESC);
CREATE INDEX form_entry_owner_version_time_idx
  ON form_entry (owner_user_id, form_version_id, occurred_at DESC);

ALTER TABLE form_template ENABLE ROW LEVEL SECURITY;
ALTER TABLE form_template FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON form_template
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

ALTER TABLE form_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE form_version FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON form_version
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

ALTER TABLE form_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE form_entry FORCE ROW LEVEL SECURITY;
CREATE POLICY lifeswitch_owner_isolation_v1
ON form_entry
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

REVOKE ALL ON ALL TABLES IN SCHEMA lifeswitch_forms FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
ON ALL TABLES IN SCHEMA lifeswitch_forms TO lifeswitch_app;

ALTER DEFAULT PRIVILEGES FOR ROLE lifeswitch_owner
IN SCHEMA lifeswitch_forms REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE lifeswitch_owner
IN SCHEMA lifeswitch_forms
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lifeswitch_app;

COMMENT ON SCHEMA lifeswitch_forms IS
  'Owner-isolated versioned forms; private application-server capability.';
COMMENT ON TABLE form_template IS
  'Owner-bound form template identity and lifecycle.';
COMMENT ON TABLE form_version IS
  'Immutable versioned JSON Schema and presentation metadata.';
COMMENT ON TABLE form_entry IS
  'Owner-bound form observations validated against a published version.';

RESET ROLE;
COMMIT;
