BEGIN;

DO $$
DECLARE
  c text;
BEGIN
  -- Drop the old UNIQUE(alias_norm, locale) constraint if present (name may vary)
  SELECT conname INTO c
  FROM pg_constraint
  WHERE conrelid = 'catalog_dev.exercise_alias'::regclass
    AND contype = 'u'
    AND pg_get_constraintdef(oid) ILIKE '%(alias_norm, locale)%';

  IF c IS NOT NULL THEN
    EXECUTE format('ALTER TABLE catalog_dev.exercise_alias DROP CONSTRAINT %I', c);
  END IF;
END $$;

ALTER TABLE catalog_dev.exercise_alias
  ADD CONSTRAINT uq_exercise_alias_exercise_alias_locale
  UNIQUE (exercise_id, alias_norm, locale);

COMMIT;
