-- Empty-only rollback for governed_memory_pilot_marker_0004.
-- Once the marker exists, rollback is permanently refused.

DO $preflight$
BEGIN
  IF pg_catalog.current_database() <> 'governed_memory'
     OR current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'pilot marker rollback requires governed_memory_owner in governed_memory';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.pilot_marker LIMIT 1) THEN
    RAISE EXCEPTION
      'pilot marker rollback refused because a pilot has started';
  END IF;
END;
$preflight$;

DROP FUNCTION memory_private.read_pilot_marker();
DROP FUNCTION memory_private.mark_pilot_started(
  text,uuid,text,text,timestamptz
);
DROP TRIGGER pilot_marker_append_only ON memory.pilot_marker;
DROP FUNCTION memory_private.guard_pilot_marker_append_only();
DROP TABLE memory.pilot_marker;
DROP FUNCTION memory_private.pilot_marker_receipt_sha256(
  text,uuid,text,text,timestamptz
);
