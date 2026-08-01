CREATE SCHEMA gm_fixture AUTHORIZATION gm_ci_owner;
CREATE TABLE gm_fixture.governed_records (
    record_key text PRIMARY KEY,
    owner_key text NOT NULL
);
ALTER TABLE gm_fixture.governed_records OWNER TO gm_ci_owner;
ALTER TABLE gm_fixture.governed_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE gm_fixture.governed_records FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON gm_fixture.governed_records AS PERMISSIVE FOR ALL TO gm_ci_reader USING (owner_key = current_setting('app.owner_key', true)) WITH CHECK (owner_key = current_setting('app.owner_key', true));
GRANT USAGE ON SCHEMA gm_fixture TO gm_ci_reader;
GRANT SELECT ON TABLE gm_fixture.governed_records TO gm_ci_reader;
