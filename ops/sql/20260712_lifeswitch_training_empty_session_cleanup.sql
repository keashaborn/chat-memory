BEGIN;

DO $cleanup$
DECLARE
  zero_set_count integer;
  reviewed_count integer;
BEGIN
  SELECT count(*)
  INTO zero_set_count
  FROM lifeswitch_training.training_session s
  WHERE s.is_active
    AND NOT EXISTS (
      SELECT 1
      FROM lifeswitch_training.training_set_log l
      WHERE l.training_session_id = s.training_session_id
        AND l.is_active
    );

  IF zero_set_count = 0 THEN
    RAISE NOTICE 'No active zero-set Training sessions remain; cleanup already applied.';
    RETURN;
  END IF;

  IF zero_set_count <> 2 THEN
    RAISE EXCEPTION
      'Expected exactly 2 active zero-set Training sessions, found %; refusing cleanup',
      zero_set_count;
  END IF;

  SELECT count(*)
  INTO reviewed_count
  FROM lifeswitch_training.training_session
  WHERE is_active
    AND owner_user_id = '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND training_session_id IN (
      '26f42493-cf27-4df3-b5f3-2a9f0705387f'::uuid,
      '96e95b1d-1565-43ff-ba21-866667cdfe56'::uuid
    )
    AND day = '2026-07-11'::date
    AND name = 'Gastrocnemius Band Push'
    AND finished_at IS NOT NULL;

  IF reviewed_count <> 2 THEN
    RAISE EXCEPTION
      'The active zero-set sessions no longer match the 2 reviewed rows; refusing cleanup';
  END IF;

  UPDATE lifeswitch_training.training_session
  SET is_active = false,
      updated_at = now()
  WHERE training_session_id IN (
      '26f42493-cf27-4df3-b5f3-2a9f0705387f'::uuid,
      '96e95b1d-1565-43ff-ba21-866667cdfe56'::uuid
    )
    AND owner_user_id = '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND is_active;

  RAISE NOTICE 'Deactivated 2 reviewed zero-set Training sessions.';
END
$cleanup$;

COMMIT;
