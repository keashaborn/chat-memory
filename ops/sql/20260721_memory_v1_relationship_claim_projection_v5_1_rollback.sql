BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE predicate_registry_version='memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'cannot roll back V5.1 relationship projection with V5.1 plans present';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.stage_relationship_claim_plan_v5_1(uuid,text,text);
DROP FUNCTION IF EXISTS memory.preflight_relationship_claim_packet_v5_1(uuid,text);
DROP FUNCTION IF EXISTS memory.preflight_relationship_claim_source_v5_1(uuid);
DROP FUNCTION IF EXISTS memory.render_relationship_claim_text_v5_1(
  text,text,text,text,text
);

ALTER TABLE memory.projection_plan_item
  DROP CONSTRAINT projection_plan_item_predicate_registry_version_check;
ALTER TABLE memory.projection_plan_item
  ADD CONSTRAINT projection_plan_item_predicate_registry_version_check
  CHECK (predicate_registry_version='memory_predicate_registry_v5');
ALTER TABLE memory.projection_plan
  DROP CONSTRAINT projection_plan_predicate_registry_version_check;
ALTER TABLE memory.projection_plan
  ADD CONSTRAINT projection_plan_predicate_registry_version_check
  CHECK (predicate_registry_version='memory_predicate_registry_v5');

COMMIT;
