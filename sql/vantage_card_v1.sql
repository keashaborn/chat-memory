BEGIN;

CREATE SCHEMA IF NOT EXISTS vantage_card;
ALTER SCHEMA vantage_card OWNER TO sage;

DO $$ BEGIN
  CREATE TYPE vantage_card.card_status AS ENUM ('active','retired','quarantined');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS vantage_card.card_head (
  card_id bigserial PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  vantage_id text NOT NULL DEFAULT 'default',
  kind text NOT NULL,                 -- e.g., 'preference', 'project', 'belief', 'style'
  topic_key text NOT NULL,            -- stable key, e.g. 'project/resse/fact_field' or 'preference/format/stepwise'

  status vantage_card.card_status NOT NULL DEFAULT 'active',
  strength numeric(4,3) NOT NULL DEFAULT 0.500,     -- activation-weight (decays unless reinforced)
  confidence numeric(4,3) NOT NULL DEFAULT 0.500,   -- epistemic confidence in content

  summary text NOT NULL DEFAULT '',                 -- human-readable current best summary
  payload jsonb NOT NULL DEFAULT '{}'::jsonb        -- structured fields (claims[], counters, etc.)
);

CREATE UNIQUE INDEX IF NOT EXISTS card_head_uniq
  ON vantage_card.card_head(vantage_id, kind, topic_key);

CREATE INDEX IF NOT EXISTS card_head_status_idx
  ON vantage_card.card_head(vantage_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS vantage_card.card_revision (
  revision_id bigserial PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  card_id bigint NOT NULL REFERENCES vantage_card.card_head(card_id) ON DELETE CASCADE,

  prev_revision_id bigint,
  summary text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  reason text,                 -- e.g. 'consolidate', 'user_correction', 'feedback', 'decay'
  delta jsonb NOT NULL DEFAULT '{}'::jsonb          -- what changed (optional)
);

CREATE INDEX IF NOT EXISTS card_revision_card_idx
  ON vantage_card.card_revision(card_id, created_at DESC);

CREATE TABLE IF NOT EXISTS vantage_card.card_link (
  card_id bigint NOT NULL REFERENCES vantage_card.card_head(card_id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),

  link_type text NOT NULL,     -- 'chat_log', 'source', 'claim', 'evidence'
  ref_id text NOT NULL,        -- e.g. chat_log uuid as text, source_id, claim_id
  note text,

  PRIMARY KEY(card_id, link_type, ref_id)
);

CREATE INDEX IF NOT EXISTS card_link_ref_idx
  ON vantage_card.card_link(link_type, ref_id);

CREATE TABLE IF NOT EXISTS vantage_card.card_signal (
  signal_id bigserial PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  vantage_id text NOT NULL DEFAULT 'default',
  kind text NOT NULL,
  topic_key text NOT NULL,

  signal_type text NOT NULL,   -- 'reward', 'punish', 'correction', 'use'
  magnitude numeric(6,3) NOT NULL DEFAULT 1.000,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS card_signal_topic_idx
  ON vantage_card.card_signal(vantage_id, kind, topic_key, created_at DESC);

COMMIT;
