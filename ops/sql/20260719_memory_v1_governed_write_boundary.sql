BEGIN;

DO $preflight$
DECLARE
  table_name text;
  function_oid regprocedure;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'governed write-boundary migration requires sage';
  END IF;

  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('memory_review_maintainer') IS NULL THEN
    RAISE EXCEPTION 'governed writer roles are absent';
  END IF;

  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'entity_alias',
    'candidate',
    'claim',
    'claim_revision',
    'claim_evidence',
    'claim_assessment',
    'claim_relation'
  ]
  LOOP
    IF to_regclass(format('memory.%I', table_name)) IS NULL THEN
      RAISE EXCEPTION 'required governed table memory.% is absent', table_name;
    END IF;
    IF NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'SELECT'
    ) THEN
      RAISE EXCEPTION 'brains_app requires read access to memory.%', table_name;
    END IF;
  END LOOP;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure,
    'memory.apply_projection_v5(uuid,uuid,text,uuid,text)'::regprocedure,
    'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)'::regprocedure,
    'memory.read_v5_shadow_claims(uuid[])'::regprocedure,
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_proc
      WHERE oid = function_oid
        AND prosecdef
        AND proconfig IN (
          ARRAY['search_path=pg_catalog']::text[],
          ARRAY['search_path=""']::text[]
        )
    ) THEN
      RAISE EXCEPTION 'controlled function % is not fail-closed', function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app', function_oid, 'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app cannot execute controlled function %', function_oid;
    END IF;
  END LOOP;

  IF NOT has_table_privilege('memory_v5_writer','memory.entity','INSERT,UPDATE')
     OR NOT has_table_privilege('memory_v5_writer','memory.entity_alias','INSERT,UPDATE')
     OR NOT has_table_privilege('memory_v5_writer','memory.claim','INSERT,UPDATE')
     OR NOT has_table_privilege('memory_v5_writer','memory.claim_revision','INSERT')
     OR NOT has_table_privilege('memory_v5_writer','memory.claim_assessment','INSERT') THEN
    RAISE EXCEPTION 'controlled V5 writer privileges are incomplete';
  END IF;
END
$preflight$;

REVOKE INSERT, UPDATE, DELETE ON
  memory.entity,
  memory.entity_alias,
  memory.candidate,
  memory.claim,
  memory.claim_revision,
  memory.claim_evidence,
  memory.claim_assessment,
  memory.claim_relation
FROM brains_app;

DO $postflight$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'entity_alias',
    'candidate',
    'claim',
    'claim_revision',
    'claim_evidence',
    'claim_assessment',
    'claim_relation'
  ]
  LOOP
    IF NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'SELECT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'INSERT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'UPDATE'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'DELETE'
    ) THEN
      RAISE EXCEPTION 'unsafe brains_app privileges remain on memory.%', table_name;
    END IF;
  END LOOP;
END
$postflight$;

COMMIT;
