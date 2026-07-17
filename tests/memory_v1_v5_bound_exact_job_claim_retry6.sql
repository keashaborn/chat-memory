\set ON_ERROR_STOP on

BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT status,attempts,apply_outcome
FROM memory.requeue_owner_skipped_evidence_job_v5(
  '25939189-2734-4684-92a0-6cd490972ad9',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  '4b5e4c81-7996-5836-a748-b61549f7404f',
  'database_contract_rejected',5,
  'diagnostic_observability_upgrade'
);
CREATE TEMP TABLE retry6_claim_result ON COMMIT DROP AS
SELECT status,attempts,apply_outcome
FROM memory.claim_owner_bound_evidence_job_v5(
  '2f8a1513-af0e-4751-af54-1a8d0425d316',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'relational_extraction','retry6-rollback-test',300,6
);
DO $assert$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM retry6_claim_result
    WHERE status='processing' AND attempts=6 AND apply_outcome='applied'
  ) THEN
    RAISE EXCEPTION 'retry6 exact claim did not apply';
  END IF;
END
$assert$;
ROLLBACK;

BEGIN;
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.claim_owner_bound_evidence_job_v5(
    '820b3ca4-e6d1-45b6-ae9b-3881b729843c',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry6-cross-owner-test',300,6
  );
  RAISE EXCEPTION 'cross-owner retry6 claim was accepted';
EXCEPTION
  WHEN check_violation THEN NULL;
END
$cross_owner$;
ROLLBACK;

BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
DO $ceiling$
BEGIN
  PERFORM * FROM memory.claim_owner_bound_evidence_job_v5(
    '15800ce2-d986-43e4-a9a7-199f1ed463ad',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry7-rejection-test',300,7
  );
  RAISE EXCEPTION 'retry7 ceiling was accepted';
EXCEPTION
  WHEN invalid_parameter_value THEN NULL;
END
$ceiling$;
ROLLBACK;
