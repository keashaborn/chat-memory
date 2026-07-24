\set ON_ERROR_STOP on

BEGIN READ ONLY;
SELECT set_config('app.user_id', :'target_owner_user_id', true);

DO $test$
DECLARE
  actor uuid := memory.current_actor_user_id();
  entity_count integer;
  edge_count integer;
BEGIN
  SELECT count(*)
    INTO entity_count
    FROM memory.read_governed_entity_scope_entities_v1();
  IF entity_count < 1 OR entity_count > 1000 THEN
    RAISE EXCEPTION 'target entity scope count is invalid';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM memory.read_governed_entity_scope_entities_v1()
     WHERE owner_user_id<>actor
  ) THEN
    RAISE EXCEPTION 'entity reader crossed the owner boundary';
  END IF;
  IF (
    SELECT count(*)
      FROM memory.read_governed_entity_scope_entities_v1()
     WHERE entity_type='self'
       AND identity_state='trusted_owner_self'
  ) <> 1 THEN
    RAISE EXCEPTION 'target owner lacks one trusted owner-self';
  END IF;

  SELECT count(*)
    INTO edge_count
    FROM memory.read_governed_entity_scope_edges_v1();
  IF edge_count > 2000 THEN
    RAISE EXCEPTION 'target entity scope edge count is invalid';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM memory.read_governed_entity_scope_edges_v1()
     WHERE owner_user_id<>actor
        OR subject_entity_id IS NULL
        OR object_entity_id IS NULL
        OR subject_entity_type IS NULL
        OR object_entity_type IS NULL
  ) THEN
    RAISE EXCEPTION 'edge reader returned an invalid or cross-owner edge';
  END IF;
END
$test$;

ROLLBACK;

BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner_user_id', true);

SELECT 1 / (
  (
    NOT EXISTS (
      SELECT 1
        FROM memory.read_governed_entity_scope_entities_v1()
       WHERE owner_user_id=:'target_owner_user_id'::uuid
    )
    AND NOT EXISTS (
      SELECT 1
        FROM memory.read_governed_entity_scope_edges_v1()
       WHERE owner_user_id=:'target_owner_user_id'::uuid
    )
  )::integer
) AS cross_owner_isolation;

ROLLBACK;
