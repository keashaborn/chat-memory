\set ON_ERROR_STOP on
\pset pager off

DO $metadata$
DECLARE
  relation_name text;
  helper regprocedure;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 target security suite must start as sage';
  END IF;
  FOREACH relation_name IN ARRAY ARRAY[
    'preference_head_v5',
    'preference_revision_v5',
    'project_knowledge_head_v5',
    'project_knowledge_revision_v5'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND relation.relowner = 'sage'::regrole::oid
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'memory.% ownership or forced RLS is unsafe', relation_name;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_policy AS policy
      JOIN pg_class AS relation ON relation.oid = policy.polrelid
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND policy.polname = 'owner_isolation'
        AND policy.polroles = ARRAY['memory_v5_writer'::regrole::oid]
        AND pg_get_expr(policy.polqual, policy.polrelid)
              LIKE '%current_actor_user_id%'
        AND pg_get_expr(policy.polwithcheck, policy.polrelid)
              LIKE '%current_actor_user_id%'
    ) THEN
      RAISE EXCEPTION 'memory.% owner policy is unsafe', relation_name;
    END IF;
    IF has_table_privilege('brains_app', format('memory.%I', relation_name), 'SELECT')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'INSERT')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'UPDATE')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'DELETE') THEN
      RAISE EXCEPTION 'brains_app has direct access to memory.%', relation_name;
    END IF;
    IF NOT has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'SELECT'
       ) OR NOT has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'INSERT'
       ) OR has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'DELETE'
       ) THEN
      RAISE EXCEPTION 'memory.% writer grants are unsafe', relation_name;
    END IF;
  END LOOP;

  IF has_table_privilege(
       'memory_v5_writer', 'memory.preference_revision_v5', 'UPDATE'
     ) OR has_table_privilege(
       'memory_v5_writer', 'memory.project_knowledge_revision_v5', 'UPDATE'
     ) OR has_table_privilege(
       'memory_v5_writer', 'memory.preference_head_v5', 'UPDATE'
     ) OR has_table_privilege(
       'memory_v5_writer', 'memory.project_knowledge_head_v5', 'UPDATE'
     ) THEN
    RAISE EXCEPTION 'V5 target role has table-wide update permission';
  END IF;
  IF NOT has_column_privilege(
       'memory_v5_writer', 'memory.preference_head_v5',
       'current_revision_id', 'UPDATE'
     ) OR NOT has_column_privilege(
       'memory_v5_writer', 'memory.project_knowledge_head_v5',
       'current_revision_id', 'UPDATE'
     ) OR has_column_privilege(
       'memory_v5_writer', 'memory.preference_head_v5',
       'semantic_key_sha256', 'UPDATE'
     ) OR has_column_privilege(
       'memory_v5_writer', 'memory.project_knowledge_head_v5',
       'semantic_key_sha256', 'UPDATE'
     ) THEN
    RAISE EXCEPTION 'V5 head column-level update permissions are unsafe';
  END IF;

  FOREACH helper IN ARRAY ARRAY[
    'memory.guard_v5_durable_actor()'::regprocedure,
    'memory.guard_preference_head_update_v5()'::regprocedure,
    'memory.guard_preference_revision_insert_v5()'::regprocedure,
    'memory.guard_project_head_update_v5()'::regprocedure,
    'memory.guard_project_revision_temporal_v5()'::regprocedure
  ]
  LOOP
    IF EXISTS (
      SELECT 1 FROM pg_proc AS procedure
      WHERE procedure.oid = helper
        AND procedure.proowner <> 'memory_v5_writer'::regrole::oid
    ) OR has_function_privilege('brains_app', helper, 'EXECUTE')
       OR has_function_privilege('public', helper, 'EXECUTE') THEN
      RAISE EXCEPTION 'V5 target helper % is exposed or misowned', helper;
    END IF;
  END LOOP;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.preference_revision_v5'::regclass
      AND confrelid = 'memory.preference_head_v5'::regclass
      AND contype = 'f'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.project_knowledge_revision_v5'::regclass
      AND confrelid = 'memory.project_knowledge_head_v5'::regclass
      AND contype = 'f'
  ) THEN
    RAISE EXCEPTION 'typed V5 revision foreign keys are missing';
  END IF;

  IF to_regclass('memory.user_preference') IS NULL
     OR to_regclass('memory.preference_revision') IS NULL
     OR to_regclass('memory.project_knowledge_head') IS NULL
     OR to_regclass('memory.project_knowledge_revision') IS NULL THEN
    RAISE EXCEPTION 'legacy durable records were destructively removed';
  END IF;
END
$metadata$;

BEGIN;

INSERT INTO memory.project_space(
  project_id, owner_user_id, project_key, display_name
) VALUES
  (
    'a5555555-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'v5-target-owner-a', 'V5 Target Owner A'
  ),
  (
    'b5555555-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'v5-target-owner-b', 'V5 Target Owner B'
  );

