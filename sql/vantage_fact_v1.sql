BEGIN;

CREATE SCHEMA IF NOT EXISTS vantage_fact;
-- If this errors due to permissions, tell me the exact error text and we’ll adjust.
ALTER SCHEMA vantage_fact OWNER TO sage;

DO $$ BEGIN
    CREATE TYPE vantage_fact.source_status AS ENUM ('pending','processing','done','error','quarantined');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE vantage_fact.claim_status AS ENUM ('active','retracted','superseded','quarantined');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE vantage_fact.contradiction_status AS ENUM ('open','resolved','ignored');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS vantage_fact.source (
    source_id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    source_type text NOT NULL DEFAULT 'text',
    external_id text,
    uri text,
    title text,
    content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    status vantage_fact.source_status NOT NULL DEFAULT 'pending',
    error text,
    processed_at timestamptz,
    content_sha256 text
);

CREATE UNIQUE INDEX IF NOT EXISTS source_external_id_uniq
    ON vantage_fact.source(external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS source_status_idx
    ON vantage_fact.source(status, source_id);

CREATE TABLE IF NOT EXISTS vantage_fact.entity (
    entity_id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    entity_type text NOT NULL DEFAULT 'unknown',
    canonical_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS entity_type_name_idx
    ON vantage_fact.entity(entity_type, canonical_name);

CREATE TABLE IF NOT EXISTS vantage_fact.predicate (
    predicate text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    arg_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    description text
);

CREATE TABLE IF NOT EXISTS vantage_fact.claim (
    claim_id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    subject_entity_id bigint NOT NULL REFERENCES vantage_fact.entity(entity_id) ON DELETE CASCADE,
    predicate text NOT NULL REFERENCES vantage_fact.predicate(predicate) ON DELETE RESTRICT,
    object_entity_id bigint REFERENCES vantage_fact.entity(entity_id) ON DELETE SET NULL,
    object_literal jsonb,
    qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence numeric(4,3) NOT NULL DEFAULT 0.500,
    status vantage_fact.claim_status NOT NULL DEFAULT 'active',
    canonical_key text NOT NULL,
    CONSTRAINT claim_object_oneof_chk CHECK (
        (CASE WHEN object_entity_id IS NULL THEN 0 ELSE 1 END) +
        (CASE WHEN object_literal IS NULL THEN 0 ELSE 1 END) = 1
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS claim_canonical_key_uniq
    ON vantage_fact.claim(canonical_key);

CREATE INDEX IF NOT EXISTS claim_subject_predicate_idx
    ON vantage_fact.claim(subject_entity_id, predicate);

CREATE TABLE IF NOT EXISTS vantage_fact.evidence (
    evidence_id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    claim_id bigint NOT NULL REFERENCES vantage_fact.claim(claim_id) ON DELETE CASCADE,
    source_id bigint NOT NULL REFERENCES vantage_fact.source(source_id) ON DELETE CASCADE,
    span_start integer,
    span_end integer,
    snippet text,
    extractor text,
    extractor_version text,
    extraction_confidence numeric(4,3),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS evidence_claim_idx
    ON vantage_fact.evidence(claim_id);

CREATE INDEX IF NOT EXISTS evidence_source_idx
    ON vantage_fact.evidence(source_id);

CREATE TABLE IF NOT EXISTS vantage_fact.contradiction (
    contradiction_id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    subject_entity_id bigint NOT NULL REFERENCES vantage_fact.entity(entity_id) ON DELETE CASCADE,
    predicate text NOT NULL REFERENCES vantage_fact.predicate(predicate) ON DELETE RESTRICT,
    qualifier_key text NOT NULL DEFAULT '',
    status vantage_fact.contradiction_status NOT NULL DEFAULT 'open',
    description text,
    resolution jsonb,
    resolved_at timestamptz
);

CREATE INDEX IF NOT EXISTS contradiction_status_idx
    ON vantage_fact.contradiction(status, contradiction_id);

CREATE INDEX IF NOT EXISTS contradiction_subject_predicate_idx
    ON vantage_fact.contradiction(subject_entity_id, predicate);

CREATE TABLE IF NOT EXISTS vantage_fact.contradiction_member (
    contradiction_id bigint NOT NULL REFERENCES vantage_fact.contradiction(contradiction_id) ON DELETE CASCADE,
    claim_id bigint NOT NULL REFERENCES vantage_fact.claim(claim_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (contradiction_id, claim_id)
);

COMMIT;
