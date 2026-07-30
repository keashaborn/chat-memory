BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
DECLARE
  source_table text;
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'pipeline-status migration requires sage'
      USING ERRCODE = '42501';
  END IF;
  IF to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'current actor resolver is absent';
  END IF;
  FOREACH source_table IN ARRAY ARRAY[
    'evidence',
    'evidence_extraction_packet_v5_local',
    'observation',
    'observation_entity_binding',
    'observation_entailment_v5',
    'projection_plan_item',
    'projection_plan_observation',
    'projection_review',
    'claim',
    'claim_observation',
    'final_answer_memory_binding_v1'
  ] LOOP
    IF to_regclass(format('memory.%I', source_table)) IS NULL THEN
      RAISE EXCEPTION 'pipeline-status source table % is absent', source_table;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = source_table
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION
        'pipeline-status source table % does not force RLS',
        source_table;
    END IF;
  END LOOP;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_pipeline_status_reader_v1') IS NULL THEN
    CREATE ROLE memory_pipeline_status_reader_v1
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'memory_pipeline_status_reader_v1'
      AND (
        rolcanlogin
        OR rolsuper
        OR rolcreatedb
        OR rolcreaterole
        OR rolinherit
        OR rolbypassrls
      )
  ) THEN
    RAISE EXCEPTION 'pipeline-status reader role is overprivileged';
  END IF;
END
$role$;

DO $policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.evidence'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.observation'::regclass,
    'memory.observation_entity_binding'::regclass,
    'memory.observation_entailment_v5'::regclass,
    'memory.projection_plan_item'::regclass,
    'memory.projection_plan_observation'::regclass,
    'memory.projection_review'::regclass,
    'memory.claim'::regclass,
    'memory.claim_observation'::regclass,
    'memory.final_answer_memory_binding_v1'::regclass
  ] LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS admin_pipeline_status_read ON %s',
      target
    );
    EXECUTE format(
      'CREATE POLICY admin_pipeline_status_read ON %s '
      'FOR SELECT TO memory_pipeline_status_reader_v1 '
      'USING (owner_user_id = memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$policies$;

GRANT USAGE ON SCHEMA memory TO memory_pipeline_status_reader_v1;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_pipeline_status_reader_v1;
GRANT SELECT ON
  memory.evidence,
  memory.evidence_extraction_packet_v5_local,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_entailment_v5,
  memory.projection_plan_item,
  memory.projection_plan_observation,
  memory.projection_review,
  memory.claim,
  memory.claim_observation,
  memory.final_answer_memory_binding_v1
TO memory_pipeline_status_reader_v1;

CREATE OR REPLACE FUNCTION memory.read_owner_pipeline_status_v1()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  payload jsonb;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'pipeline status requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;

  SELECT jsonb_build_object(
    'schema', 'memory_pipeline_status_v1',
    'stages', jsonb_build_object(
      'evidence_rows', (
        SELECT count(*)
        FROM memory.evidence AS value
        WHERE value.owner_user_id = actor
          AND value.status::text = 'active'
      ),
      'extracted_evidence_rows', (
        SELECT count(DISTINCT value.evidence_id)
        FROM memory.evidence_extraction_packet_v5_local AS value
        WHERE value.owner_user_id = actor
      ),
      'durable_observations', (
        SELECT count(*)
        FROM memory.observation AS value
        WHERE value.owner_user_id = actor
      ),
      'bound_observations', (
        SELECT count(DISTINCT value.observation_id)
        FROM memory.observation_entity_binding AS value
        WHERE value.owner_user_id = actor
      ),
      'evaluated_observations', (
        SELECT count(DISTINCT value.observation_id)
        FROM memory.observation_entailment_v5 AS value
        WHERE value.owner_user_id = actor
      ),
      'claim_plan_items', (
        SELECT count(*)
        FROM memory.projection_plan_item AS value
        WHERE value.owner_user_id = actor
          AND value.lane::text = 'claim'
      ),
      'reviewed_claim_plan_items', (
        SELECT count(*)
        FROM memory.projection_plan_item AS value
        WHERE value.owner_user_id = actor
          AND value.lane::text = 'claim'
          AND EXISTS (
            SELECT 1
            FROM memory.projection_review AS review
            WHERE review.owner_user_id = value.owner_user_id
              AND review.plan_id = value.plan_id
              AND review.projection_ref = value.projection_ref
          )
      ),
      'supported_claims', (
        SELECT count(*)
        FROM memory.claim AS value
        WHERE value.owner_user_id = actor
          AND value.status::text = 'supported'
      ),
      'memory_bound_answers_7d', (
        SELECT count(*)
        FROM memory.final_answer_memory_binding_v1 AS value
        WHERE value.owner_user_id = actor
          AND value.created_at >= now() - interval '7 days'
      )
    ),
    'backlog', jsonb_build_object(
      'waiting_for_binding', (
        SELECT count(*)
        FROM memory.observation AS observation
        WHERE observation.owner_user_id = actor
          AND NOT EXISTS (
            SELECT 1
            FROM memory.observation_entity_binding AS binding
            WHERE binding.owner_user_id = observation.owner_user_id
              AND binding.observation_id = observation.observation_id
          )
      ),
      'waiting_for_entailment', (
        SELECT count(*)
        FROM memory.observation_entity_binding AS binding
        WHERE binding.owner_user_id = actor
          AND NOT EXISTS (
            SELECT 1
            FROM memory.observation_entailment_v5 AS assessment
            WHERE assessment.owner_user_id = binding.owner_user_id
              AND assessment.observation_id = binding.observation_id
          )
      ),
      'waiting_for_claim_review', (
        SELECT count(*)
        FROM memory.projection_plan_item AS item
        WHERE item.owner_user_id = actor
          AND item.lane::text = 'claim'
          AND NOT EXISTS (
            SELECT 1
            FROM memory.projection_review AS review
            WHERE review.owner_user_id = item.owner_user_id
              AND review.plan_id = item.plan_id
              AND review.projection_ref = item.projection_ref
          )
      ),
      'ready_for_materialization', (
        SELECT count(*)
        FROM memory.projection_plan_item AS item
        WHERE item.owner_user_id = actor
          AND item.lane::text = 'claim'
          AND (
            SELECT review.decision::text
            FROM memory.projection_review AS review
            WHERE review.owner_user_id = item.owner_user_id
              AND review.plan_id = item.plan_id
              AND review.projection_ref = item.projection_ref
            ORDER BY review.review_number DESC
            LIMIT 1
          ) = 'authorized'
          AND EXISTS (
            SELECT 1
            FROM memory.projection_plan_observation AS planned
            WHERE planned.owner_user_id = item.owner_user_id
              AND planned.plan_id = item.plan_id
              AND planned.projection_ref = item.projection_ref
          )
          AND NOT EXISTS (
            SELECT 1
            FROM memory.projection_plan_observation AS planned
            JOIN memory.claim_observation AS linked
              ON linked.owner_user_id = planned.owner_user_id
             AND linked.observation_id = planned.observation_id
            WHERE planned.owner_user_id = item.owner_user_id
              AND planned.plan_id = item.plan_id
              AND planned.projection_ref = item.projection_ref
          )
      )
    )
  )
  INTO payload;
  RETURN payload;
END
$function$;

ALTER FUNCTION memory.read_owner_pipeline_status_v1()
OWNER TO memory_pipeline_status_reader_v1;
REVOKE ALL ON FUNCTION memory.read_owner_pipeline_status_v1()
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.read_owner_pipeline_status_v1()
TO brains_app;

COMMIT;
