BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 shadow read API migration requires sage';
  END IF;
  IF to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL
     OR to_regclass('memory.projection_apply_event') IS NULL THEN
    RAISE EXCEPTION 'V5 shadow read API prerequisites are missing';
  END IF;
END
$prerequisite$;

DO $reader_role$
DECLARE
  role_state record;
BEGIN
  SELECT rolcanlogin, rolinherit, rolbypassrls, rolsuper,
         rolcreatedb, rolcreaterole
    INTO role_state
    FROM pg_roles
   WHERE rolname='memory_v5_reader';
  IF NOT FOUND THEN
    CREATE ROLE memory_v5_reader
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  ELSIF role_state.rolcanlogin
     OR role_state.rolinherit
     OR role_state.rolbypassrls
     OR role_state.rolsuper
     OR role_state.rolcreatedb
     OR role_state.rolcreaterole THEN
    RAISE EXCEPTION 'existing memory_v5_reader role is not restricted';
  END IF;
END
$reader_role$;

CREATE OR REPLACE FUNCTION memory.require_v5_reader_context()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' OR current_user <> 'memory_v5_reader' THEN
    RAISE EXCEPTION 'V5 reader requires the brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'V5 reader requires an authenticated transaction-local actor'
      USING ERRCODE='42501';
  END IF;
  RETURN actor;
END
$function$;

ALTER FUNCTION memory.require_v5_reader_context() OWNER TO memory_v5_reader;
REVOKE ALL ON FUNCTION memory.require_v5_reader_context()
  FROM PUBLIC,brains_app,memory_v5_writer;

GRANT USAGE ON SCHEMA memory TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_reader;
GRANT SELECT ON
  memory.claim,
  memory.claim_observation,
  memory.evidence,
  memory.observation,
  memory.observation_temporal,
  memory.projection_apply_event
TO memory_v5_reader;

DO $reader_policies$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'claim_observation',
    'observation',
    'observation_temporal',
    'projection_apply_event'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
        FROM pg_policies
       WHERE schemaname='memory'
         AND tablename=relation_name
         AND policyname='owner_isolation_v5_reader'
    ) THEN
      EXECUTE format(
        'CREATE POLICY owner_isolation_v5_reader ON memory.%I '
        'FOR SELECT TO memory_v5_reader '
        'USING (owner_user_id=(SELECT memory.current_actor_user_id()))',
        relation_name
      );
    END IF;
  END LOOP;
END
$reader_policies$;

CREATE INDEX IF NOT EXISTS projection_apply_event_owner_claim_idx
  ON memory.projection_apply_event(owner_user_id,resulting_claim_id)
  WHERE resulting_claim_id IS NOT NULL;

CREATE OR REPLACE FUNCTION memory.read_v5_shadow_claims(
  p_claim_ids uuid[]
)
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  canonical_key text,
  canonical_text text,
  predicate text,
  status text,
  sensitivity text,
  importance numeric,
  salience numeric,
  valid_from timestamptz,
  valid_to timestamptz,
  metadata jsonb,
  retrieval_policy jsonb,
  projection_review_decision text,
  projection_apply_outcome text,
  evidence_by_stance jsonb,
  observation_ids text[],
  temporal_facts jsonb,
  project_key text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  requested_count integer;