CREATE FUNCTION pg_temp.assert_cross_owner_preference_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.preference_head_v5(
      preference_id, owner_user_id, semantic_key_sha256,
      preference_class, preference_domain, preference_key, scope
    ) VALUES (
      'b6666666-2222-4222-8222-222222222222',
      '22222222-2222-4222-8222-222222222222', repeat('b', 64),
      'life', 'music', 'music.classical', 'user_global'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner preference insert succeeded';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_life_control_rejected()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.preference_revision_v5(
      revision_id, owner_user_id, preference_id, revision_number,
      value, preference_polarity, stability, surface_policy, content_sha256
    ) VALUES (
      'a7777777-1111-4111-8111-111111111112',
      '11111111-1111-4111-8111-111111111111',
      'a6666666-1111-4111-8111-111111111112', 1,
      '{"value":"concise"}'::jsonb, 'not_applicable', 'stable',
      'zero_token_control_only', repeat('7', 64)
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'life preference accepted response-control semantics';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_revision_mutation_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    UPDATE memory.preference_revision_v5
    SET value = '{"value":"mutated"}'::jsonb
    WHERE revision_id = 'a7777777-1111-4111-8111-111111111111';
  EXCEPTION WHEN insufficient_privilege OR raise_exception THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'append-only preference revision was updated';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_stale_project_revision_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.project_knowledge_revision_v5(
      revision_id, owner_user_id, project_id, knowledge_id,
      revision_number, canonical_text, content_sha256,
      document_state, authority_level, surface_policy,
      prior_revision_id
    ) VALUES (
      'a9999999-1111-4111-8111-111111111112',
      '11111111-1111-4111-8111-111111111111',
      'a5555555-1111-4111-8111-111111111111',
      'a8888888-1111-4111-8111-111111111111',
      3, 'Skipped revision.', repeat('9', 64),
      'working', 'user_reported', 'exact_project_scope_only',
      'a9999999-1111-4111-8111-111111111111'
    );
  EXCEPTION WHEN serialization_failure THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'stale project revision optimistic lock succeeded';
  END IF;
END
$function$;

SET ROLE memory_v5_writer;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);

INSERT INTO memory.preference_head_v5(
  preference_id, owner_user_id, semantic_key_sha256,
  preference_class, preference_domain, preference_key, scope
) VALUES
  (
    'a6666666-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111', repeat('6', 64),
    'response', 'response_style', 'response.length', 'user_global'
  ),
  (
    'a6666666-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111', repeat('5', 64),
    'life', 'music', 'music.classical', 'user_global'
  );

INSERT INTO memory.preference_revision_v5(
  revision_id, owner_user_id, preference_id, revision_number,
  value, preference_polarity, stability, surface_policy, content_sha256
) VALUES (
  'a7777777-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 1,
  '{"value":"concise"}'::jsonb, 'not_applicable', 'stable',
  'zero_token_control_only', repeat('7', 64)
);

UPDATE memory.preference_head_v5
SET current_revision_id = 'a7777777-1111-4111-8111-111111111111',
    revision_number = 1,
    updated_at = clock_timestamp()
WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
  AND preference_id = 'a6666666-1111-4111-8111-111111111111';

SELECT pg_temp.assert_cross_owner_preference_denied();
SELECT pg_temp.assert_life_control_rejected();
SELECT pg_temp.assert_revision_mutation_denied();

INSERT INTO memory.project_knowledge_head_v5(
  knowledge_id, owner_user_id, project_id, semantic_key_sha256,
  knowledge_kind, knowledge_key
) VALUES (
  'a8888888-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'a5555555-1111-4111-8111-111111111111', repeat('8', 64),
  'requirement', 'security.account_isolation'
);

INSERT INTO memory.project_knowledge_revision_v5(
  revision_id, owner_user_id, project_id, knowledge_id,
  revision_number, canonical_text, content_sha256,
  document_state, authority_level, surface_policy
) VALUES (
  'a9999999-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'a5555555-1111-4111-8111-111111111111',
  'a8888888-1111-4111-8111-111111111111',
  1, 'Account data must never cross owner boundaries.', repeat('9', 64),
  'ratified', 'user_ratified', 'exact_project_scope_only'
);

UPDATE memory.project_knowledge_head_v5
SET current_revision_id = 'a9999999-1111-4111-8111-111111111111',
    revision_number = 1,
    updated_at = clock_timestamp()
WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
  AND project_id = 'a5555555-1111-4111-8111-111111111111'
  AND knowledge_id = 'a8888888-1111-4111-8111-111111111111';

SELECT pg_temp.assert_stale_project_revision_denied();

SELECT set_config(
  'app.user_id', '22222222-2222-4222-8222-222222222222', true
);
SELECT 1 / ((SELECT count(*) FROM memory.preference_head_v5) = 0)::integer;
SELECT 1 / ((SELECT count(*) FROM memory.project_knowledge_head_v5) = 0)::integer;

RESET ROLE;

DO $results$
BEGIN
  IF (SELECT count(*) FROM memory.preference_head_v5) <> 2
     OR (SELECT count(*) FROM memory.preference_revision_v5) <> 1
     OR (SELECT count(*) FROM memory.project_knowledge_head_v5) <> 1
     OR (SELECT count(*) FROM memory.project_knowledge_revision_v5) <> 1 THEN
    RAISE EXCEPTION 'V5 durable target test counts differ';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.preference_head_v5
    WHERE current_revision_id IS NOT NULL
      AND revision_number <> 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_knowledge_head_v5
    WHERE current_revision_id IS NOT NULL
      AND revision_number <> 1
  ) THEN
    RAISE EXCEPTION 'V5 durable head pointer state is invalid';
  END IF;
END
$results$;

ROLLBACK;

SELECT 'memory_v1_projection_targets_v5: PASS' AS result;
