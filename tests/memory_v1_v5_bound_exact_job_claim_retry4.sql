\set ON_ERROR_STOP on

BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
CREATE TEMP TABLE retry4_claim_result ON COMMIT DROP AS
SELECT status,attempts,apply_outcome
FROM memory.claim_owner_bound_evidence_job_v5(
  '8fcb2f87-9ae8-5ae2-b133-45b328ebcb31',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'relational_extraction','retry4-rollback-test',300,4
);
DO $assert$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM retry4_claim_result
    WHERE status='processing' AND attempts=4 AND apply_outcome='applied'
  ) THEN
    RAISE EXCEPTION 'retry4 exact claim did not apply';
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
    '5bd86ca4-b217-59f1-b617-b75ad371b949',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry4-cross-owner-test',300,4
  );
  RAISE EXCEPTION 'cross-owner retry4 claim was accepted';
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
    '63855d8b-3f14-5c8a-81d7-5b992bb31afb',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','retry5-rejection-test',300,5
  );
  RAISE EXCEPTION 'retry5 ceiling was accepted';
EXCEPTION
  WHEN invalid_parameter_value THEN NULL;
END
$ceiling$;
ROLLBACK;
