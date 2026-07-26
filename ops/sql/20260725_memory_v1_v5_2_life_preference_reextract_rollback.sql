BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $rollback$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_job
    WHERE selector_version =
      '20260725_v5_2_life_preference_reextract_v1'
  ) OR EXISTS (
    SELECT 1
    FROM memory.evidence_intake_terminal
    WHERE selector_version =
      '20260725_v5_2_life_preference_reextract_v1'
  ) THEN
    RAISE EXCEPTION
      'cannot remove V5.2 life-preference re-extraction after use';
  END IF;
END
$rollback$;

DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  uuid,
  text,
  text,
  text
);

COMMIT;
