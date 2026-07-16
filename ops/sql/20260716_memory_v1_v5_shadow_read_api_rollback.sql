BEGIN;

DO $guard$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 shadow read API rollback requires sage';
  END IF;
END
$guard$;

REVOKE EXECUTE ON FUNCTION memory.read_v5_shadow_claims(uuid[])
  FROM brains_app;
DROP FUNCTION IF EXISTS memory.read_v5_shadow_claims(uuid[]);
DROP FUNCTION IF EXISTS memory.require_v5_reader_context();

DROP POLICY IF EXISTS owner_isolation_v5_reader
  ON memory.claim_observation;
DROP POLICY IF EXISTS owner_isolation_v5_reader
  ON memory.observation;
DROP POLICY IF EXISTS owner_isolation_v5_reader
  ON memory.observation_temporal;
DROP POLICY IF EXISTS owner_isolation_v5_reader
  ON memory.projection_apply_event;

REVOKE ALL ON
  memory.claim,
  memory.claim_observation,
  memory.evidence,
  memory.observation,
  memory.observation_temporal,
  memory.projection_apply_event
FROM memory_v5_reader;
REVOKE USAGE ON SCHEMA memory FROM memory_v5_reader;
REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
  FROM memory_v5_reader;

DROP INDEX IF EXISTS memory.projection_apply_event_owner_claim_idx;
DROP ROLE memory_v5_reader;

COMMIT;
