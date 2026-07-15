BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 predicate migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.predicate') IS NULL THEN
    RAISE EXCEPTION 'memory.predicate registry is required';
  END IF;
END
$block$;

INSERT INTO memory.predicate(
  predicate,
  object_kind,
  cardinality,
  description
) VALUES
  (
    'stopped_alcohol_use',
    'literal',
    'many',
    'User self-reported cessation of alcohol use with an anchored date'
  ),
  (
    'spends_time_on',
    'literal',
    'many',
    'User self-reported recurring time spent using or working with an object'
  ),
  (
    'does_activity',
    'literal',
    'many',
    'User self-reported recurring activity'
  ),
  (
    'raises',
    'literal',
    'many',
    'User self-reported raising or husbandry of an animal type'
  )
ON CONFLICT (predicate) DO NOTHING;

DO $block$
DECLARE
  mismatch_count integer;
BEGIN
  WITH expected(predicate, object_kind, cardinality, description) AS (
    VALUES
      (
        'stopped_alcohol_use',
        'literal',
        'many',
        'User self-reported cessation of alcohol use with an anchored date'
      ),
      (
        'spends_time_on',
        'literal',
        'many',
        'User self-reported recurring time spent using or working with an object'
      ),
      (
        'does_activity',
        'literal',
        'many',
        'User self-reported recurring activity'
      ),
      (
        'raises',
        'literal',
        'many',
        'User self-reported raising or husbandry of an animal type'
      )
  )
  SELECT count(*)
  INTO mismatch_count
  FROM expected
  LEFT JOIN memory.predicate AS actual USING (predicate)
  WHERE actual.predicate IS NULL
     OR actual.object_kind <> expected.object_kind
     OR actual.cardinality <> expected.cardinality
     OR actual.description IS DISTINCT FROM expected.description
     OR NOT actual.active;

  IF mismatch_count <> 0 THEN
    RAISE EXCEPTION 'memory V1 extraction-v3 predicate registry conflict'
      USING ERRCODE='23514';
  END IF;
END
$block$;

COMMIT;
