BEGIN;

DO $$
DECLARE
  lifecycle_events_exist boolean := false;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 evidence lifecycle rollback must run as sage, current_user=%',
      current_user;
  END IF;

  IF to_regclass('memory.evidence_lifecycle_event') IS NOT NULL THEN
    EXECUTE
      'SELECT EXISTS (SELECT 1 FROM memory.evidence_lifecycle_event LIMIT 1)'
      INTO lifecycle_events_exist;
    IF lifecycle_events_exist THEN
      RAISE EXCEPTION
        'refusing to drop nonempty memory.evidence_lifecycle_event';
    END IF;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS candidate_active_evidence_guard ON memory.candidate;
DROP TRIGGER IF EXISTS evidence_insert_only_guard ON memory.evidence;
DROP TRIGGER IF EXISTS evidence_lifecycle_event_append_only_guard
  ON memory.evidence_lifecycle_event;

DROP FUNCTION IF EXISTS memory.transition_evidence_lifecycle(
  uuid, uuid, text, text, text, text, jsonb
);
DROP FUNCTION IF EXISTS memory.guard_candidate_active_evidence();
DROP FUNCTION IF EXISTS memory.guard_evidence_insert_only();
DROP FUNCTION IF EXISTS memory.guard_lifecycle_event_append_only();

DROP TABLE IF EXISTS memory.evidence_lifecycle_event;

GRANT SELECT, INSERT, UPDATE, DELETE ON memory.evidence TO brains_app;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA memory
  FROM memory_evidence_maintainer;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA memory
  FROM memory_evidence_maintainer;
REVOKE ALL PRIVILEGES ON SCHEMA memory FROM memory_evidence_maintainer;
DROP ROLE IF EXISTS memory_evidence_maintainer;

COMMIT;
