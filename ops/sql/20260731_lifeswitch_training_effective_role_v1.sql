BEGIN;

CREATE VIEW lifeswitch_training.training_set_effective_role_v1
WITH (security_barrier=true, security_invoker=true)
AS
SELECT
  log.owner_user_id,
  log.training_session_id,
  log.training_set_log_id,
  log.capture_role,
  log.exercise_role_snapshot,
  role_event.assigned_role AS training_session_role_event_role,
  session.workout_role_snapshot,
  CASE
    WHEN evidence.distinct_role_count > 1 THEN 'unknown'
    ELSE COALESCE(
      candidates.capture_role,
      candidates.exercise_role_snapshot,
      candidates.training_session_role_event_role,
      candidates.workout_role_snapshot,
      'unknown'
    )
  END AS effective_role,
  CASE
    WHEN evidence.distinct_role_count > 1 THEN 'conflict'
    WHEN candidates.capture_role IS NOT NULL THEN 'capture_role'
    WHEN candidates.exercise_role_snapshot IS NOT NULL
      THEN 'exercise_role_snapshot'
    WHEN candidates.training_session_role_event_role IS NOT NULL
      THEN 'training_session_role_event'
    WHEN candidates.workout_role_snapshot IS NOT NULL
      THEN 'workout_role_snapshot'
    ELSE 'unresolved'
  END AS resolution_source,
  evidence.distinct_role_count > 1 AS role_conflict
FROM lifeswitch_training.training_set_log AS log
JOIN lifeswitch_training.training_session AS session
  ON session.training_session_id=log.training_session_id
 AND session.owner_user_id=log.owner_user_id
LEFT JOIN lifeswitch_training.training_session_role_event AS role_event
  ON role_event.training_session_id=session.training_session_id
 AND role_event.owner_user_id=session.owner_user_id
CROSS JOIN LATERAL (
  SELECT
    CASE WHEN log.capture_role IN ('strength','rehab')
      THEN log.capture_role END AS capture_role,
    CASE WHEN log.exercise_role_snapshot IN ('strength','rehab')
      THEN log.exercise_role_snapshot END AS exercise_role_snapshot,
    CASE WHEN role_event.assigned_role IN ('strength','rehab')
      THEN role_event.assigned_role END AS training_session_role_event_role,
    CASE WHEN session.workout_role_snapshot IN ('strength','rehab')
      THEN session.workout_role_snapshot END AS workout_role_snapshot
) AS candidates
CROSS JOIN LATERAL (
  SELECT pg_catalog.count(DISTINCT candidate.role)::integer
    AS distinct_role_count
  FROM (
    VALUES
      (candidates.capture_role),
      (candidates.exercise_role_snapshot),
      (candidates.training_session_role_event_role),
      (candidates.workout_role_snapshot)
  ) AS candidate(role)
  WHERE candidate.role IS NOT NULL
) AS evidence;

ALTER VIEW lifeswitch_training.training_set_effective_role_v1 OWNER TO sage;

REVOKE ALL ON lifeswitch_training.training_set_effective_role_v1
  FROM PUBLIC,lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;
GRANT SELECT ON lifeswitch_training.training_set_effective_role_v1 TO brains_app;

COMMENT ON VIEW lifeswitch_training.training_set_effective_role_v1 IS
  'Canonical non-mutating per-set role resolution. Conflicting immutable or reviewed evidence resolves to unknown and is never counted as strength or rehab.';
COMMENT ON COLUMN lifeswitch_training.training_set_effective_role_v1.effective_role IS
  'strength, rehab, or unknown after conflict detection and immutable/reviewed precedence.';
COMMENT ON COLUMN lifeswitch_training.training_set_effective_role_v1.resolution_source IS
  'conflict, capture_role, exercise_role_snapshot, training_session_role_event, workout_role_snapshot, or unresolved.';
COMMENT ON COLUMN lifeswitch_training.training_set_effective_role_v1.role_conflict IS
  'True when two or more non-unknown immutable or reviewed candidates disagree.';

COMMIT;
