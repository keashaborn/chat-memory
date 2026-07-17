\set ON_ERROR_STOP on

DO $$
DECLARE
  item record;
  missing text;
  unsafe text;
  actual_owner name;
  actual_security_definer boolean;
  actual_config text[];
  brains_execute boolean;
  public_execute boolean;
BEGIN
  WITH expected(table_name) AS (
    VALUES
      ('preference_candidate'),
      ('preference_candidate_evidence'),
      ('preference_candidate_review'),
      ('preference_candidate_review_replacement'),
      ('preference_revision'),
      ('preference_revision_evidence'),
      ('preference_apply_event'),
      ('user_preference'),
      ('project_space'),
      ('project_space_registration_event'),
      ('project_knowledge_candidate'),
      ('project_knowledge_candidate_evidence'),
      ('project_knowledge_candidate_review'),
      ('project_knowledge_candidate_review_replacement'),
      ('project_knowledge_head'),
      ('project_knowledge_revision'),
      ('project_knowledge_revision_evidence'),
      ('project_knowledge_relation'),
      ('project_knowledge_apply_event')
  )
  SELECT string_agg(expected.table_name, ', ' ORDER BY expected.table_name)
  INTO missing
  FROM expected
  LEFT JOIN pg_namespace AS namespace
    ON namespace.nspname = 'memory'
  LEFT JOIN pg_class AS relation
    ON relation.relnamespace = namespace.oid
   AND relation.relname = expected.table_name
   AND relation.relkind IN ('r', 'p')
  WHERE relation.oid IS NULL
     OR NOT relation.relrowsecurity
     OR NOT relation.relforcerowsecurity;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'preference/project tables missing or not forced-RLS: %', missing;
  END IF;

  IF NOT has_schema_privilege('brains_app', 'memory', 'USAGE') THEN
    RAISE EXCEPTION 'brains_app lacks memory schema USAGE';
  END IF;

  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO unsafe
  FROM (VALUES
    ('preference_candidate'),
    ('preference_candidate_evidence'),
    ('project_knowledge_candidate'),
    ('project_knowledge_candidate_evidence')
  ) AS candidate(table_name)
  WHERE NOT has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'SELECT'
        )
     OR NOT has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'INSERT'
        )
     OR has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'UPDATE'
        )
     OR has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'DELETE'
        );

  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'unsafe candidate grants: %', unsafe;
  END IF;

  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO unsafe
  FROM (VALUES
    ('preference_candidate_review'),
    ('preference_candidate_review_replacement'),
    ('preference_revision'),
    ('preference_revision_evidence'),
    ('preference_apply_event'),
    ('user_preference'),
    ('project_space'),
    ('project_space_registration_event'),
    ('project_knowledge_candidate_review'),
    ('project_knowledge_candidate_review_replacement'),
    ('project_knowledge_head'),
    ('project_knowledge_revision'),
    ('project_knowledge_revision_evidence'),
    ('project_knowledge_relation'),
    ('project_knowledge_apply_event')
  ) AS protected(table_name)
  WHERE NOT has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'SELECT'
        )
     OR has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'INSERT'
        )
     OR has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'UPDATE'
        )
     OR has_table_privilege(
          'brains_app', format('memory.%I', table_name), 'DELETE'
        );

  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'unsafe durable/review grants: %', unsafe;
  END IF;

  IF has_table_privilege('brains_app', 'memory.evidence', 'INSERT')
     OR has_table_privilege('brains_app', 'memory.evidence', 'UPDATE')
     OR has_table_privilege('brains_app', 'memory.evidence', 'DELETE') THEN
    RAISE EXCEPTION 'brains_app can directly mutate canonical evidence';
  END IF;

  IF (
    SELECT count(*)
    FROM information_schema.columns
    WHERE table_schema = 'memory'
      AND table_name IN (
        'preference_candidate_review',
        'project_knowledge_candidate_review'
      )
      AND column_name IN ('request_id', 'request_sha256')
      AND is_nullable = 'NO'
  ) <> 4 THEN
    RAISE EXCEPTION 'review idempotency columns are missing or nullable';
  END IF;

  IF to_regclass('memory.preference_review_owner_request_uq') IS NULL
     OR to_regclass('memory.project_review_owner_request_uq') IS NULL THEN
    RAISE EXCEPTION 'review idempotency indexes are missing';
  END IF;

  FOR item IN
    SELECT *
    FROM (VALUES
      ('guard_specialized_active_evidence',
       'memory_evidence_maintainer', true,
       ARRAY['search_path=pg_catalog']::text[], false),
      ('populate_specialized_evidence_link_counts',
       'memory_evidence_maintainer', true,
       ARRAY['search_path=pg_catalog']::text[], false),
      ('guard_preference_review_insert',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog']::text[], false),
      ('guard_project_revision_insert',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog']::text[], false),
      ('review_request_sha',
       'memory_review_maintainer', false,
       ARRAY['search_path=pg_catalog']::text[], false),
      ('register_project_space',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog', 'row_security=on']::text[], true),
      ('review_preference_candidate',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog', 'row_security=on']::text[], true),
      ('review_project_candidate',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog', 'row_security=on']::text[], true),
      ('apply_preference_candidate',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog', 'row_security=on']::text[], true),
      ('apply_project_candidate',
       'memory_review_maintainer', true,
       ARRAY['search_path=pg_catalog', 'row_security=on']::text[], true)
    ) AS expected(
      function_name,
      function_owner,
      security_definer,
      function_config,
      application_api
    )
  LOOP
    SELECT owner.rolname,
           proc.prosecdef,
           proc.proconfig,
           has_function_privilege(
             'brains_app', proc.oid, 'EXECUTE'
           ),
           EXISTS (
             SELECT 1
             FROM aclexplode(
               coalesce(
                 proc.proacl,
                 acldefault('f', proc.proowner)
               )
             ) AS acl
             WHERE acl.grantee = 0
               AND acl.privilege_type = 'EXECUTE'
           )
    INTO actual_owner,
         actual_security_definer,
         actual_config,
         brains_execute,
         public_execute
    FROM pg_proc AS proc
    JOIN pg_namespace AS namespace
      ON namespace.oid = proc.pronamespace
    JOIN pg_roles AS owner
      ON owner.oid = proc.proowner
    WHERE namespace.nspname = 'memory'
      AND proc.proname = item.function_name;

    IF NOT FOUND
       OR actual_owner <> item.function_owner
       OR actual_security_definer <> item.security_definer
       OR actual_config IS DISTINCT FROM item.function_config
       OR brains_execute <> item.application_api
       OR public_execute THEN
      RAISE EXCEPTION 'unsafe preference/project function: %', item.function_name;
    END IF;
  END LOOP;

  FOR item IN
    SELECT role_row.*
    FROM pg_roles AS role_row
    WHERE role_row.rolname IN (
      'memory_evidence_maintainer',
      'memory_review_maintainer'
    )
  LOOP
    IF item.rolcanlogin
       OR item.rolsuper
       OR item.rolcreatedb
       OR item.rolcreaterole
       OR item.rolinherit
       OR item.rolbypassrls THEN
      RAISE EXCEPTION 'unsafe maintainer role: %', item.rolname;
    END IF;
  END LOOP;

  IF (
    SELECT count(*)
    FROM pg_roles
    WHERE rolname IN (
      'memory_evidence_maintainer',
      'memory_review_maintainer'
    )
  ) <> 2 THEN
    RAISE EXCEPTION 'required maintainer role is missing';
  END IF;
END
$$;
