\set ON_ERROR_STOP on

DO $test$
DECLARE
  function_name text;
BEGIN
  IF current_database() !~ '^memory_contextual_intake_v2_[0-9]+$' THEN
    RAISE EXCEPTION 'security test requires the disposable contextual clone';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid=relation.relnamespace
     WHERE namespace.nspname='memory'
       AND relation.relname='evidence_contextual_span_v2'
       AND relation.relrowsecurity
       AND relation.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'contextual span table must force RLS';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_trigger
     WHERE tgrelid='memory.evidence_contextual_span_v2'::regclass
       AND tgname='evidence_contextual_span_v2_append_only'
       AND tgenabled='O'
       AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'contextual span append-only trigger is absent';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.evidence_contextual_span_v2','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_contextual_span_v2','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_contextual_span_v2','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_contextual_span_v2','DELETE'
     ) THEN
    RAISE EXCEPTION 'brains_app has forbidden direct contextual-span access';
  END IF;
  IF NOT has_table_privilege(
       'memory_evidence_maintainer',
       'memory.evidence_contextual_span_v2',
       'SELECT'
     )
     OR NOT has_table_privilege(
       'memory_evidence_maintainer',
       'memory.evidence_contextual_span_v2',
       'INSERT'
     )
     OR has_table_privilege(
       'memory_evidence_maintainer',
       'memory.evidence_contextual_span_v2',
       'UPDATE'
     )
     OR has_table_privilege(
       'memory_evidence_maintainer',
       'memory.evidence_contextual_span_v2',
       'DELETE'
     ) THEN
    RAISE EXCEPTION 'evidence maintainer contextual-span ACL is invalid';
  END IF;
  IF has_table_privilege(
       'memory_context_rebind_maintainer',
       'memory.evidence_contextual_span_v2',
       'SELECT'
     )
     OR has_table_privilege(
       'memory_context_rebind_maintainer',
       'memory.evidence_contextual_span_v2',
       'INSERT'
     ) THEN
    RAISE EXCEPTION 'context reader has forbidden contextual-span access';
  END IF;
  IF NOT has_table_privilege(
       'memory_intake_maintainer',
       'memory.evidence_contextual_span_v2',
       'SELECT'
     )
     OR has_table_privilege(
       'memory_intake_maintainer',
       'memory.evidence_contextual_span_v2',
       'INSERT,UPDATE,DELETE'
     ) THEN
    RAISE EXCEPTION 'intake maintainer contextual-span ACL is invalid';
  END IF;

  FOREACH function_name IN ARRAY ARRAY[
    'memory.plan_owner_evidence_intake_v2(text,integer,uuid)',
    'memory.preflight_owner_contextual_split_v2(uuid,text,text,jsonb)',
    'memory.apply_owner_contextual_split_v2(uuid,text,text,jsonb,text)',
    'memory.finalize_owner_contextual_split_v2(uuid,text,text,text,integer)'
  ]
  LOOP
    IF NOT has_function_privilege('brains_app',function_name,'EXECUTE')
       OR has_function_privilege('public',function_name,'EXECUTE') THEN
      RAISE EXCEPTION 'restricted function ACL is invalid: %', function_name;
    END IF;
  END LOOP;

  IF pg_get_userbyid(
       (
         SELECT proowner
           FROM pg_proc
          WHERE oid=
            'memory.plan_owner_evidence_intake_v2(text,integer,uuid)'
            ::regprocedure
       )
     )<>'memory_intake_maintainer'
     OR pg_get_userbyid(
       (
         SELECT proowner
           FROM pg_proc
          WHERE oid=
            'memory.finalize_owner_contextual_split_v2(uuid,text,text,text,integer)'
            ::regprocedure
       )
     )<>'memory_intake_maintainer'
     OR pg_get_userbyid(
       (
         SELECT proowner
           FROM pg_proc
          WHERE oid=
            'memory.preflight_owner_contextual_split_v2(uuid,text,text,jsonb)'
            ::regprocedure
       )
     )<>'memory_context_rebind_maintainer'
     OR pg_get_userbyid(
       (
         SELECT proowner
           FROM pg_proc
          WHERE oid=
            'memory.apply_owner_contextual_split_v2(uuid,text,text,jsonb,text)'
            ::regprocedure
       )
     )<>'memory_evidence_maintainer' THEN
    RAISE EXCEPTION 'restricted function ownership is invalid';
  END IF;
  IF NOT has_function_privilege(
       'memory_context_rebind_maintainer',
       'memory.v5_jsonb_exact_keys(jsonb,text[])',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_context_rebind_maintainer',
       'memory.atomic_span_uuid_v1(text)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_context_rebind_maintainer',
       'memory.preflight_owner_contextual_split_v2(uuid,text,text,jsonb)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_evidence_maintainer',
       'memory.preflight_owner_contextual_split_v2(uuid,text,text,jsonb)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'context rebind helper ACL is invalid';
  END IF;
END
$test$;
