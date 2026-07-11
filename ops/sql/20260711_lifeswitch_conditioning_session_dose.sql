BEGIN;

ALTER TABLE lifeswitch_training.conditioning_session_log
  ADD COLUMN IF NOT EXISTS dose_type text NOT NULL DEFAULT 'open';

ALTER TABLE lifeswitch_training.conditioning_session_log
  ADD COLUMN IF NOT EXISTS dose_config jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE lifeswitch_training.conditioning_session_log
  DROP CONSTRAINT IF EXISTS ck_conditioning_session_dose_type;

ALTER TABLE lifeswitch_training.conditioning_session_log
  ADD CONSTRAINT ck_conditioning_session_dose_type
  CHECK (
    dose_type IN (
      'open',
      'time',
      'distance',
      'rounds',
      'intervals',
      'laps',
      'repetitions',
      'loaded_carry'
    )
  );

COMMIT;
