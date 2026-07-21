\set ON_ERROR_STOP on

BEGIN;

DO $test$
DECLARE
  value record;
BEGIN
  SELECT p.prosecdef,
         p.provolatile,
         r.rolname AS owner_name,
         array_to_string(p.proconfig,',') AS settings
    INTO value
    FROM pg_proc AS p
    JOIN pg_roles AS r ON r.oid=p.proowner
   WHERE p.oid='memory.read_governed_claims_v2(uuid[])'::regprocedure;
  IF value.prosecdef IS DISTINCT FROM true
     OR value.provolatile IS DISTINCT FROM 's'
     OR value.owner_name IS DISTINCT FROM 'memory_v5_reader'
     OR value.settings NOT LIKE '%search_path=""%' THEN
    RAISE EXCEPTION 'V2 governed reader security contract changed';
  END IF;
  IF has_function_privilege('public','memory.read_governed_claims_v2(uuid[])','EXECUTE') THEN
    RAISE EXCEPTION 'PUBLIC can execute the V2 governed reader';
  END IF;
  IF NOT has_function_privilege('brains_app','memory.read_governed_claims_v2(uuid[])','EXECUTE') THEN
    RAISE EXCEPTION 'brains_app cannot execute the V2 governed reader';
  END IF;
END
$test$;

SELECT set_config('test.target_owner_user_id',:'target_owner_user_id',true);
SELECT set_config('test.target_claim_id',:'target_claim_id',true);
SELECT set_config('test.subject_entity_id',:'subject_entity_id',true);
SELECT set_config('test.subject_entity_type',:'subject_entity_type',true);
SELECT set_config('test.object_entity_id',:'object_entity_id',true);
SELECT set_config('test.object_entity_type',:'object_entity_type',true);
SELECT set_config('test.other_owner_claim_id',:'other_owner_claim_id',true);

DO $test$
DECLARE
  target_owner uuid := current_setting('test.target_owner_user_id')::uuid;
  target_claim uuid := current_setting('test.target_claim_id')::uuid;
  expected_subject uuid := current_setting('test.subject_entity_id')::uuid;
  expected_subject_type text := current_setting('test.subject_entity_type');
  expected_object uuid := NULLIF(current_setting('test.object_entity_id'),'')::uuid;
  expected_object_type text := NULLIF(current_setting('test.object_entity_type'),'');
  other_claim uuid := NULLIF(current_setting('test.other_owner_claim_id'),'')::uuid;
  returned record;
  returned_count integer;
BEGIN
  PERFORM set_config('app.user_id','',true);
  BEGIN
    PERFORM * FROM memory.read_governed_claims_v2(ARRAY[target_claim]);
    RAISE EXCEPTION 'V2 governed reader accepted a missing actor';
  EXCEPTION
    WHEN insufficient_privilege OR invalid_parameter_value THEN NULL;
  END;

  PERFORM set_config('app.user_id',target_owner::text,true);
  SELECT *
    INTO STRICT returned
    FROM memory.read_governed_claims_v2(ARRAY[target_claim]);
  IF returned.owner_user_id IS DISTINCT FROM target_owner
     OR returned.claim_id IS DISTINCT FROM target_claim
     OR returned.subject_entity_id IS DISTINCT FROM expected_subject
     OR returned.subject_entity_type IS DISTINCT FROM expected_subject_type
     OR returned.object_entity_id IS DISTINCT FROM expected_object
     OR returned.object_entity_type IS DISTINCT FROM expected_object_type THEN
    RAISE EXCEPTION 'V2 governed reader entity binding differs from authoritative rows';
  END IF;

  IF other_claim IS NOT NULL THEN
    SELECT count(*)
      INTO returned_count
      FROM memory.read_governed_claims_v2(ARRAY[other_claim]);
    IF returned_count<>0 THEN
      RAISE EXCEPTION 'V2 governed reader returned another owner claim';
    END IF;
  END IF;

  BEGIN
    PERFORM * FROM memory.read_governed_claims_v2(ARRAY[]::uuid[]);
    RAISE EXCEPTION 'V2 governed reader accepted an empty request';
  EXCEPTION
    WHEN invalid_parameter_value THEN NULL;
  END;

  BEGIN
    PERFORM * FROM memory.read_governed_claims_v2(
      ARRAY[target_claim,target_claim]
    );
    RAISE EXCEPTION 'V2 governed reader accepted duplicate claim IDs';
  EXCEPTION
    WHEN invalid_parameter_value THEN NULL;
  END;
END
$test$;

ROLLBACK;
