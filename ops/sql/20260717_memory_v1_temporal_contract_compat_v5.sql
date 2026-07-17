BEGIN;

DO $guard$
BEGIN
  IF to_regclass('memory.observation_temporal') IS NULL THEN
    RAISE EXCEPTION 'memory.observation_temporal is not installed';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conrelid='memory.observation_temporal'::regclass
       AND conname='observation_temporal_check'
       AND contype='c'
  ) THEN
    RAISE EXCEPTION 'observation_temporal_check is missing';
  END IF;
END
$guard$;

ALTER TABLE memory.observation_temporal
  DROP CONSTRAINT observation_temporal_check;

ALTER TABLE memory.observation_temporal
  ADD CONSTRAINT observation_temporal_check CHECK (
    (basis = 'none'
     AND semantic IN ('none', 'occurrence', 'state_validity', 'planned_time')
     AND shape = 'none' AND source_form = 'none'
     AND certainty = 'unknown' AND precision = 'unknown'
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NULL AND recurrence IS NULL
     AND NOT anchored_to_source_time)
    OR
    (basis = 'instant'
     AND semantic <> 'none'
     AND source_form IN ('absolute', 'implicit_source_time')
     AND precision IN ('exact', 'minute')
     AND calendar_range IS NULL AND relative_offset IS NULL AND recurrence IS NULL
     AND (
       (shape = 'instant' AND instant_at IS NOT NULL AND instant_range IS NULL)
       OR
       (shape = 'bounded_interval' AND instant_at IS NULL
        AND instant_range IS NOT NULL
        AND NOT lower_inf(instant_range) AND NOT upper_inf(instant_range))
       OR
       (shape = 'open_interval' AND instant_at IS NULL
        AND instant_range IS NOT NULL
        AND (lower_inf(instant_range) <> upper_inf(instant_range)))
     )
     AND (anchored_to_source_time = (source_form = 'implicit_source_time')))
    OR
    (basis = 'calendar'
     AND semantic <> 'none'
     AND shape IN ('instant', 'bounded_interval', 'open_interval')
     AND source_form IN ('absolute', 'partial_absolute')
     AND precision IN ('day', 'month', 'year')
     AND instant_at IS NULL AND calendar_range IS NOT NULL
     AND instant_range IS NULL AND relative_offset IS NULL AND recurrence IS NULL
     AND (anchored_to_source_time = (source_form = 'partial_absolute'))
     AND (
       (shape IN ('instant', 'bounded_interval')
        AND NOT lower_inf(calendar_range) AND NOT upper_inf(calendar_range))
       OR
       (shape = 'open_interval'
        AND (lower_inf(calendar_range) <> upper_inf(calendar_range)))
     ))
    OR
    (basis = 'relative'
     AND semantic <> 'none'
     AND shape IN ('instant', 'open_interval')
     AND source_form = 'relative'
     AND precision IN ('minute', 'day', 'month', 'year', 'unknown')
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NOT NULL
     AND memory.v5_relative_offset_valid(relative_offset)
     AND recurrence IS NULL AND anchored_to_source_time)
    OR
    (basis = 'recurring'
     AND semantic <> 'none' AND shape = 'recurring' AND source_form = 'none'
     AND precision = 'unknown'
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NULL AND recurrence IS NOT NULL
     AND memory.v5_recurrence_valid(recurrence)
     AND NOT anchored_to_source_time)
  );

COMMENT ON CONSTRAINT observation_temporal_check
  ON memory.observation_temporal IS
  'V5.1 temporal value matrix: preserves unknown-time semantics, trusted partial-calendar anchors, and relative state intervals.';

COMMIT;
