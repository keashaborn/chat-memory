BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT pg_advisory_xact_lock(hashtextextended('memory_predicate_registry_v5_2_install', 0));

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = 'memory_predicate_registry_v5_2'
      AND (status <> 'proposed' OR runtime_active)
  ) THEN
    RAISE EXCEPTION 'refusing to roll back an active predicate registry V5.2'
      USING ERRCODE = '55000';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.observation
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_plan_item
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) THEN
    RAISE EXCEPTION 'refusing to roll back a used predicate registry V5.2'
      USING ERRCODE = '55000';
  END IF;
END
$guard$;

CREATE TEMP TABLE v5_2_created_predicate ON COMMIT DROP AS
SELECT predicate
FROM memory.predicate_registry_seed
WHERE registry_version = 'memory_predicate_registry_v5_2'
  AND base_predicate_created;

DROP TABLE IF EXISTS memory.relationship_predicate_contract_v5_2;
DROP TABLE IF EXISTS memory.predicate_registry_source_binding_v5_2;
DROP FUNCTION IF EXISTS memory.reject_predicate_registry_v5_2_mutation();

DELETE FROM memory.predicate_contract
WHERE registry_version = 'memory_predicate_registry_v5_2';

DELETE FROM memory.predicate_registry_seed
WHERE registry_version = 'memory_predicate_registry_v5_2';

DELETE FROM memory.predicate_registry_version
WHERE registry_version = 'memory_predicate_registry_v5_2';

DELETE FROM memory.predicate AS base
USING v5_2_created_predicate AS created
WHERE base.predicate = created.predicate
  AND NOT EXISTS (
    SELECT 1 FROM memory.predicate_contract AS contract
    WHERE contract.predicate = base.predicate
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory.observation AS observation
    WHERE observation.predicate = base.predicate
  );

COMMIT;
