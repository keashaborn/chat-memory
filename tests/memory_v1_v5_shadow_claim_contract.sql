\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT 1 / (((
  SELECT count(*)
  FROM memory.read_v5_shadow_claims(ARRAY[
    '2c3ab91d-c423-43d9-8d55-19e4f1069028'::uuid,
    '3073b518-1f12-4fa3-93d6-eaf0b22f864c'::uuid
  ]) AS value
  WHERE value.predicate='occupation.works_as'
    AND value.valid_to IS NULL
    AND jsonb_array_length(value.temporal_facts) > 0
)=2)::integer);

SELECT 1 / (((
  SELECT count(*)
  FROM memory.read_v5_shadow_claims(ARRAY[
    'b93c565d-6511-4648-b5e4-5c441e3373f8'::uuid,
    'f4688838-7193-4e0e-961f-c9bbbf9904c7'::uuid
  ]) AS value
  WHERE (
    value.claim_id='b93c565d-6511-4648-b5e4-5c441e3373f8'::uuid
    AND value.retrieval_policy->>'surface_policy'='direct_or_relevant'
  ) OR (
    value.claim_id='f4688838-7193-4e0e-961f-c9bbbf9904c7'::uuid
    AND value.retrieval_policy->>'surface_policy'='explicit_recall_only'
  )
)=2)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / (((
  SELECT count(*)
  FROM memory.read_v5_shadow_claims(ARRAY[
    '2c3ab91d-c423-43d9-8d55-19e4f1069028'::uuid,
    '3073b518-1f12-4fa3-93d6-eaf0b22f864c'::uuid,
    'b93c565d-6511-4648-b5e4-5c441e3373f8'::uuid,
    'f4688838-7193-4e0e-961f-c9bbbf9904c7'::uuid
  ])
)=0)::integer);

RESET SESSION AUTHORIZATION;
ROLLBACK;

DO $maintenance_denial$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(
      ARRAY['2c3ab91d-c423-43d9-8d55-19e4f1069028'::uuid]
    );
    RAISE EXCEPTION 'maintenance-session V5 read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$maintenance_denial$;

SELECT 1 / (((
  SELECT count(*)
  FROM pg_proc
  WHERE oid='memory.read_v5_shadow_claims(uuid[])'::regprocedure
    AND prosecdef
    AND provolatile='s'
)=1)::integer);

SELECT 'memory_v1_v5_shadow_claim_contract: PASS' AS result;
