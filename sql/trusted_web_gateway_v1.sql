BEGIN;

CREATE SCHEMA IF NOT EXISTS trusted_web;
REVOKE ALL ON SCHEMA trusted_web FROM PUBLIC;

CREATE TABLE IF NOT EXISTS trusted_web.request_rate_window (
    actor_user_id uuid NOT NULL,
    window_start timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_user_id, window_start)
);

CREATE INDEX IF NOT EXISTS request_rate_window_updated_at_idx
    ON trusted_web.request_rate_window (updated_at);

CREATE TABLE IF NOT EXISTS trusted_web.retrieval_audit (
    search_id uuid PRIMARY KEY,
    actor_user_id uuid NOT NULL,
    request_id varchar(128) NOT NULL,
    query_sha256 char(64) NOT NULL
        CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version varchar(100) NOT NULL,
    topic varchar(100) NOT NULL,
    disposition varchar(40) NOT NULL,
    allowed_domains text[] NOT NULL DEFAULT '{}',
    status varchar(40) NOT NULL,
    provider_response_id varchar(200),
    source_metadata jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_metadata) = 'array'),
    source_count integer NOT NULL DEFAULT 0
        CHECK (source_count >= 0 AND source_count <= 50),
    error_code varchar(100),
    latency_ms integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS retrieval_audit_actor_created_idx
    ON trusted_web.retrieval_audit (actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS retrieval_audit_status_created_idx
    ON trusted_web.retrieval_audit (status, created_at DESC);

CREATE TABLE IF NOT EXISTS trusted_web.source_cache (
    source_id varchar(120) PRIMARY KEY,
    authority_type varchar(80) NOT NULL,
    evidence_type varchar(80) NOT NULL,
    url text NOT NULL CHECK (url ~ '^https://'),
    title varchar(500) NOT NULL,
    section_title varchar(200) NOT NULL,
    guidance_text text NOT NULL CHECK (char_length(guidance_text) >= 1 AND char_length(guidance_text) <= 5000),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    status varchar(40) NOT NULL DEFAULT 'active',
    error_code varchar(100),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS source_cache_status_expires_idx
    ON trusted_web.source_cache (status, expires_at);

REVOKE ALL ON ALL TABLES IN SCHEMA trusted_web FROM PUBLIC;

DO $trusted_web_role_revokes$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'REVOKE ALL ON SCHEMA trusted_web FROM %I',
                role_name
            );
            EXECUTE format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA trusted_web FROM %I',
                role_name
            );
        END IF;
    END LOOP;
END
$trusted_web_role_revokes$;

COMMIT;
