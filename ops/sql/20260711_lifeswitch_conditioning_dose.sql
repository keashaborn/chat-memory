BEGIN;

ALTER TABLE lifeswitch_training.my_conditioning_prescription
  ADD COLUMN IF NOT EXISTS dose_type text NOT NULL DEFAULT 'open';

ALTER TABLE lifeswitch_training.my_conditioning_prescription
  ADD COLUMN IF NOT EXISTS dose_config jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE lifeswitch_training.my_conditioning_prescription
  DROP CONSTRAINT IF EXISTS ck_my_conditioning_dose_type;

ALTER TABLE lifeswitch_training.my_conditioning_prescription
  ADD CONSTRAINT ck_my_conditioning_dose_type
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
