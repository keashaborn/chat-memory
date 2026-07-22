BEGIN;

DROP FUNCTION IF EXISTS memory.preflight_relational_stage_bundle_v5_2(
  uuid,text,text,timestamptz
);
DROP FUNCTION IF EXISTS memory.stage_relational_packet_v5_2(
  uuid,uuid,text,text,text,text,text,text
);

DO $registry_constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM memory.entity_resolution_plan
    WHERE predicate_registry_version='memory_predicate_registry_v5_2'
  ) THEN
    ALTER TABLE memory.entity_resolution_plan
      DROP CONSTRAINT entity_resolution_plan_predicate_registry_version_check;
    ALTER TABLE memory.entity_resolution_plan
      ADD CONSTRAINT entity_resolution_plan_predicate_registry_version_check
      CHECK (predicate_registry_version IN (
        'memory_predicate_registry_v5',
        'memory_predicate_registry_v5_1'
      ));
  END IF;
END
$registry_constraint$;

COMMIT;
