\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $preflight$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.v5_2_atom_admission_proposal
    WHERE policy_version = 'memory_v1_v5_2_atom_admission_policy_v2'
  ) THEN
    RAISE EXCEPTION
      'V5.2 atom-admission V2 rollback refused because V2 proposals exist';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.apply_owner_v5_2_atom_review_v2(
  uuid,uuid,uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_v5_2_atom_apply_v2(uuid);
DROP FUNCTION IF EXISTS memory.review_owner_v5_2_atom_proposal_v2(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_v5_2_atom_review_v2(
  uuid,memory.v5_2_atom_review_decision,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.record_owner_v5_2_atom_proposal_v2(
  uuid,uuid,uuid,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_atom_admission_v2(uuid);

ALTER TABLE memory.v5_2_atom_admission_proposal
  DROP CONSTRAINT IF EXISTS
    v5_2_atom_admission_proposal_policy_version_check;
ALTER TABLE memory.v5_2_atom_admission_proposal
  ADD CONSTRAINT v5_2_atom_admission_proposal_policy_version_check
  CHECK (policy_version = 'memory_v1_v5_2_atom_admission_policy_v1');

COMMIT;