BEGIN
  actor := memory.require_v5_reader_context();
  requested_count := cardinality(p_claim_ids);
  IF p_claim_ids IS NULL
     OR requested_count < 1
     OR requested_count > 100
     OR array_position(p_claim_ids,NULL::uuid) IS NOT NULL
     OR (SELECT count(DISTINCT value) FROM unnest(p_claim_ids) AS value)
          <> requested_count THEN
    RAISE EXCEPTION 'claim_ids must contain 1 to 100 unique non-null UUIDs'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH requested AS (
    SELECT value AS claim_id
      FROM unnest(p_claim_ids) AS value
  )
  SELECT
    claim.owner_user_id,
    claim.claim_id,
    claim.canonical_key,
    claim.canonical_text,
    claim.predicate,
    claim.status::text,
    claim.sensitivity::text,
    claim.importance,
    claim.salience,
    -- Claim validity and observation-event time are different dimensions.
    -- A historical observation may have ended while the retrospective claim
    -- ("formerly worked as ...") remains valid indefinitely.
    claim.valid_from,
    claim.valid_to,
    claim.metadata,
    claim.retrieval_policy,
    CASE
      WHEN applied.parent_review_state='auto_apply_eligible'
        THEN 'auto_apply_eligible'
      ELSE applied.review_decision::text
    END,
    applied.outcome,
    provenance.evidence_by_stance,
    provenance.observation_ids,
    temporal.temporal_facts,
    NULL::text
  FROM requested
  JOIN memory.claim AS claim
    ON claim.owner_user_id=actor
   AND claim.claim_id=requested.claim_id
   AND claim.canonical_key LIKE 'v5:%'
   AND claim.metadata->>'memory_contract'='memory_projection_v5'
  JOIN LATERAL (
    SELECT event.*
      FROM memory.projection_apply_event AS event
     WHERE event.owner_user_id=claim.owner_user_id
       AND event.resulting_claim_id=claim.claim_id
       AND event.lane='claim'
       AND event.outcome='applied'
     ORDER BY event.resulting_claim_revision_number DESC,event.created_at DESC
     LIMIT 1
  ) AS applied ON TRUE
  LEFT JOIN LATERAL (
    SELECT
      jsonb_build_object(
        'supports',COALESCE(
          jsonb_agg(DISTINCT evidence.evidence_id::text)
            FILTER (WHERE link.stance='supports'),
          '[]'::jsonb
        ),
        'opposes',COALESCE(
          jsonb_agg(DISTINCT evidence.evidence_id::text)
            FILTER (WHERE link.stance='opposes'),
          '[]'::jsonb
        ),
        'qualifies',COALESCE(
          jsonb_agg(DISTINCT evidence.evidence_id::text)
            FILTER (WHERE link.stance='qualifies'),
          '[]'::jsonb
        ),
        'context',COALESCE(
          jsonb_agg(DISTINCT evidence.evidence_id::text)
            FILTER (WHERE link.stance='context'),
          '[]'::jsonb
        )
      ) AS evidence_by_stance,
      COALESCE(
        array_agg(DISTINCT observation.observation_id::text),
        ARRAY[]::text[]
      ) AS observation_ids
    FROM memory.claim_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id=link.owner_user_id
     AND observation.observation_id=link.observation_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
   WHERE link.owner_user_id=claim.owner_user_id
     AND link.claim_id=claim.claim_id
  ) AS provenance ON TRUE
  LEFT JOIN LATERAL (
    SELECT
      max(
        CASE
          WHEN value.semantic='state_validity'
           AND value.basis='instant'
            THEN COALESCE(value.instant_at,lower(value.instant_range))
        END
      ) AS effective_valid_from,
      min(
        CASE
          WHEN value.semantic='state_validity'
           AND value.basis='instant'
           AND value.instant_range IS NOT NULL
           AND NOT upper_inf(value.instant_range)
            THEN upper(value.instant_range)
        END
      ) AS effective_valid_to,
      COALESCE(
        jsonb_agg(
          DISTINCT jsonb_build_object(
            'observation_id',value.observation_id::text,
            'semantic',value.semantic::text,
            'shape',value.shape::text,
            'basis',value.basis::text,
            'certainty',value.certainty::text,
            'precision',value.precision::text,
            'instant_at',value.instant_at,
            'calendar_range',value.calendar_range::text,
            'instant_range',value.instant_range::text,
            'normalized_sha256',value.normalized_sha256
          )
        ) FILTER (WHERE value.observation_id IS NOT NULL),
        '[]'::jsonb
      ) AS temporal_facts
    FROM memory.claim_observation AS link
    JOIN memory.observation_temporal AS value
      ON value.owner_user_id=link.owner_user_id
     AND value.observation_id=link.observation_id
   WHERE link.owner_user_id=claim.owner_user_id
     AND link.claim_id=claim.claim_id
  ) AS temporal ON TRUE
  ORDER BY claim.claim_id;
END
$function$;

ALTER FUNCTION memory.read_v5_shadow_claims(uuid[])
  OWNER TO memory_v5_reader;
REVOKE ALL ON FUNCTION memory.read_v5_shadow_claims(uuid[])
  FROM PUBLIC,memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.read_v5_shadow_claims(uuid[])
  TO brains_app;

COMMIT;
