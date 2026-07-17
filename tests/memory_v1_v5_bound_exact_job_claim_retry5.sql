\set ON_ERROR_STOP on

BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT status,attempts,apply_outcome
FROM memory.requeue_owner_skipped_evidence_job_v5(
  '5a1755f6-0940-4644-bd49-01ca0af47d56',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  '8c56ea41-da2a-511b-b6d5-6f114fea240f',
  'uncatalogued_validator_rejection',4,
  'diagnostic_observability_upgrade'
);
CREATE TEMP TABLE retry5_claim_result ON COMMIT DROP AS
SELECT status,attempts,apply_outcome
FROM memory.claim_owner_bound_evidence_job_v5(
  '1a0975c8-ebd2-4ce4-ad46-899bc058b0bd',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'relational_extraction','retry5-rollback-test',300,5
);
DO $assert$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM retry5_claim_result
    WHERE status='processing' AND attempts=5 AND apply_outcome='applied'
  ) THEN
    RAISE EXCEPTION 'retry5 exact claim did not apply';
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
    'de090d77-47ae-40bc-bed8-43fb10cc5bfd',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry5-cross-owner-test',300,5
  );
  RAISE EXCEPTION 'cross-owner retry5 claim was accepted';
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
    'de82f255-db86-4d40-a15a-48867cce48c4',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry6-rejection-test',300,6
  );
  RAISE EXCEPTION 'retry6 ceiling was accepted';
EXCEPTION
  WHEN invalid_parameter_value THEN NULL;
END
$ceiling$;
ROLLBACK;
