\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $rollback$
DECLARE
  definition text;
BEGIN
  IF session_user <> 'sage'
     OR to_regprocedure(
       'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'reviewed temporal rollback prerequisite is absent';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_2_temporal_review_registration
  ) THEN
    RAISE EXCEPTION
      'reviewed temporal registrations exist; restore the pre-install backup';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)'
      ::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  EXECUTE definition;
END
$rollback$;

DROP FUNCTION IF EXISTS memory.apply_owner_v5_2_temporal_reviews_v1(
  uuid,uuid,jsonb
);
DROP FUNCTION IF EXISTS memory.review_owner_v5_2_temporal_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,jsonb,text,jsonb
);
DROP TABLE IF EXISTS memory.v5_2_temporal_review_registration;
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid);

COMMIT;
